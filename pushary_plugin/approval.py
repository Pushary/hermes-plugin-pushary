import logging
import os
import sys
import time
from typing import Literal, Optional, Tuple

from . import api, approval_ledger, cancellation, desktop_answers, desktop_contract, identity, installation

logger = logging.getLogger(__name__)

HERMES_SHELL_TOOL = "terminal"
DECISION_LINE_MAX = 500
ACTION_BODY_MAX = 4000

POLL_CEILING_SECONDS = 50.0
WITHDRAW_RESERVE_SECONDS = 10.0
HOST_RETURN_MARGIN_SECONDS = 1.0
LATE_ANSWER_POLL_MS = 1000

TUI_GATEWAY_MODULE = "tui_gateway.server"

RouteAnswerability = Literal["requester", "routed_elsewhere", "unknown"]
ANSWERABLE_BY_REQUESTER: RouteAnswerability = "requester"
ROUTED_ELSEWHERE: RouteAnswerability = "routed_elsewhere"
ANSWERABILITY_UNKNOWN: RouteAnswerability = "unknown"

Withdrawal = Literal["withdrawn", "answered_late", "ended", "stopped"]
WITHDRAWN: Withdrawal = "withdrawn"
ANSWERED_LATE: Withdrawal = "answered_late"
ENDED_UNANSWERED: Withdrawal = "ended"
STOPPED: Withdrawal = "stopped"


CHOICE_LABELS = (
    ("once", "Allow once"),
    ("session", "Allow for this session"),
    ("always", "Always allow"),
    ("deny", "Deny"),
)


class TransportUnavailable(Exception):
    pass


QUESTION_COMMAND_MAX = 160

PLUGIN_RULE_MARKER = "(plugin approval rule)"


def _offered(allowed):
    return [pair for pair in CHOICE_LABELS if pair[0] in set(allowed or ())]


def _command_head(command):
    return " ".join(str(command or "").split()[:2])[:80]


def _command_summary(command):
    collapsed = " ".join(str(command or "").split())
    if len(collapsed) <= QUESTION_COMMAND_MAX:
        return collapsed
    return collapsed[:QUESTION_COMMAND_MAX - 1] + "…"


def _choice_from_answer(value, offered, uses_select):
    answer = str(value or "").strip()
    if uses_select:
        for choice, label in offered:
            if answer.lower() == label.lower():
                return choice
        return "deny"
    return "once" if api.is_affirmative(answer) else "deny"


def _subject(request):
    """What to put in the question.

    A dangerous-command approval carries the real command. A plugin rule
    escalated from pre_tool_call carries a synthetic display label instead, and
    the plugin's own message is the part worth reading, so use that.
    """
    command = str(request.command or "")
    if PLUGIN_RULE_MARKER in command:
        lines = str(request.description or "").strip().splitlines()
        if lines and lines[0].strip():
            return _command_summary(lines[0])
    return _command_summary(command)


def _open_question(request, offered, uses_select):
    command = str(request.command or "")
    return api.ask_user(
        question=f"Allow Hermes to run `{_subject(request)}`?",
        question_type="select" if uses_select else "confirm",
        options=[label for _, label in offered] if uses_select else None,
        context=str(request.description or "")[:500] or None,
        agent_name=identity.agent_name(),
        session_id=identity.session_id(),
        machine_id=identity.machine_id(),
        tool_name=HERMES_SHELL_TOOL,
        tool_target=_command_head(command),
        action=command[:DECISION_LINE_MAX] or None,
        action_body=command[:ACTION_BODY_MAX] or None,
        blocker=(
            f"Hermes flagged this command as {request.pattern_key}"[:DECISION_LINE_MAX]
            if request.pattern_key
            else None
        ),
        wait=False,
    )


def _stopped(result):
    return result.get("handoffAction") == cancellation.STOP_HANDOFF or result.get("status") in (
        "cancelled", cancellation.UNAVAILABLE_STATUS, "stopped")


def _route_answerability(created) -> RouteAnswerability:
    requester_may_answer = created.get("requesterMayAnswer")
    if requester_may_answer is True:
        return ANSWERABLE_BY_REQUESTER
    if requester_may_answer is False:
        return ROUTED_ELSEWHERE
    return ANSWERABILITY_UNKNOWN


def _routed_to(created) -> Optional[str]:
    routed_to = created.get("routedTo")
    return routed_to if isinstance(routed_to, str) and routed_to.strip() else None


