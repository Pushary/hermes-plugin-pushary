import json
import hashlib
import os
import re
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

from . import identity

BASE_URL = os.environ.get("PUSHARY_BASE_URL", "https://pushary.com")
SERVER_BASE = f"{BASE_URL}/api/v1/server"

PENDING_DIR = Path.home() / ".pushary" / "run" / "hermes-commands"


def _pending_path(session_id):
    identity_value = f"{_get_api_key()}:{identity.machine_id()}:{session_id or '_no_session'}"
    name = hashlib.sha256(identity_value.encode("utf-8")).hexdigest()
    return PENDING_DIR / f"{name}.json"


def _read_pending(session_id):
    path = _pending_path(session_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value.get("id"), str) or not isinstance(value.get("text"), str):
            return None
        if value.get("expiresAtMs", 0) <= int(time.time() * 1000):
            path.unlink(missing_ok=True)
            return None
        return value
    except Exception:
        return None


def _write_pending(session_id, value, exclusive=False):
    path = _pending_path(session_id)
    try:
        PENDING_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        if exclusive:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(value, handle)
        else:
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(value), encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        os.chmod(path, 0o600)
        return True
    except Exception:
        if exclusive:
            return False
        pending = _read_pending(session_id)
        return pending is not None \
            and pending.get("id") == value.get("id") \
            and pending.get("delivered") == value.get("delivered")


def _remove_pending(session_id, command_id):
    pending = _read_pending(session_id)
    if pending and pending.get("id") == command_id:
        try:
            _pending_path(session_id).unlink(missing_ok=True)
        except Exception:
            pass


