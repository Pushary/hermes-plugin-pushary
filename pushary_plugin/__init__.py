import json
import logging
import os
from pathlib import Path

from . import api, approval, host, identity, schemas, tools

logger = logging.getLogger(__name__)

MAX_ERROR_NOTIFICATIONS = 3
SYSTEM_PROMPT_SECTION_ID = "pushary.reach-the-human"

SYSTEM_PROMPT_SECTION = (
    "Pushary reaches the operator when they are away. Honor authorization already "
    "granted in this session. Call pushary_ask only for an unresolved decision or "
    "an action outside that authority; never bypass an enforced host gate. Use "
    "pushary_propose_scope only for an unresolved or requested file boundary. "
    "Follow answered, status and handoffAction (or nextAction); silence is not "
    "consent, and choice/text answers do not authorize a separate action. "
    "Call pushary_notify with context_type task_complete or error for meaningful "
    "unattended results, not every step. Batch related questions. Delivery follows "
    "the user's policy and presence; do not duplicate a live question in chat. "
    "Confirm notifications can offer lock-screen actions; choices and text open "
    "the app. Personal tools reach the operator; Partner tools use external_id "
    "to reach an enrolled customer."
)

TOOL_REGISTRATIONS = (
    ("pushary_notify", schemas.PUSHARY_NOTIFY, "pushary_notify",
     "Send push notification to user's phone"),
    ("pushary_ask", schemas.PUSHARY_ASK, "pushary_ask",
     "Ask user a question via push notification"),
    ("pushary_wait", schemas.PUSHARY_WAIT, "pushary_wait",
     "Wait for user's answer to a push question"),
    ("pushary_cancel", schemas.PUSHARY_CANCEL, "pushary_cancel",
     "Cancel a pending push question"),
    ("pushary_propose_scope", schemas.PUSHARY_PROPOSE_SCOPE, "pushary_propose_scope",
     "Agree what this run may touch before starting it"),
    ("pushary_enroll", schemas.PUSHARY_ENROLL, "pushary_enroll",
     "Connect one of your own end-users' phones for approvals (Partner plan)"),
    ("pushary_ask_end_user", schemas.PUSHARY_ASK_END_USER, "pushary_ask_end_user",
     "Ask one of your own end-users and block on a fail-closed answer (Partner plan)"),
)

_session_totals = {"tools": 0, "errors": 0, "notified": 0}
_last_turn = {"completed": False, "interrupted": False}


def register(ctx):
    for name, schema, handler_name, description in TOOL_REGISTRATIONS:
        ctx.register_tool(
            name=name,
            toolset="pushary",
            schema=schema,
            handler=getattr(tools, handler_name),
            description=description,
        )

    hooks = {
        "pre_llm_call": _on_pre_llm_call,
        "pre_tool_call": _on_pre_tool_call,
        "post_tool_call": _on_post_tool_call,
        "on_session_start": _on_session_start,
        "on_session_end": _on_session_end,
        "on_session_finalize": _on_session_finalize,
    }
    for hook_name, callback in hooks.items():
        ctx.register_hook(hook_name, callback)

    transport = _register_approval_transport(ctx)
    _register_system_prompt_section(ctx)

    skills_dir = Path(__file__).parent / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                ctx.register_skill(child.name, skill_md)

    logger.info(
        "[pushary] Plugin registered: %d tools, %d hooks, approval transport %s",
        len(TOOL_REGISTRATIONS),
        len(hooks),
        "registered" if transport else "unavailable",
    )


def _register_approval_transport(ctx):
    try:
        ctx.register_approval_transport(host.TRANSPORT_NAME, approval.present)
        return True
    except Exception as exc:
        logger.debug("[pushary] approval transport unavailable: %s", exc)
        return False


def _register_system_prompt_section(ctx):
    try:
        ctx.register_system_prompt_section(SYSTEM_PROMPT_SECTION_ID, SYSTEM_PROMPT_SECTION)
    except Exception as exc:
        logger.debug("[pushary] system prompt section unavailable: %s", exc)


def _gated_tools():
    raw = os.environ.get("PUSHARY_GATE_TOOLS", "")
    return {name.strip().lower() for name in raw.split(",") if name.strip()}


def _summarize_args(args):
    if not isinstance(args, dict) or not args:
        return None
    parts = []
    for key, value in args.items():
        rendered = str(value)
        if len(rendered) > 200:
            rendered = rendered[:200] + "…"
        parts.append(f"{key}: {rendered}")
    return "\n".join(parts)[:500]


def _arg_target(args):
    if not isinstance(args, dict):
        return None
    for key in ("command", "path", "file_path", "url", "query"):
        value = args.get(key)
        if value:
            return " ".join(str(value).split()[:2])[:80]
    return None


_APPROVE_VALUES = {"yes", "y", "true", "approve", "approved", "allow", "ok"}


def _block_unapproved(tool_name):
    return {
        "action": "block",
        "message": f"Approval for '{tool_name}' was not obtained. The tool was blocked.",
    }


def _escalate_to_host_gate(tool_name, args):
    summary = _summarize_args(args)
    detail = f"\n{summary}" if summary else ""
    return {
        "action": "approve",
        "message": f"Run {tool_name}?{detail}"[:500],
        "rule_key": tool_name,
    }


def _requested_gate_seconds():
    try:
        return int(os.environ.get("PUSHARY_GATE_TIMEOUT_MS", "55000")) / 1000
    except (TypeError, ValueError):
        return host.GATE_MAX_SECONDS


