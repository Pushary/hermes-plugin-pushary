TRANSPORT_NAME = "pushary"

HOOK_TIMEOUT_DEFAULT_SECONDS = 30.0
HOOK_TIMEOUT_MAX_SECONDS = 600.0

GATE_HEADROOM_SECONDS = 5.0
GATE_MIN_SECONDS = 5.0
GATE_MAX_SECONDS = 55.0

# The last Hermes that runs a pre_tool_call callback to completion. Anything
# after this abandons one that outlives plugins.hook_callback_timeout.
UNBOUNDED_THROUGH_VERSION = (0, 20, 5)


def _config():
    try:
        from hermes_cli.config import load_config_readonly

        loaded = load_config_readonly()
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _section(node, *path):
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return {}
    return node if isinstance(node, dict) else {}


def _hermes_version():
    """This Hermes as a comparable tuple, or None when it will not say."""
    raw = None
    try:
        from importlib.metadata import version

        raw = version("hermes-cli")
    except Exception:
        try:
            import hermes_cli

            raw = getattr(hermes_cli, "__version__", None)
        except Exception:
            raw = None
    if not raw:
        return None
    parts = []
    for piece in str(raw).split(".")[:3]:
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            return None
        parts.append(int(digits))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def bounds_pre_tool_call():
    """Whether this Hermes abandons a slow pre_tool_call callback.

    The timeout was added after 0.20.5, which runs plugin hooks to completion.
    Clamping the wait on a host that would never have cut it off just shortens
    the window the user has to answer, for nothing.

    Unreadable means assume bounded. A wait that is shorter than it had to be
    costs the user seconds; a wait longer than the host allows costs them the
    answer they already gave.

    That rule is why the private symbol is not asked on its own. `hasattr` on
    `_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS` cannot tell "this host runs hooks to
    completion" apart from "the private name moved", and those two answers want
    opposite behaviour: the first earns the full window, the second spends 55
    seconds against a budget of 30 and loses an answer the user already gave.
    A rename is the likelier of the two, because a private name is the one thing
    a host is free to change. So the symbol is taken as proof of bounded, and
    only a version this side of the timeout landing is taken as proof of the
    other. Anything unreadable falls to bounded.
    """
    try:
        from hermes_cli import plugins
    except Exception:
        return True
    if hasattr(plugins, "_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS"):
        return True
    version = _hermes_version()
    if version is None:
        return True
    return version > UNBOUNDED_THROUGH_VERSION


def hook_callback_timeout_seconds():
    raw = _section(_config(), "plugins").get("hook_callback_timeout")
    if raw is None:
        return HOOK_TIMEOUT_DEFAULT_SECONDS
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return HOOK_TIMEOUT_DEFAULT_SECONDS
    if timeout < 0:
        return HOOK_TIMEOUT_DEFAULT_SECONDS
    return min(timeout, HOOK_TIMEOUT_MAX_SECONDS)


def gate_budget_seconds(requested_seconds):
    if not bounds_pre_tool_call():
        return max(1.0, min(float(requested_seconds), GATE_MAX_SECONDS))
    timeout = hook_callback_timeout_seconds()
    ceiling = (
        GATE_MAX_SECONDS
        if timeout <= 0
        else max(GATE_MIN_SECONDS, timeout - GATE_HEADROOM_SECONDS)
    )
    return max(1.0, min(float(requested_seconds), ceiling, GATE_MAX_SECONDS))


def pushary_is_selected_transport():
    selected = _section(_config(), "security", "approval").get("transport")
    return str(selected or "").strip().lower() == TRANSPORT_NAME