def _ack_command(command_id, session_id):
    ack = urllib.request.Request(
        f"{BASE_URL}/api/agent/command/ack",
        data=json.dumps(_with_identity({
            "commandId": command_id,
            "sessionId": session_id,
            "state": "delivered",
        })).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_get_api_key()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(ack, timeout=10) as response:
            return 200 <= response.status < 300
    except Exception:
        return False

# Fail-closed affirmative set for a confirm decision. Anything not in here (a
# decline, an empty answer, a timeout) is treated as "not approved".
AFFIRMATIVE = {"yes", "y", "true", "approve", "approved", "allow", "ok", "okay", "confirm", "accept"}


def is_affirmative(value):
    return str(value or "").strip().lower() in AFFIRMATIVE


def _parse_sse(body):
    last = None
    for event in re.split(r"\r?\n\r?\n", body):
        data = "\n".join(
            line[5:].lstrip()
            for line in re.split(r"\r?\n", event)
            if line.startswith("data:")
        ).strip()
        if not data:
            continue
        try:
            last = json.loads(data)
        except json.JSONDecodeError:
            continue
    return last


def is_configured():
    return bool(_configured_key())


def _configured_key():
    key = os.environ.get("PUSHARY_API_KEY")
    if key:
        return key
    path = Path(os.environ.get("PUSHARY_CONFIG_FILE", "")).expanduser() \
        if os.environ.get("PUSHARY_CONFIG_FILE") else Path.home() / ".pushary" / "config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("apiKey")
        return value if isinstance(value, str) and value else None
    except Exception:
        return None


def _get_api_key():
    key = _configured_key()
    if not key:
        raise ValueError(
            "PUSHARY_API_KEY not set. Get your key at https://pushary.com/sign-up?from=hermes"
        )
    return key


def agent_event(event, session_id, can_drain=False):
    """Report a Hermes session and pick up a queued notch instruction."""
    try:
        pending = _read_pending(session_id) if can_drain else None
        payload = _with_identity({
            "event": event,
            "agentType": "hermes",
            "agentName": identity.agent_name(),
            "sessionId": session_id,
            "canDrainCommand": can_drain and pending is None,
            "ackMode": can_drain and pending is None,
            "skipCommandDrain": pending is not None,
        })
        req = urllib.request.Request(
            f"{BASE_URL}/api/agent/event",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_get_api_key()}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
        result = json.loads(raw) if raw.strip() else {}
        if not isinstance(result, dict):
            return result
        if pending:
            result.pop("pendingCommand", None)
            result.pop("pendingCommandId", None)
            if not _ack_command(pending["id"], session_id):
                return result
            if pending.get("delivered") is True:
                _remove_pending(session_id, pending["id"])
                return result
            delivered = {**pending, "delivered": True}
            if not _write_pending(session_id, delivered):
                return result
            result["pendingCommand"] = pending["text"]
            result["pendingCommandId"] = pending["id"]
            return result
        command_id = result.get("pendingCommandId")
        command = result.get("pendingCommand")
        if isinstance(command_id, str) and isinstance(command, str):
            received = {
                "id": command_id,
                "text": command,
                "expiresAtMs": result.get("pendingCommandExpiresAtMs", int(time.time() * 1000) + 3_600_000),
                "delivered": False,
            }
            saved = _write_pending(session_id, received, exclusive=True)
            if not saved or not _ack_command(command_id, session_id):
                result.pop("pendingCommand", None)
                result.pop("pendingCommandId", None)
                return result
            if not _write_pending(session_id, {**received, "delivered": True}):
                result.pop("pendingCommand", None)
                result.pop("pendingCommandId", None)
        return result
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:
        return {"error": str(exc)}


def _mcp_call(tool_name, params):
    api_key = _get_api_key()
    url = f"{BASE_URL}/api/mcp/mcp"
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": params},
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    blocking = tool_name in ("wait_for_answer", "propose_scope") or (tool_name == "ask_user" and params.get("wait", True))
    timeout = min(max(params.get("timeoutMs", 30000), 1000), 55000) / 1000 + 2 if blocking else 4 if tool_name == "cancel_question" else 10
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            content_type = resp.headers.get_content_type()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8").strip()[:200]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {e.reason}" + (f" — {detail}" if detail else "")}
    except Exception as e:
        return {"error": str(e)}

    if "text/event-stream" in (content_type or ""):
        body = _parse_sse(raw)
    else:
        try:
            body = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError:
            body = None

    if not body:
        return {"error": "Empty response from Pushary"}

    if "error" in body:
        err = body["error"]
        return {"error": err.get("message", str(err)) if isinstance(err, dict) else str(err)}

    text = body.get("result", {}).get("content", [{}])[0].get("text", "{}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


IDENTITY_PARAM_NAMES = {
    "session_id": "sessionId",
    "machine_id": "machineId",
}

DECISION_PARAM_NAMES = {
    **IDENTITY_PARAM_NAMES,
    "tool_name": "toolName",
    "tool_target": "toolTarget",
    "repo_key": "repoKey",
    "intent": "intent",
    "action": "action",
    "blocker": "blocker",
    "action_body": "actionBody",
    "scope_path": "scopePath",
}


def _with_decision_fields(params, values, accepted=DECISION_PARAM_NAMES):
    unknown = set(values) - set(accepted)
    if unknown:
        raise TypeError("unexpected parameters: " + ", ".join(sorted(unknown)))
    for key, camel in accepted.items():
        value = values.get(key)
        if value:
            params[camel] = value
    return _with_identity(params)


def _with_identity(params):
    for camel, value in (
        ("sessionId", identity.session_id()),
        ("machineId", identity.machine_id()),
    ):
        if value and not params.get(camel):
            params[camel] = value
    return params


def send_notification(title, body, agent_name=None, context=None, **identity_fields):
    params = {"title": title, "body": body}
    if agent_name:
        params["agentName"] = agent_name
    if context:
        params["context"] = context
    return _mcp_call(
        "send_notification",
        _with_decision_fields(params, identity_fields, IDENTITY_PARAM_NAMES),
    )


def ask_user(question, question_type="confirm", options=None, placeholder=None,
             context=None, agent_name=None, wait=True, timeout_ms=None, **decision):
    params = {"question": question, "type": question_type, "wait": wait}
    if options:
        params["options"] = options
    if placeholder:
        params["placeholder"] = placeholder
    if context:
        params["context"] = context
    if agent_name:
        params["agentName"] = agent_name
    if timeout_ms is not None:
        params["timeoutMs"] = min(max(int(timeout_ms), 1000), 55000)
    return _mcp_call("ask_user", _with_decision_fields(params, decision))


def propose_scope(done_when, session_id, allowed_paths=None, off_limits_paths=None,
                  agent_name=None, timeout_ms=None):
    params = {"doneWhen": done_when, "sessionId": session_id}
    if allowed_paths:
        params["allowedPaths"] = allowed_paths
    if off_limits_paths:
        params["offLimitsPaths"] = off_limits_paths
    if agent_name:
        params["agentName"] = agent_name
    if timeout_ms is not None:
        params["timeoutMs"] = min(max(int(timeout_ms), 1000), 55000)
    return _mcp_call("propose_scope", _with_identity(params))


def wait_for_answer(correlation_id, timeout_ms=30000):
    return _mcp_call("wait_for_answer", {
        "correlationId": correlation_id,
        "timeoutMs": min(max(int(timeout_ms), 1000), 55000),
    })


def cancel_question(correlation_id):
    return _mcp_call("cancel_question", {"correlationId": correlation_id})


# --- Partner tools: reach your OWN product's end-users on their phones -----------
#
# The four tools above speak MCP (/api/mcp/mcp), which targets the phones connected
# to the owner's workspace. Enrolling an end-user and the durable end-user decision
# ledger are REST-only, so the partner tools bypass MCP and call the Server API
# (/api/v1/server/*) directly with the same Bearer key. Both require the Partner plan.


def _rest_call(method, path, body=None, params=None):
    """Call the REST Server API directly and return the parsed JSON dict.

    On a transport or HTTP error returns {"error": <message>}; a 403 carries the
    "requires the Partner plan" message so the model can surface it rather than
    retrying blindly.
    """
    api_key = _get_api_key()
    url = f"{SERVER_BASE}{path}"
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if query:
            url = f"{url}?{query}"

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            parsed = json.loads(e.read().decode("utf-8"))
            detail = parsed.get("error") or parsed.get("message") or ""
        except Exception:
            detail = ""
        return {"error": detail or f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}

    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {"error": "Invalid response from Pushary"}


def enroll_end_user(external_id):
    """Mint a keyless one-tap connect link for one of your end-users."""
    return _rest_call("POST", "/enroll", body={"externalId": external_id})


def create_end_user_decision(question, external_id, question_type="confirm",
                             options=None, agent_name=None, context=None,
                             idempotency_key=None):
    """Open a durable decision addressed to a specific end-user (does not wait)."""
    body = {
        "question": question,
        "type": question_type,
        "externalId": external_id,
        "wait": False,
    }
    if options:
        body["options"] = options
    if agent_name:
        body["agentName"] = agent_name
    if context:
        body["context"] = context
    if idempotency_key:
        body["idempotencyKey"] = idempotency_key
    return _rest_call("POST", "/decisions", body=body)


def get_end_user_decision(decision_id, wait_seconds=45):
    """Durably long-poll a decision (server caps wait at 55s)."""
    seg = urllib.parse.quote(str(decision_id), safe="")
    return _rest_call("GET", f"/decisions/{seg}", params={"wait": wait_seconds})