def _block_undeliverable(tool_name):
    return {
        "action": "block",
        "message": (
            f"'{tool_name}' is gated by Pushary but no phone, browser, or Slack "
            "channel is connected to approve on, so nobody could be asked. Ask in "
            "this session instead, or connect a device in the Pushary dashboard."
        ),
    }


def _on_pre_tool_call(tool_name=None, args=None, session_id=None, **kwargs):
    identity.remember_session(session_id)

    gated = _gated_tools()
    if not gated:
        return None

    name = (tool_name or "").lower()
    if not name or name.startswith("pushary"):
        return None
    if name not in gated:
        return None

    if host.pushary_is_selected_transport():
        return _escalate_to_host_gate(tool_name, args)

    if not api.is_configured():
        return _block_unapproved(tool_name)

    try:
        budget_ms = int(host.gate_budget_seconds(_requested_gate_seconds()) * 1000)
        summary = _summarize_args(args)
        result = json.loads(tools.pushary_ask({
            "question": f"Allow Hermes to run `{tool_name}`?",
            "type": "confirm",
            "context": summary,
            "agent_name": identity.agent_name(),
            "tool_name": tool_name,
            "tool_target": _arg_target(args),
            "action_body": summary,
            "wait": True,
            "timeout_ms": budget_ms,
        }))
        if "error" in result:
            return _block_unapproved(tool_name)

        correlation_id = result.get("correlationId")
        if not correlation_id:
            return _block_unapproved(tool_name)

        answer = result
        if result.get("handoffAction") == "cancel_then_ask_in_current_client":
            cancellation = api.cancel_question(correlation_id)
            if cancellation.get("handoffAction") == "stop":
                return _block_unapproved(tool_name)
            if not cancellation.get("cancelled"):
                answer = api.wait_for_answer(correlation_id, 1000)

        if answer.get("answered"):
            if str(answer.get("value", "")).strip().lower() in _APPROVE_VALUES:
                return None
            return {"action": "block", "message": f"Denied from Pushary: '{tool_name}' was not approved by the user."}

        if answer.get("error"):
            return _block_unapproved(tool_name)

        if not result.get("handoffAction") and not result.get("nextAction"):
            api.cancel_question(correlation_id)
        if result.get("noDevices"):
            return _block_undeliverable(tool_name)
        return _block_unapproved(tool_name)
    except Exception:
        return _block_unapproved(tool_name)


def _tool_failure(result, status, error_message):
    if status is not None:
        return (error_message or "tool returned an error") if status == "error" else None
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, dict) and parsed.get("error"):
        return str(parsed["error"])
    return None


def _on_post_tool_call(tool_name, args, result, duration_ms=0, status=None,
                       error_message=None, **kwargs):
    _session_totals["tools"] += 1

    failure = _tool_failure(result, status, error_message)
    if failure is None:
        return

    _session_totals["errors"] += 1
    if not api.is_configured():
        return
    if _session_totals["notified"] >= MAX_ERROR_NOTIFICATIONS:
        return
    _session_totals["notified"] += 1

    try:
        api.send_notification(
            title="Tool error in Hermes",
            body=f"{tool_name} failed: {failure[:150]}",
            agent_name=identity.agent_name(),
            context={
                "type": "error",
                "errorMessage": failure[:500],
                "summary": f"Tool '{tool_name}' returned an error after {duration_ms}ms",
            },
        )
    except Exception:
        pass


def _on_session_start(session_id=None, **kwargs):
    identity.remember_session(session_id)
    _session_totals.update(tools=0, errors=0, notified=0)
    _last_turn.update(completed=False, interrupted=False)
    _report_session_event("session_start", session_id, can_drain=True)


def _on_pre_llm_call(session_id=None, **kwargs):
    identity.remember_session(session_id)
    if not api.is_configured() or not identity.session_id():
        return None
    result = api.agent_event("user_prompt", identity.session_id(), can_drain=True)
    command = result.get("pendingCommand") if isinstance(result, dict) else None
    if not isinstance(command, str) or not command.strip():
        return None
    return {
        "context": (
            "The operator sent this instruction to the current Hermes session "
            f"from Pushary:\n\n{command.strip()}"
        )
    }


def _report_session_event(event, session_id, can_drain=False):
    if not api.is_configured() or not session_id:
        return
    try:
        api.agent_event(event, session_id, can_drain=can_drain)
    except Exception:
        pass


def _on_session_end(session_id=None, completed=False, interrupted=False, **kwargs):
    identity.remember_session(session_id)
    _last_turn.update(completed=bool(completed), interrupted=bool(interrupted))


def _on_session_finalize(session_id=None, **kwargs):
    _report_session_event("session_closed", session_id or identity.session_id())
    tool_count = _session_totals["tools"]
    error_count = _session_totals["errors"]
    _session_totals.update(tools=0, errors=0, notified=0)

    if not os.environ.get("PUSHARY_AUTO_NOTIFY_SESSION_END"):
        identity.forget_session()
        return
    if not api.is_configured() or not tool_count:
        identity.forget_session()
        return

    completed = _last_turn["completed"]
    status = "completed" if completed else ("interrupted" if _last_turn["interrupted"] else "ended")
    reference = str(session_id or identity.session_id() or "")

    try:
        api.send_notification(
            title=f"Hermes session {status}",
            body=f"Session used {tool_count} tool calls with {error_count} errors",
            agent_name=identity.agent_name(),
            context={
                "type": "task_complete" if completed else "info",
                "summary": f"Session {reference[:8]} {status}",
                "details": [
                    f"{tool_count} tool calls executed",
                    f"{error_count} errors encountered",
                ],
            },
        )
    except Exception:
        pass
    finally:
        identity.forget_session()
