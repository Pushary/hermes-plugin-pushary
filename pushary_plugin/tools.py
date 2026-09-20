import json
import time
import uuid
from . import api, identity

CONTEXT_FIELDS = {
    "summary": "summary",
    "details": "details",
    "files_changed": "filesChanged",
    "error_message": "errorMessage",
    "next_steps": "nextSteps",
}


def _accepted_fields(params, accepted):
    return {key: params[key] for key in accepted if params.get(key) is not None}


def pushary_notify(params, **kwargs):
    title = params.get("title", "")
    body = params.get("body", "")
    agent_name = params.get("agent_name")

    context = None
    context_type = params.get("context_type")
    if context_type:
        context = {"type": context_type}
        for field, camel in CONTEXT_FIELDS.items():
            value = params.get(field)
            if value is not None:
                context[camel] = value

    try:
        result = api.send_notification(
            title,
            body,
            agent_name=agent_name,
            context=context,
            **_accepted_fields(params, api.IDENTITY_PARAM_NAMES),
        )
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


def pushary_ask(params, **kwargs):
    question = params.get("question", "")
    question_type = params.get("type", "confirm")
    options = params.get("options")
    placeholder = params.get("placeholder")
    context = params.get("context")
    agent_name = params.get("agent_name")
    wait = params.get("wait", True)
    timeout_ms = params.get("timeout_ms", 30000)

    try:
        result = api.ask_user(
            question,
            question_type=question_type,
            options=options,
            placeholder=placeholder,
            context=context,
            agent_name=agent_name,
            wait=wait,
            timeout_ms=timeout_ms,
            **_accepted_fields(params, api.DECISION_PARAM_NAMES),
        )

        next_action = result.get("handoffAction") or result.get("nextAction")
        if (
            not wait
            or "error" in result
            or result.get("answered")
            or (next_action and next_action != "wait_for_answer")
        ):
            return json.dumps(result)

        correlation_id = result.get("correlationId")
        if not correlation_id:
            return json.dumps(result)

        answer = api.wait_for_answer(correlation_id, timeout_ms)
        return json.dumps({
            "correlationId": correlation_id,
            "question": question,
            "type": question_type,
            **answer,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def pushary_wait(params, **kwargs):
    correlation_id = params.get("correlation_id", "")
    timeout_ms = params.get("timeout_ms", 30000)

    try:
        result = api.wait_for_answer(correlation_id, timeout_ms)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


def pushary_cancel(params, **kwargs):
    correlation_id = params.get("correlation_id", "")

    try:
        result = api.cancel_question(correlation_id)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


def pushary_propose_scope(params, **kwargs):
    done_when = params.get("done_when", "")
    if not done_when:
        return json.dumps({"error": "done_when is required"})

    session = params.get("session_id") or identity.session_id()
    if not session:
        return json.dumps({
            "error": "No session id is available yet, so a scope contract could not be "
                     "enforced. Proceed without one and ask before each risky step.",
        })

    try:
        result = api.propose_scope(
            done_when,
            session,
            allowed_paths=params.get("allowed_paths"),
            off_limits_paths=params.get("off_limits_paths"),
            promises=params.get("promises"),
            agent_name=params.get("agent_name") or identity.agent_name(),
            timeout_ms=params.get("timeout_ms"),
        )
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


def pushary_enroll(params, **kwargs):
    """Partner tool: connect one of your OWN end-users' phones (keyless, one tap)."""
    external_id = params.get("external_id", "")
    if not external_id:
        return json.dumps({"error": "external_id is required"})

    try:
        result = api.enroll_end_user(external_id)
        if "error" in result:
            return json.dumps(result)
        return json.dumps({
            "externalId": result.get("externalId", external_id),
            "connectLink": result.get("universalLink"),
            "expiresInSeconds": result.get("expiresInSeconds"),
            "hint": (
                "Show connectLink to this end-user. One tap connects their phone for "
                "approvals. The link is single-use and expires, so cache the enrollment, "
                "not the link."
            ),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def pushary_ask_end_user(params, **kwargs):
    """Partner tool: ask one of your OWN end-users and block on a fail-closed answer.

    Opens a durable decision addressed to external_id, then long-polls the ledger
    until it resolves or the timeout passes. Fail-closed: only an answered,
    affirmative confirm returns approved=True.
    """
    question = params.get("question", "")
    external_id = params.get("external_id", "")
    question_type = params.get("type", "confirm")
    options = params.get("options")
    context = params.get("context")
    agent_name = params.get("agent_name")
    try:
        timeout_seconds = max(1, int(params.get("timeout_seconds", 50)))
    except (TypeError, ValueError):
        timeout_seconds = 50

    if not question or not external_id:
        return json.dumps({"error": "question and external_id are required"})

    try:
        created = api.create_end_user_decision(
            question,
            external_id,
            question_type=question_type,
            options=options,
            agent_name=agent_name,
            context=context,
            # One tool call = one intended ask. A fresh key per invocation means two
            # identical-text asks never collapse into one silent auto-approval.
            idempotency_key=uuid.uuid4().hex,
        )
        if "error" in created:
            return json.dumps(created)

        decision_id = created.get("decisionId")
        if not decision_id:
            return json.dumps({"error": "Pushary did not return a decisionId", "raw": created})

        status = created.get("status", "pending")
        value = created.get("value")

        deadline = time.monotonic() + timeout_seconds
        while status == "pending" and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            wait = max(1, min(45, int(remaining) + 1))
            polled = api.get_end_user_decision(decision_id, wait_seconds=wait)
            if "error" in polled:
                break
            status = polled.get("status", status)
            value = polled.get("value")

        answered = status == "answered"
        approved = answered and question_type == "confirm" and api.is_affirmative(value)
        return json.dumps({
            "decisionId": decision_id,
            "externalId": external_id,
            "status": status,
            "answered": answered,
            "approved": approved,
            "value": value,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})