def _withdraw(correlation_id) -> Tuple[Withdrawal, dict]:
    try:
        result = cancellation.classify_cancellation(api.cancel_question(correlation_id))
        if result in (cancellation.WITHDRAWN_BY_THIS_CALL, cancellation.CANCELLED_UNPROVEN):
            return WITHDRAWN, {}
        if result == cancellation.NOT_PENDING:
            answer = api.wait_for_answer(correlation_id, LATE_ANSWER_POLL_MS)
            if answer.get("answered"):
                return ANSWERED_LATE, answer
            if answer.get("status") in ("expired", "missing"):
                return ENDED_UNANSWERED, answer
    except Exception:
        pass
    return STOPPED, {}


def _wall_clock_ms():
    return int(time.time() * 1000)


def announce_approvals_changed() -> None:
    if TUI_GATEWAY_MODULE not in sys.modules:
        return
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
    except Exception:
        return
    try:
        broadcast_plugin_event(desktop_contract.PLUGIN_ID, desktop_contract.APPROVALS_CHANGED_EVENT, {})
    except Exception:
        logger.debug("[pushary] could not tell Hermes Desktop about an approval", exc_info=True)


def _open_answer_slot(request, correlation_id: str, offered, answerability: RouteAnswerability,
                      routed_to: Optional[str], opened_at_ms: int, deadline_at_ms: int) -> None:
    try:
        desktop_answers.shared_answers().open_slot(
            correlation_id,
            request_id=getattr(request, "request_id", None),
            subject=_subject(request),
            choices=approval_ledger.offered_choices(offered),
            opened_at_ms=opened_at_ms,
            deadline_at_ms=deadline_at_ms,
            answerable_here=answerability == ANSWERABLE_BY_REQUESTER,
            routed_to=routed_to,
        )
    except Exception:
        logger.debug("[pushary] could not offer an approval to Hermes Desktop", exc_info=True)


def _close_answer_slot(correlation_id: str) -> None:
    try:
        desktop_answers.shared_answers().close_slot(correlation_id)
    except Exception:
        logger.debug("[pushary] could not withdraw an approval from Hermes Desktop", exc_info=True)


def _record_opened(request, correlation_id: str, offered, answerability: RouteAnswerability,
                   routed_to: Optional[str]) -> None:
    opened_at_ms = _wall_clock_ms()
    deadline_at_ms = opened_at_ms + int(float(request.timeout_seconds or 0) * 1000)
    _open_answer_slot(request, correlation_id, offered, answerability, routed_to, opened_at_ms, deadline_at_ms)
    try:
        home = installation.hermes_home()
        approval_ledger.prune(home, time.time())
        opened = approval_ledger.open_approval(
            home,
            correlation_id=correlation_id,
            request_id=getattr(request, "request_id", None),
            subject=_subject(request),
            choices=approval_ledger.offered_choices(offered),
            pid=os.getpid(),
            opened_at_ms=opened_at_ms,
            deadline_at_ms=deadline_at_ms,
        )
    except Exception:
        logger.debug("[pushary] could not record an open approval", exc_info=True)
        opened = False
    if opened:
        announce_approvals_changed()


def _record_settled(correlation_id: str, outcome, answer_source: Optional[str]) -> None:
    try:
        settled = approval_ledger.settle_approval(
            installation.hermes_home(),
            correlation_id,
            outcome=outcome,
            answer_source=answer_source,
            settled_at_ms=_wall_clock_ms(),
        )
    except Exception:
        logger.debug("[pushary] could not record a settled approval", exc_info=True)
        return
    if settled:
        announce_approvals_changed()


def record_host_outcome(request_id, choice) -> None:
    try:
        desktop_answers.shared_answers().end_for_request(request_id)
    except Exception:
        logger.debug("[pushary] could not end an approval in Hermes Desktop", exc_info=True)
    try:
        recorded = approval_ledger.record_host_outcome(installation.hermes_home(), request_id, choice, _wall_clock_ms())
    except Exception:
        logger.debug("[pushary] could not record Hermes's approval outcome", exc_info=True)
        return
    if recorded:
        announce_approvals_changed()


def _decided(request, correlation_id, choice, answer_source):
    _record_settled(correlation_id, choice, answer_source)
    return request.respond(choice)


def _answer_source(answer) -> str:
    source = answer.get("answerSource")
    return source if approval_ledger.is_token(source) else approval_ledger.UNNAMED_ANSWER_SOURCE


def _answered(request, correlation_id, answer, offered, uses_select):
    choice = _choice_from_answer(answer.get("value"), offered, uses_select)
    return _decided(request, correlation_id, choice, _answer_source(answer))


def _offered_choices(offered) -> Tuple[str, ...]:
    return tuple(choice for choice, _ in offered)


def _host_has_finished(request, correlation_id: str) -> bool:
    try:
        if desktop_answers.shared_answers().has_ended(correlation_id):
            return True
        return approval_ledger.host_has_finished(installation.hermes_home(), getattr(request, "request_id", None))
    except Exception:
        return False


