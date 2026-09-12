import time

from . import api, identity

HERMES_SHELL_TOOL = "terminal"
DECISION_LINE_MAX = 500
ACTION_BODY_MAX = 4000

POLL_CEILING_SECONDS = 50.0

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


def present(request):
    offered = _offered(request.allowed_choices)
    if not offered:
        raise TransportUnavailable("no approval choices offered")
    uses_select = len(offered) > 2

    created = _open_question(request, offered, uses_select)
    if "error" in created:
        raise TransportUnavailable(str(created["error"]))
    if created.get("noDevices"):
        raise TransportUnavailable("no Pushary device is connected")

    correlation_id = created.get("correlationId")
    if not correlation_id:
        raise TransportUnavailable("Pushary did not return a correlationId")

    if created.get("answered"):
        return request.respond(_choice_from_answer(created.get("value"), offered, uses_select))

    deadline = time.monotonic() + max(float(request.timeout_seconds or 0), 0.0)
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        wait_ms = int(min(POLL_CEILING_SECONDS, max(1.0, remaining)) * 1000)
        answer = api.wait_for_answer(correlation_id, wait_ms)
        if answer.get("answered"):
            return request.respond(_choice_from_answer(answer.get("value"), offered, uses_select))
        if answer.get("error"):
            break

    api.cancel_question(correlation_id)
    return request.respond("deny")
