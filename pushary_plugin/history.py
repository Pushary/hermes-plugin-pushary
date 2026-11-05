from __future__ import annotations

import hashlib
import contextvars
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from . import api

logger = logging.getLogger(__name__)
MAX_MESSAGES = 5000
MAX_SOURCE_BYTES = 20_000_000
POLL_SECONDS = 5
SNAPSHOT_REFRESH_SECONDS = 60
AUTHORIZATION_LIFETIME_SECONDS = 60
SNAPSHOT_RETENTION_SECONDS = 48 * 60 * 60
SNAPSHOT_PRUNE_INTERVAL_SECONDS = 60 * 60
SOURCE_UNAVAILABLE = "source_unavailable"
HISTORY_NOT_ALLOWED_WARNING = ("[pushary] Hermes session history is not saved on this computer; it needs "
                               "`npx pushary@latest setup` with the key Hermes uses, or Session history turned on in the Pushary Mac app")
HELPER_ENVIRONMENT_NAMES = frozenset({
    "HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
    "SYSTEMROOT", "PATHEXT", "COMSPEC", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
    "PUSHARY_BASE_URL", "PUSHARY_API_URL", "PUSHARY_CONFIG_FILE", "PUSHARY_TRANSCRIPTS", "PUSHARY_TRANSCRIPT_REGISTRY_DIR",
})


class _Authorization(NamedTuple):
    inputs: tuple[object, ...]
    expires_at: float
    command: list[str] | None


_workers: dict[tuple[str, str], tuple[threading.Event, threading.Thread]] = {}
_authorizations: dict[str, _Authorization] = {}
_lock = threading.Lock()
_next_prune_at = 0.0
_history_not_allowed_reported = False


def _text_blocks(content: object) -> list[dict[str, object]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    return [{"type": "text", "text": block["text"]} for block in content
            if isinstance(block, dict) and block.get("type") in ("text", "input_text", "output_text")
            and isinstance(block.get("text"), str)]


def _tool_blocks(calls: object) -> list[dict[str, object]]:
    blocks = []
    if not isinstance(calls, list):
        return blocks
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("id"), str):
            continue
        function = call.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            continue
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, dict):
            raise ValueError("Invalid Hermes tool arguments")
        blocks.append({"type": "tool_use", "id": call["id"], "name": function["name"], "input": arguments})
    return blocks


def normalize_messages(rows: list[object]) -> list[dict[str, object]]:
    messages = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid Hermes message")
        if not (row.get("active") or row.get("compacted")) or row.get("display_kind") in ("hidden", "model_only"):
            continue
        role = row.get("role")
        if role not in ("user", "assistant", "tool") or row.get("_compressed_summary"):
            continue
        identity = row.get("id")
        stamp = row.get("timestamp")
        if not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0:
            raise ValueError("Invalid Hermes message identity")
        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp <= 0:
            raise ValueError("Invalid Hermes message timestamp")
        content = _text_blocks(row.get("content"))
        if role == "assistant":
            content.extend(_tool_blocks(row.get("tool_calls")))
        elif role == "tool":
            call_id = row.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            content = [{"type": "tool_result", "tool_use_id": call_id, "content": content}]
        messages.append({
            "id": f"hermes:{identity}", "role": "user" if role == "tool" else role,
            "timestamp": datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z"),
            "content": content,
        })
    return messages


def read_display_history(database_path: Path, session_id: str) -> list[dict[str, object]]:
    from hermes_state import SessionDB

    database = SessionDB(db_path=database_path, read_only=True)
    try:
        connection = database._conn
        deadline = time.monotonic() + 2
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute("BEGIN")
        if database.get_session(session_id) is None:
            raise ValueError("Hermes session is not in this profile")
        lineage = database._resume_lineage_ids(session_id)
        if not lineage or len(lineage) > 100:
            raise ValueError("Unsupported Hermes compression lineage")
        columns = [row[1] for row in connection.execute("PRAGMA table_info(messages)")]
        required = {"id", "session_id", "role", "content", "timestamp", "active", "compacted", "display_kind", "message_uid"}
        if not required.issubset(columns):
            raise ValueError("Unsupported Hermes history schema")
        identifiers = ['"' + column.replace('"', '""') + '"' for column in columns]
        row_bytes = " + ".join(f"COALESCE(length(CAST({column} AS BLOB)), 0)" for column in identifiers)
        placeholders = ",".join("?" for _ in lineage)
        count, size = connection.execute(
            f"SELECT COUNT(*), SUM({row_bytes}) FROM messages WHERE session_id IN ({placeholders})", lineage
        ).fetchone()
        if count > MAX_MESSAGES or (size or 0) > MAX_SOURCE_BYTES:
            raise ValueError("Hermes history exceeds the bounded reader limit")
        rows = database.get_messages(session_id, include_compacted=True, include_ancestors=True, limit=MAX_MESSAGES)
        return normalize_messages(rows)
    finally:
        database.close()