def _desktop_decision(correlation_id: str, offered) -> Optional[str]:
    try:
        return desktop_answers.shared_answers().desktop_decision(correlation_id, _offered_choices(offered))
    except Exception:
        logger.debug("[pushary] could not read a Hermes Desktop answer", exc_info=True)
        return None


def _final_say(correlation_id: str, offered, host_deadline: float, claim_may_still_win: bool) -> Optional[str]:
    wait = min(desktop_answers.CLAIM_RESOLVE_SECONDS, host_deadline - time.monotonic()) if claim_may_still_win else 0.0
    try:
        return desktop_answers.shared_answers().take_final_say(correlation_id, _offered_choices(offered),
                                                               claim_wait_seconds=wait)
    except Exception:
        logger.debug("[pushary] could not settle who answers this approval", exc_info=True)
        return None


def present(request):
    offered = _offered(request.allowed_choices)
    if not offered:
        return request.respond("deny")
    uses_select = [choice for choice, _ in offered] != ["once", "deny"]
    # Reserve time for withdrawal before the host discards a late decision.
    started_at = time.monotonic()
    timeout_seconds = float(request.timeout_seconds or 0)
    deadline = started_at + max(timeout_seconds - WITHDRAW_RESERVE_SECONDS, 0.0)
    host_deadline = started_at + max(timeout_seconds - HOST_RETURN_MARGIN_SECONDS, 0.0)
    created = _open_question(request, offered, uses_select)
    if created.get("answered"):
        return request.respond(_choice_from_answer(created.get("value"), offered, uses_select))
    if _stopped(created):
        return request.respond("deny")
    correlation_id = created.get("correlationId")
    if not correlation_id:
        raise TransportUnavailable("Pushary did not create an approval")
    answerability = _route_answerability(created)
    _record_opened(request, correlation_id, offered, answerability, _routed_to(created))
    try:
        return _await_decision(request, created, correlation_id, offered, uses_select, answerability,
                               started_at, deadline, host_deadline)
    finally:
        _close_answer_slot(correlation_id)


def _await_decision(request, created, correlation_id: str, offered, uses_select: bool,
                    answerability: RouteAnswerability, started_at: float, deadline: float, host_deadline: float):
    may_hand_back = answerability != ROUTED_ELSEWHERE
    nothing_was_delivered = bool(created.get("noDevices") or created.get("status") in ("terminal", "notified"))
    policy_deadline = None
    window_ms = created.get("policyTimeoutMs")
    if may_hand_back and created.get("deliveryMode") == "push_first" and isinstance(window_ms, (int, float)) and window_ms >= 0:
        policy_deadline = started_at + window_ms / 1000
        deadline = min(deadline, policy_deadline)
    handoff = may_hand_back and (nothing_was_delivered or bool(created.get("suppressed")))
    nobody_can_answer = not may_hand_back and nothing_was_delivered
    answer = created
    try:
        while not handoff and not nobody_can_answer and time.monotonic() < deadline:
            if _host_has_finished(request, correlation_id):
                answer = {"handoffAction": "stop"}
                break
            remaining = deadline - time.monotonic()
            answer = api.wait_for_answer(correlation_id, int(min(POLL_CEILING_SECONDS, max(1.0, remaining)) * 1000))
            if answer.get("answered"):
                return _answered(request, correlation_id, answer, offered, uses_select)
            if _stopped(answer) or answer.get("error") or answer.get("status") in ("expired", "missing"):
                break
            # Some hosts/proxies return pending immediately instead of long-polling.
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    except Exception:
        answer = {"handoffAction": "stop"}

    desktop = _desktop_decision(correlation_id, offered)
    if desktop:
        return _decided(request, correlation_id, desktop, approval_ledger.DESKTOP_ANSWER_SOURCE)
    withdrawal, late = _withdraw(correlation_id)
    if withdrawal == ANSWERED_LATE:
        return _answered(request, correlation_id, late, offered, uses_select)
    desktop = _final_say(correlation_id, offered, host_deadline, claim_may_still_win=withdrawal == STOPPED)
    if desktop:
        return _decided(request, correlation_id, desktop, approval_ledger.DESKTOP_ANSWER_SOURCE)
    if _stopped(answer) or withdrawal == STOPPED:
        return _decided(request, correlation_id, "deny", None)
    if handoff or (policy_deadline is not None and time.monotonic() >= policy_deadline):
        # Only a fenced question may reach Hermes's configured builtin fallback.
        _record_settled(correlation_id, approval_ledger.HANDED_TO_HERMES, None)
        raise TransportUnavailable("Approval handed back to Hermes")
    return _decided(request, correlation_id, "deny", None)
