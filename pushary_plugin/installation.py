import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import host

logger = logging.getLogger(__name__)

PLUGIN_DIRECTORY = Path(__file__).parent
MANIFEST_FILE = "plugin.yaml"
INSTALL_MARKER_FILE = ".pushary-install.json"
INSTALL_MARKER_OWNER = "pushary"
HERMES_PLUGINS_FOLDER = "plugins"
PLUGIN_DATA_NAMESPACE = "pushary"
LOAD_RECORDS_DIRECTORY = "loaded"
LOAD_RECORD_RETENTION_SECONDS = 7 * 24 * 60 * 60


def plugin_version(directory=None):
    try:
        manifest = ((directory or PLUGIN_DIRECTORY) / MANIFEST_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in manifest.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "version":
            return value.strip().strip("'\"") or None
    return None


LOADED_PLUGIN_VERSION = plugin_version()
LOADED_HERMES_VERSION = host.hermes_version_label()


def install_marker(directory=None):
    try:
        marker = json.loads(((directory or PLUGIN_DIRECTORY) / INSTALL_MARKER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(marker, dict) or marker.get("owner") != INSTALL_MARKER_OWNER:
        return None
    return marker


def follows_catalog_rules(directory=None):
    folder = directory or PLUGIN_DIRECTORY
    return folder.parent.name == HERMES_PLUGINS_FOLDER and install_marker(folder) is None


def routes_questions(directory=None):
    marker = install_marker(directory)
    return bool(marker and marker.get("routeQuestions") is True)


def hermes_home():
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        configured = os.environ.get("HERMES_HOME")
        return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def load_records_directory(home=None):
    return (home or hermes_home()) / "plugin-data" / PLUGIN_DATA_NAMESPACE / LOAD_RECORDS_DIRECTORY


def load_record(approval_transport_registered, pid, loaded_at):
    return {
        "pluginVersion": LOADED_PLUGIN_VERSION,
        "hermesVersion": LOADED_HERMES_VERSION,
        "pid": pid,
        "approvalTransport": bool(approval_transport_registered),
        "loadedAt": loaded_at,
    }


def _process_is_running(pid):
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _record_belongs_to_running_process(record):
    return record.stem.isdigit() and _process_is_running(int(record.stem))


def _prune_expired_load_records(directory, keep, now):
    for record in directory.glob("*.json"):
        if record.name == keep:
            continue
        try:
            expired = now - record.stat().st_mtime > LOAD_RECORD_RETENTION_SECONDS
            if expired and not _record_belongs_to_running_process(record):
                record.unlink()
        except OSError:
            continue


def record_plugin_load(approval_transport_registered, home=None, pid=None, now=None):
    process_id = os.getpid() if pid is None else pid
    moment = time.time() if now is None else now
    directory = load_records_directory(home)
    record_name = f"{process_id}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        loaded_at = datetime.fromtimestamp(moment, timezone.utc).isoformat()
        staged = directory / f".{record_name}.tmp"
        staged.write_text(
            json.dumps(load_record(approval_transport_registered, process_id, loaded_at)),
            encoding="utf-8",
        )
        os.replace(staged, directory / record_name)
        _prune_expired_load_records(directory, record_name, moment)
        return directory / record_name
    except OSError as exc:
        logger.debug("[pushary] could not record the plugin load: %s", exc)
        return None
