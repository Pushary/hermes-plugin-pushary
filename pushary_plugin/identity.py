import hashlib
import os
import socket

DEFAULT_AGENT_NAME = "Hermes"
MAX_ID_LENGTH = 128

_session_id = None


def agent_name():
    return os.environ.get("PUSHARY_AGENT_NAME", DEFAULT_AGENT_NAME)


def machine_id():
    try:
        host = socket.gethostname()
    except Exception:
        return None
    if not host:
        return None
    return hashlib.sha256(host.encode("utf-8")).hexdigest()[:8]


def remember_session(value):
    global _session_id
    if value:
        _session_id = str(value)[:MAX_ID_LENGTH]


def forget_session():
    global _session_id
    _session_id = None


def session_id():
    return _session_id