def write_snapshot(path: Path, session_id: str, messages: list[dict[str, object]], error: str | None = None, completed: bool = False) -> None:
    value = {"version": 1, "sessionId": session_id, "updatedAtMs": int(time.time() * 1000), "messages": messages, "completed": completed}
    if error:
        value["error"] = error
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_SOURCE_BYTES:
        raise ValueError("Hermes history export exceeds its size limit")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".history-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _helper_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if name in HELPER_ENVIRONMENT_NAMES}


def _registration_environment(key: str) -> dict[str, str]:
    return {**_helper_environment(), "PUSHARY_API_KEY": key, "PUSHARY_BASE_URL": api.BASE_URL}


def _registration_command(key: str) -> list[str] | None:
    root = Path.home() / ".pushary"
    try:
        if (root / "native-transcript-consent.json").exists():
            bridge = (root / "bin" / "pushary-bridge").resolve(strict=True)
            helper = str(bridge.parent / "pushary-native-transcripts")
            command = [helper, "register", "hermes"]
        else:
            helper = shutil.which("pushary-transcript-register")
            if helper is None:
                return None
            command = [helper, "hermes"]
        result = subprocess.run([helper, "authorize-export"],
                                input=json.dumps({"apiKey": key, "baseUrl": api.BASE_URL}), text=True,
                                env=_helper_environment(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False)
        if result.returncode != 0:
            return None
        authorization = json.loads(result.stdout)
        if not isinstance(authorization, dict) or type(authorization.get("version")) is not int or authorization.get("version") != 1 or authorization.get("authorized") is not True:
            return None
        return command
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _file_state(path: Path | None) -> tuple[int, ...] | None:
    if path is None:
        return None
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return (info.st_ino, info.st_mode, info.st_nlink, info.st_uid, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _authorization_inputs(key: str) -> tuple[object, ...]:
    root = Path.home() / ".pushary"
    helper = shutil.which("pushary-transcript-register")
    sources = (root / "native-transcript-consent.json", root / "bridge-auth", root / "bridge-auth" / "agent-key.json",
               root / "machine-id", root / "bin" / "pushary-bridge", api.pushary_config_path(),
               Path(helper) if helper else None)
    return (key, api.BASE_URL, os.environ.get("PUSHARY_TRANSCRIPTS"), helper, *(_file_state(source) for source in sources))


def _authorized_command(key: str) -> list[str] | None:
    inputs = _authorization_inputs(key)
    now = time.monotonic()
    with _lock:
        cached = _authorizations.get(key)
    if cached is not None and cached.inputs == inputs and now < cached.expires_at:
        return cached.command
    command = _registration_command(key)
    with _lock:
        _authorizations[key] = _Authorization(inputs, now + AUTHORIZATION_LIFETIME_SECONDS, command)
    return command


def _report_history_not_allowed_once() -> None:
    global _history_not_allowed_reported
    with _lock:
        if _history_not_allowed_reported:
            return
        _history_not_allowed_reported = True
    logger.warning(HISTORY_NOT_ALLOWED_WARNING)


def _history_root() -> Path:
    return Path.home() / ".pushary" / "hermes-history"


def _snapshot_expired(path: Path, now: float) -> bool:
    return now - path.lstat().st_mtime > SNAPSHOT_RETENTION_SECONDS


def _prune_expired_snapshots(root: Path, now: float) -> None:
    try:
        bindings = [entry for entry in root.iterdir() if not entry.is_symlink() and entry.is_dir()]
    except OSError:
        return
    for binding in bindings:
        try:
            snapshots = list(binding.iterdir())
        except OSError:
            continue
        for snapshot in snapshots:
            try:
                if _snapshot_expired(snapshot, now):
                    snapshot.unlink()
            except OSError:
                continue


def _prune_expired_snapshots_periodically() -> None:
    global _next_prune_at
    now = time.monotonic()
    with _lock:
        if now < _next_prune_at:
            return
        _next_prune_at = now + SNAPSHOT_PRUNE_INTERVAL_SECONDS
    _prune_expired_snapshots(_history_root(), time.time())


def _source_state(database_path: Path) -> tuple[object, ...]:
    return (_file_state(database_path), _file_state(database_path.with_name(database_path.name + "-wal")))


def _snapshot_refresh_due(written_at: float | None, now: float) -> bool:
    return written_at is None or not 0 <= now - written_at < SNAPSHOT_REFRESH_SECONDS


def _is_permanent_source_failure(failure: Exception) -> bool:
    return isinstance(failure, ValueError)


def _snapshot_digest(messages: list[dict[str, object]], error: str | None, completed: bool) -> str:
    encoded = json.dumps([messages, error, completed], ensure_ascii=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_changed_snapshot(path: Path, session_id: str, messages: list[dict[str, object]], error: str | None,
                            completed: bool, written: str | None, refresh_due: bool) -> str | None:
    digest = _snapshot_digest(messages, error, completed)
    if not refresh_due and digest == written:
        return None
    write_snapshot(path, session_id, messages, error=error, completed=completed)
    return digest


def _export_loop(database_path: Path, session_id: str, key: str, command: list[str], stop: threading.Event) -> None:
    binding = hashlib.sha256((api.BASE_URL.rstrip("/") + "\0" + key + "\0" + str(database_path)).encode()).hexdigest()
    path = _history_root() / binding / f"{session_id}.json"
    next_registration = 0
    written_at = None
    source_state = None
    written = None
    warned = False
    revoked = False
    try:
        while True:
            if api._get_api_key() != key or _authorized_command(key) != command:
                revoked = True
                break
            ending = stop.is_set()
            now = time.time()
            refresh_due = ending or _snapshot_refresh_due(written_at, now)
            current_state = _source_state(database_path)
            if refresh_due or current_state != source_state:
                source_state = current_state
                error = None
                try:
                    messages = read_display_history(database_path, session_id)
                except Exception as failure:
                    messages, error = [], SOURCE_UNAVAILABLE
                    if not _is_permanent_source_failure(failure):
                        source_state = None
                try:
                    digest = _write_changed_snapshot(path, session_id, messages, error, ending and error is None, written, refresh_due)
                except ValueError:
                    messages, error = [], SOURCE_UNAVAILABLE
                    digest = _write_changed_snapshot(path, session_id, messages, error, False, written, refresh_due)
                if digest is not None:
                    written, written_at = digest, now
                if error is not None and not warned:
                    logger.warning("[pushary] Hermes history source is unavailable; existing phone history is preserved")
                warned = error is not None
            if ending or time.monotonic() >= next_registration:
                payload = {"session_id": session_id, "cwd": str(database_path.parent), "transcript_path": str(path),
                           "hook_event_name": "SessionEnd" if ending else "SessionStart"}
                result = subprocess.run(command, input=json.dumps(payload), text=True, env=_registration_environment(key),
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
                if result.returncode != 0:
                    logger.warning("[pushary] Hermes history reader could not register")
                next_registration = time.monotonic() + 60
            if ending:
                break
            stop.wait(POLL_SECONDS)
    except Exception:
        logger.warning("[pushary] Hermes history reader stopped; reopen the session to retry")
    finally:
        if revoked:
            path.unlink(missing_ok=True)
        with _lock:
            _workers.pop((str(database_path), session_id), None)


def start_history(session_id: object, parent_session_id: object = None) -> None:
    if parent_session_id or not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        return
    try:
        from hermes_constants import get_hermes_home

        _prune_expired_snapshots_periodically()
        database_path = (get_hermes_home() / "state.db").resolve()
        identity = (str(database_path), session_id)
        with _lock:
            if identity in _workers or len(_workers) >= 32:
                return
        key = api._get_api_key()
        command = _authorized_command(key)
        if command is None:
            _report_history_not_allowed_once()
            return
        stop = threading.Event()
        context = contextvars.copy_context()
        thread = threading.Thread(target=context.run, args=(_export_loop, database_path, session_id, key, command, stop),
                                  name="pushary-history", daemon=True)
        with _lock:
            if identity in _workers or len(_workers) >= 32:
                return
            _workers[identity] = (stop, thread)
        try:
            thread.start()
        except Exception:
            with _lock:
                _workers.pop(identity, None)
            raise
    except Exception:
        logger.warning("[pushary] Hermes history reader could not start")


def stop_history(session_id: str) -> None:
    try:
        from hermes_constants import get_hermes_home
        database_path = str((get_hermes_home() / "state.db").resolve())
    except Exception:
        return
    with _lock:
        worker = _workers.get((database_path, session_id))
    if worker:
        stop, thread = worker
        stop.set()
        thread.join(timeout=8)
        if thread.is_alive():
            logger.warning("[pushary] Hermes final history export is still pending")
