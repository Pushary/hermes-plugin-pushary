import json
import re
from typing import AbstractSet, Any, List, Mapping, Optional

HERMES_SHELL_TOOL = "terminal"
GATE_TOOLS_ENV = "PUSHARY_GATE_TOOLS"
TERMINAL_BACKEND_ENV = "TERMINAL_ENV"
DOCKER_MOUNT_CWD_ENV = "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE"
DOCKER_VOLUMES_ENV = "TERMINAL_DOCKER_VOLUMES"

DEFAULT_BACKEND = "local"
DOCKER_BACKEND = "docker"
BACKENDS_THAT_SKIP_APPROVALS = frozenset({"modal", "daytona", "vercel_sandbox"})

BACKEND_NAME = re.compile(r"^[a-z0-9_]+$")
TRUE_WORDS = frozenset({"true", "1", "yes"})
HOST_PATH_PREFIXES = ("/", "~", "./", "../")
WINDOWS_DRIVE_SEPARATORS = ("/", "\\")
GATE_TOOLS_SEPARATOR = ","


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _backend_name(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    name = value.strip()
    return name if BACKEND_NAME.match(name) else None


def terminal_backend(environ: Mapping[str, str], terminal_config: Mapping[str, Any]) -> Optional[str]:
    for value in (environ.get(TERMINAL_BACKEND_ENV), terminal_config.get("backend")):
        if not _is_blank(value):
            return _backend_name(value)
    return DEFAULT_BACKEND


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return isinstance(value, str) and value.strip().lower() in TRUE_WORDS


def volume_binds_host_path(spec: Any) -> bool:
    if not isinstance(spec, str):
        return False
    volume = spec.strip()
    if not volume:
        return False
    if volume.startswith(HOST_PATH_PREFIXES):
        return True
    return len(volume) >= 3 and volume[1] == ":" and volume[2] in WINDOWS_DRIVE_SEPARATORS


def _docker_volumes(environ: Mapping[str, str], terminal_config: Mapping[str, Any]) -> List[Any]:
    raw = environ.get(DOCKER_VOLUMES_ENV)
    volumes: Any = terminal_config.get("docker_volumes")
    if raw is not None:
        try:
            volumes = json.loads(raw)
        except ValueError:
            return []
    return volumes if isinstance(volumes, list) else []


def _docker_may_reach_host(environ: Mapping[str, str], terminal_config: Mapping[str, Any]) -> bool:
    mount = environ.get(DOCKER_MOUNT_CWD_ENV, terminal_config.get("docker_mount_cwd_to_workspace"))
    return _is_true(mount) or any(volume_binds_host_path(volume) for volume in _docker_volumes(environ, terminal_config))


def skips_dangerous_command_approvals(environ: Mapping[str, str], terminal_config: Mapping[str, Any]) -> bool:
    backend = terminal_backend(environ, terminal_config)
    if backend == DOCKER_BACKEND:
        return not _docker_may_reach_host(environ, terminal_config)
    return backend in BACKENDS_THAT_SKIP_APPROVALS


def _gate_tools_with_shell(environ: Mapping[str, str]) -> str:
    listed = [entry.strip() for entry in environ.get(GATE_TOOLS_ENV, "").split(GATE_TOOLS_SEPARATOR) if entry.strip()]
    return GATE_TOOLS_SEPARATOR.join([*listed, HERMES_SHELL_TOOL])


def shell_gate_warning(
    environ: Mapping[str, str],
    terminal_config: Mapping[str, Any],
    gated_tools: AbstractSet[str],
) -> Optional[str]:
    if HERMES_SHELL_TOOL in gated_tools or not skips_dangerous_command_approvals(environ, terminal_config):
        return None
    backend = terminal_backend(environ, terminal_config)
    return (
        f"[pushary] Hermes skips its dangerous-command approvals on the {backend} terminal backend, "
        f"because it treats the container as the boundary. Pushary only gates the tools listed in "
        f"{GATE_TOOLS_ENV}, and {HERMES_SHELL_TOOL} is not one of them, so no shell command reaches "
        f"your phone. To gate them, run: hermes config set {GATE_TOOLS_ENV} {_gate_tools_with_shell(environ)} "
        f"and restart Hermes."
    )
