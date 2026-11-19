import json
import os
import sys
import tempfile
import contextlib
import threading
import time
import types
import unittest
import shutil
import stat
import shlex
import sqlite3
import subprocess
from unittest import mock
from pathlib import Path

from pushary_plugin import history

CLI_HELPERS = Path(__file__).resolve().parents[2] / "agent-hooks" / "bin"
requires_the_cli_helpers = unittest.skipUnless(
    CLI_HELPERS.is_dir(),
    "runs the Pushary CLI's own transcript helpers, which only the Pushary monorepo carries",
)

HELPER_START_ROOM_SECONDS = 120
_run_helper = subprocess.run


def _run_with_room_to_start(*args, **kwargs):
    return _run_helper(*args, **{**kwargs, "timeout": HELPER_START_ROOM_SECONDS})


helpers_have_room_to_start = mock.patch.object(history.subprocess, "run", _run_with_room_to_start)

try:
    from hermes_state import SessionDB
except ImportError:
    SessionDB = None


class _Clock:
    def __init__(self):
        self.now = 1000.0
        self.wall_offset = float(int(time.time())) - self.now

    def monotonic(self):
        return self.now

    def time(self):
        return self.now + self.wall_offset


class _ClockTime:
    def __init__(self, clock):
        self.monotonic = clock.monotonic
        self.time = clock.time


class _Ticks(threading.Event):
    def __init__(self, clock, ticks, between=None):
        super().__init__()
        self.clock = clock
        self.remaining = ticks
        self.between = between or (lambda tick: None)
        self.count = 0

    def wait(self, timeout=None):
        self.clock.now += timeout
        self.count += 1
        self.between(self.count)
        self.remaining -= 1
        if self.remaining <= 0:
            self.set()
        return self.is_set()


def _touch(path, clock):
    stamp = int(clock.now * 1_000_000_000)
    os.utime(path, ns=(stamp, stamp))


def _write_bridge_launcher(root):
    launcher = root / "bin" / "pushary-bridge"
    launcher.write_text('#!/bin/sh\nexec "/Applications/Pushary.app/Contents/Helpers/pushary-bridge" "$@"\n')
    launcher.chmod(0o700)


def _write_cli_helper_record(root, command):
    root.mkdir(parents=True, exist_ok=True)
    record = root / history.CLI_HELPER_RECORD
    record.write_text(json.dumps({"version": history.CLI_HELPER_RECORD_VERSION, "command": command}))
    record.chmod(0o600)
    return record


def _write_authorizing_helper(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\ncat >/dev/null\nprintf \'{"version":1,"authorized":true}\'\n')
    path.chmod(0o700)
    return path


class HistoryTests(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory(prefix="pushary-history-home-")
        self.addCleanup(sandbox.cleanup)
        for patcher in (mock.patch.object(Path, "home", return_value=Path(sandbox.name)),
                        mock.patch.object(history, "SYSTEM_APPLICATIONS", Path(sandbox.name) / "system-applications"),
                        mock.patch.dict(history._authorizations, clear=True), mock.patch.object(history, "_next_prune_at", 0.0),
                        mock.patch.object(history, "_history_not_allowed_reported", False)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run_export(self, directory, clock, ticks, messages, between=None, registrations=None):
        database = Path(directory) / "state.db"
        database.write_text("fixture")
        _touch(database, clock)
        reads = []
        writes = []
        real_write = history.write_snapshot

        def read(path, session_id):
            reads.append(clock.now)
            return messages()

        def write(*args, **kwargs):
            writes.append(clock.now)
            return real_write(*args, **kwargs)

        def register(command, **kwargs):
            if registrations is not None:
                registrations.append(json.loads(kwargs["input"])["hook_event_name"])
            return mock.Mock(returncode=0)

        with mock.patch.object(Path, "home", return_value=Path(directory)), \
                mock.patch.object(history, "time", _ClockTime(clock)), \
                mock.patch.object(history.api, "_get_api_key", return_value="fixture"), \
                mock.patch.object(history, "_authorized_command", return_value=["helper"]), \
                mock.patch.object(history.subprocess, "run", side_effect=register), \
                mock.patch.object(history, "read_display_history", side_effect=read), \
                mock.patch.object(history, "write_snapshot", side_effect=write):
            history._export_loop(database, "session", "fixture", ["helper"], _Ticks(clock, ticks, between))
        snapshots = list((Path(directory) / ".pushary" / "hermes-history").glob("*/session.json"))
        return reads, writes, snapshots

    def test_unchanged_history_is_only_rewritten_to_keep_it_fresh(self):
        clock = _Clock()
        message = {"id": "hermes:1", "role": "user", "timestamp": "2026-10-24T00:00:00Z", "content": []}
        with tempfile.TemporaryDirectory() as directory:
            reads, writes, snapshots = self._run_export(directory, clock, 25, lambda: [message])
            self.assertEqual(writes, [1000.0, 1060.0, 1120.0, 1125.0])
            self.assertEqual(reads, writes)
            self.assertLess(history.SNAPSHOT_REFRESH_SECONDS + history.POLL_SECONDS, 180,
                            "the CLI treats an unfinished Hermes snapshot older than 180 seconds as unavailable")
            saved = json.loads(snapshots[0].read_text())
            self.assertEqual(saved["messages"], [message])
            self.assertTrue(saved["completed"])

    def test_a_source_change_is_read_at_once_and_written_only_when_the_conversation_changed(self):
        clock = _Clock()
        first = {"id": "hermes:1", "role": "user", "timestamp": "2026-10-24T00:00:00Z", "content": []}
        second = {"id": "hermes:2", "role": "assistant", "timestamp": "2026-10-24T00:00:01Z", "content": []}
        conversation = [first]

        def between(tick):
            database = Path(directory) / "state.db"
            if tick == 2:
                _touch(database, clock)
            if tick == 4:
                conversation.append(second)
                wal = Path(directory) / "state.db-wal"
                wal.write_text("frame")
                _touch(wal, clock)

        with tempfile.TemporaryDirectory() as directory:
            reads, writes, snapshots = self._run_export(directory, clock, 6, lambda: list(conversation), between)
            self.assertEqual(reads, [1000.0, 1010.0, 1020.0, 1030.0])
            self.assertEqual(writes, [1000.0, 1020.0, 1030.0])
            self.assertEqual(json.loads(snapshots[0].read_text())["messages"], [first, second])

    def test_an_unreadable_source_is_retried_without_rewriting_the_same_failure(self):
        for failure in (sqlite3.OperationalError("database is locked"), OSError("state.db is unreadable")):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                clock = _Clock()

                def unavailable():
                    raise failure

                reads, writes, snapshots = self._run_export(directory, clock, 3, unavailable)
                self.assertEqual(reads, [1000.0, 1005.0, 1010.0, 1015.0])
                self.assertEqual(writes, [1000.0, 1015.0])
                saved = json.loads(snapshots[0].read_text())
                self.assertEqual(saved["error"], "source_unavailable")
                self.assertFalse(saved["completed"])

    def test_a_source_that_cannot_be_exported_is_read_again_only_when_the_database_changes(self):
        clock = _Clock()

        def over_limit():
            raise ValueError("Hermes history exceeds the bounded reader limit")

        def between(tick):
            if tick == 2:
                _touch(Path(directory) / "state.db", clock)

        with tempfile.TemporaryDirectory() as directory:
            reads, writes, snapshots = self._run_export(directory, clock, 4, over_limit, between)
            self.assertEqual(reads, [1000.0, 1010.0, 1020.0])
            self.assertEqual(writes, [1000.0, 1020.0])
            saved = json.loads(snapshots[0].read_text())
            self.assertEqual(saved["error"], "source_unavailable")
            self.assertFalse(saved["completed"])

    def test_a_conversation_too_large_to_export_leaves_an_unavailable_snapshot_and_the_exporter_keeps_running(self):
        clock = _Clock()
        wide = {"id": "hermes:1", "role": "user", "timestamp": "2026-10-24T00:00:00Z",
                "content": [{"type": "text", "text": "\u6f22" * 800}]}
        self.assertGreater(len(json.dumps(wide, ensure_ascii=False).encode("utf-8")), 2_000)
        registrations = []

        def between(tick):
            if tick == 1:
                _touch(Path(directory) / "state.db", clock)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(history, "MAX_SOURCE_BYTES", 2_000), \
                mock.patch.object(history.logger, "warning") as warning:
            reads, writes, snapshots = self._run_export(directory, clock, 3, lambda: [wide], between, registrations)
            self.assertEqual(reads, [1000.0, 1005.0, 1015.0])
            self.assertEqual(writes, [1000.0, 1000.0, 1005.0, 1015.0, 1015.0],
                             "a failed export is replaced by one unavailable snapshot and not rewritten until a refresh")
            self.assertEqual(registrations, ["SessionStart", "SessionEnd"])
            saved = json.loads(snapshots[0].read_text())
            self.assertEqual((saved["messages"], saved["error"], saved["completed"]), ([], "source_unavailable", False))
            warning.assert_called_once_with("[pushary] Hermes history source is unavailable; existing phone history is preserved")

    def test_a_snapshot_is_refreshed_on_the_first_poll_after_the_wall_clock_jumps(self):
        message = {"id": "hermes:1", "role": "user", "timestamp": "2026-10-24T00:00:00Z", "content": []}
        for jump, reason in ((3600, "the computer slept for an hour"), (-3600, "the clock was set back an hour")):
            with self.subTest(reason), tempfile.TemporaryDirectory() as directory:
                clock = _Clock()
                stamps = []

                def between(tick):
                    if tick == 1:
                        clock.wall_offset += jump
                    if tick == 2:
                        snapshot = next((Path(directory) / ".pushary" / "hermes-history").glob("*/session.json"))
                        stamps.append((json.loads(snapshot.read_text())["updatedAtMs"], clock.time() * 1000))

                reads, writes, snapshots = self._run_export(directory, clock, 3, lambda: [message], between)
                self.assertEqual(writes, [1000.0, 1005.0, 1015.0])
                [(updated_at_ms, now_ms)] = stamps
                self.assertLessEqual(updated_at_ms, now_ms)
                self.assertLessEqual(now_ms - updated_at_ms, history.POLL_SECONDS * 1000,
                                     "the CLI shows Hermes history only from a snapshot under 180 seconds old and not from the future")

    def test_export_authorization_is_reused_until_its_inputs_change_or_it_expires(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(Path, "home", return_value=Path(directory)), \
                mock.patch.object(history, "time", _ClockTime(clock)), \
                mock.patch.object(history.shutil, "which", return_value=None), \
                mock.patch.dict("os.environ", {"PUSHARY_CONFIG_FILE": str(Path(directory) / "config.json")}), \
                mock.patch.object(history, "_registration_command", return_value=["helper"]) as ask:
            self.assertEqual(history._authorized_command("fixture"), ["helper"])
            self.assertEqual(history._authorized_command("fixture"), ["helper"])
            self.assertEqual(ask.call_count, 1)
            consent = Path(directory) / ".pushary" / "native-transcript-consent.json"
            consent.parent.mkdir()
            consent.write_text("{}")
            ask.return_value = None
            self.assertIsNone(history._authorized_command("fixture"), "a consent change is re-checked on the next tick")
            self.assertIsNone(history._authorized_command("fixture"))
            self.assertEqual(ask.call_count, 2)
            ask.return_value = ["helper"]
            (Path(directory) / "config.json").write_text("{}")
            self.assertEqual(history._authorized_command("fixture"), ["helper"])
            self.assertEqual(ask.call_count, 3)
            consent.chmod(stat.S_IMODE(consent.stat().st_mode) ^ stat.S_IRGRP)
            history._authorized_command("fixture")
            self.assertEqual(ask.call_count, 4, "a permission change is re-checked")
            clock.now += history.AUTHORIZATION_LIFETIME_SECONDS
            history._authorized_command("fixture")
            self.assertEqual(ask.call_count, 5)
            history._authorized_command("another-key")
            self.assertEqual(ask.call_count, 6)
            with mock.patch.dict("os.environ", {"PUSHARY_TRANSCRIPTS": "0"}):
                history._authorized_command("fixture")
            self.assertEqual(ask.call_count, 7)

    @requires_the_cli_helpers
    @helpers_have_room_to_start
    def test_a_computer_without_a_cli_account_says_once_per_process_that_history_is_not_saved(self):
        helper_source = CLI_HELPERS / "pushary-transcript-register.ts"
        bun = shutil.which("bun")
        self.assertIsNotNone(bun)
        clock = _Clock()
        with tempfile.TemporaryDirectory() as directory:
            hermes = types.ModuleType("hermes_constants")
            hermes.get_hermes_home = lambda: Path(directory)
            helper = Path(directory) / "pushary-transcript-register"
            helper.write_text("#!/bin/sh\nexec " + shlex.quote(bun) + " " + shlex.quote(str(helper_source)) + ' "$@"\n')
            helper.chmod(0o700)
            environment = {"HOME": directory, "PUSHARY_API_KEY": "profile-key", "PUSHARY_TRANSCRIPTS": "1",
                           "PUSHARY_BASE_URL": history.api.BASE_URL, "PUSHARY_CONFIG_FILE": str(Path(directory) / "config.json")}
            with mock.patch.dict(sys.modules, {"hermes_constants": hermes}), \
                    mock.patch.dict("os.environ", environment), \
                    mock.patch.dict(history._workers, clear=True), \
                    mock.patch.object(history, "time", _ClockTime(clock)), \
                    mock.patch.object(history.shutil, "which", return_value=str(helper)), \
                    mock.patch.object(history.api, "_get_api_key", return_value="profile-key"), \
                    mock.patch.object(history, "_export_loop") as export, \
                    mock.patch.object(history.logger, "warning") as warning:
                for _ in range(3):
                    history.start_history("session")
                    clock.now += history.AUTHORIZATION_LIFETIME_SECONDS
                warning.assert_called_once_with(history.HISTORY_NOT_ALLOWED_WARNING)
                self.assertEqual(history._workers, {})
                Path(environment["PUSHARY_CONFIG_FILE"]).write_text(json.dumps({"apiKey": "profile-key"}))
                history.start_history("session")
                warning.assert_called_once()
                _, thread = history._workers[(str((Path(directory) / "state.db").resolve()), "session")]
                thread.join(timeout=5)
                export.assert_called_once()

    def test_a_prompt_in_a_session_that_is_already_exporting_does_not_ask_the_helper_again(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes = types.ModuleType("hermes_constants")
            hermes.get_hermes_home = lambda: Path(directory)
            running = (str((Path(directory) / "state.db").resolve()), "session")
            with mock.patch.dict(sys.modules, {"hermes_constants": hermes}), \
                    mock.patch.object(Path, "home", return_value=Path(directory)), \
                    mock.patch.dict(history._workers, {running: (threading.Event(), mock.Mock())}), \
                    mock.patch.object(history.api, "_get_api_key", return_value="fixture"), \
                    mock.patch.object(history, "_authorized_command", return_value=["helper"]) as authorize, \
                    mock.patch.object(history, "_registration_command", return_value=["helper"]) as ask:
                history.start_history("session")
                authorize.assert_not_called()
                ask.assert_not_called()

    def test_snapshots_expire_after_the_cli_registration_lifetime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "hermes-history"
            stale = root / "account-a" / "old.json"
            fresh = root / "account-a" / "new.json"
            crashed = root / "account-b" / ".history-crashed"
            outside = Path(directory) / "elsewhere" / "kept.json"
            for path in (stale, fresh, crashed, outside):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
            (root / "linked").symlink_to(outside.parent, target_is_directory=True)
            now = time.time()
            expired = now - history.SNAPSHOT_RETENTION_SECONDS - 1
            for path in (stale, crashed, outside):
                os.utime(path, (expired, expired))
            history._prune_expired_snapshots(root, now)
            self.assertFalse(stale.exists())
            self.assertFalse(crashed.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(outside.exists(), "cleanup never follows a link out of the history folder")
            history._prune_expired_snapshots(Path(directory) / "missing", now)

    def test_session_starts_remove_expired_snapshots_hourly_even_while_history_is_off(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as directory:
            hermes = types.ModuleType("hermes_constants")
            hermes.get_hermes_home = lambda: Path(directory)
            snapshots = Path(directory) / ".pushary" / "hermes-history" / "another-account"
            snapshots.mkdir(parents=True)
            expired = clock.time() - history.SNAPSHOT_RETENTION_SECONDS - 1

            def stale(name):
                path = snapshots / name
                path.write_text("{}")
                os.utime(path, (expired, expired))
                return path

            with mock.patch.dict(sys.modules, {"hermes_constants": hermes}), \
                    mock.patch.object(Path, "home", return_value=Path(directory)), \
                    mock.patch.object(history, "time", _ClockTime(clock)), \
                    mock.patch.object(history.api, "_get_api_key", return_value="fixture"), \
                    mock.patch.object(history, "_authorized_command", return_value=None):
                first = stale("first.json")
                history.start_history("session")
                self.assertFalse(first.exists())
                second = stale("second.json")
                history.start_history("session")
                self.assertTrue(second.exists(), "cleanup runs at most once an hour")
                clock.now += history.SNAPSHOT_PRUNE_INTERVAL_SECONDS
                history.start_history("session")
                self.assertFalse(second.exists())

    def test_the_mac_app_helper_is_found_inside_the_app_behind_the_bridge_launcher(self):
        home = Path.home()
        root = home / ".pushary"
        (root / "bin").mkdir(parents=True)
        _write_bridge_launcher(root)
        (root / "native-transcript-consent.json").write_text("{}")
        system_app = history.SYSTEM_APPLICATIONS / history.PUSHARY_APP_NAME
        user_app = home / "Applications" / history.PUSHARY_APP_NAME
        cached_app = home / "Elsewhere" / history.PUSHARY_APP_NAME
        (root / "bin" / ".bridge-cache").write_text(f"{cached_app}\n")
        self.assertIsNone(history._registration_command("fixture"), "no installed helper means no export")
        for app in (cached_app, user_app, system_app):
            helper = _write_authorizing_helper(app / history.PUSHARY_APP_HELPER)
            self.assertEqual(history._registration_command("fixture"), [str(helper), "register", "hermes"])
        sibling = _write_authorizing_helper(root / "bin" / history.NATIVE_HELPER_NAME)
        self.assertEqual(history._registration_command("fixture"), [str(sibling.resolve()), "register", "hermes"])

    def test_a_relative_app_path_in_the_bridge_cache_is_ignored(self):
        root = Path.home() / ".pushary"
        (root / "bin").mkdir(parents=True)
        (root / "bin" / ".bridge-cache").write_text(f"Relative/{history.PUSHARY_APP_NAME}\n")
        self.assertIsNone(history._cached_pushary_app(root))
        (root / "bin" / ".bridge-cache").write_text(f"{Path.home() / history.PUSHARY_APP_NAME}\n")
        self.assertEqual(history._cached_pushary_app(root), Path.home() / history.PUSHARY_APP_NAME)

    def test_a_helper_that_cannot_run_or_refuses_the_key_never_exports(self):
        root = Path.home() / ".pushary"
        (root / "bin").mkdir(parents=True)
        _write_bridge_launcher(root)
        (root / "native-transcript-consent.json").write_text("{}")
        helper = _write_authorizing_helper(history.SYSTEM_APPLICATIONS / history.PUSHARY_APP_NAME / history.PUSHARY_APP_HELPER)
        helper.chmod(0o600)
        self.assertIsNone(history._registration_command("fixture"))
        helper.write_text('#!/bin/sh\ncat >/dev/null\nprintf \'{"version":1,"authorized":false}\'\n')
        helper.chmod(0o700)
        self.assertIsNone(history._registration_command("fixture"))
        (root / "bin" / "pushary-bridge").unlink()
        self.assertIsNone(history._registration_command("fixture"), "the Mac app path needs its bridge launcher")

    def test_a_hermes_without_its_pushary_key_starts_no_history_and_logs_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes = types.ModuleType("hermes_constants")
            hermes.get_hermes_home = lambda: Path(directory)
            missing = ValueError("PUSHARY_API_KEY is not set")
            with mock.patch.dict(sys.modules, {"hermes_constants": hermes}), \
                    mock.patch.dict(history._workers, clear=True), \
                    mock.patch.object(history.api, "_get_api_key", side_effect=missing), \
                    mock.patch.object(history, "_registration_command") as ask, \
                    mock.patch.object(history.logger, "warning") as warning:
                history.start_history("session")
                self.assertEqual(history._workers, {})
                ask.assert_not_called()
                warning.assert_not_called()

    def test_native_optout_and_account_switch_prevent_even_local_export(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(Path, "home", return_value=Path(directory)):
            root = Path(directory) / ".pushary"
            (root / "bridge-auth").mkdir(parents=True)
            account = {"key": "fixture", "siteID": "site", "machineID": "machine", "baseURL": history.api.BASE_URL}
            (root / "bridge-auth" / "agent-key.json").write_text(json.dumps(account))
            consent = root / "native-transcript-consent.json"
            consent.write_text(json.dumps({**account, "enabled": False}))
            self.assertIsNone(history._registration_command("fixture"))
            consent.write_text(json.dumps({**account, "enabled": True, "siteID": "another-site"}))
            self.assertIsNone(history._registration_command("fixture"))
            consent.write_text(json.dumps({**account, "enabled": True}))
            self.assertIsNone(history._registration_command("another-key"))

    @requires_the_cli_helpers
    @helpers_have_room_to_start
    def test_export_authorization_uses_the_real_native_grant_contract(self):
        helper_source = CLI_HELPERS / "pushary-native-transcripts.ts"
        bun = shutil.which("bun")
        self.assertIsNotNone(bun)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(Path, "home", return_value=Path(directory)):
            root = Path(directory) / ".pushary"
            (root / "bridge-auth").mkdir(parents=True, mode=0o700)
            (root / "bin").mkdir()
            _write_bridge_launcher(root)
            helper = Path(directory) / "Applications" / history.PUSHARY_APP_NAME / history.PUSHARY_APP_HELPER
            helper.parent.mkdir(parents=True)
            helper.write_text("#!/bin/sh\nexec " + shlex.quote(bun) + " " + shlex.quote(str(helper_source)) + ' "$@"\n')
            helper.chmod(0o700)
            account = {"key": "fixture", "keyScope": "agent", "siteID": "site", "machineID": "machine", "baseURL": history.api.BASE_URL}
            account_path = root / "bridge-auth/agent-key.json"
            account_path.write_text(json.dumps(account))
            account_path.chmod(0o600)
            (root / "machine-id").write_text("machine")
            consent = root / "native-transcript-consent.json"
            with mock.patch.dict("os.environ", {"HOME": directory, "CFFIXED_USER_HOME": directory}):
                for grant in [{**account, "enabled": True}, {**account, "enabled": True, "grantedAtMs": 0}]:
                    consent.write_text(json.dumps(grant))
                    consent.chmod(0o600)
                    self.assertIsNone(history._registration_command("fixture"), "invalid grants must prevent even local export")
                consent.write_text(json.dumps({**account, "enabled": True, "grantedAtMs": 1792670400000}))
                self.assertIsNotNone(history._registration_command("fixture"))
                self.assertIsNone(history._registration_command("another-key"))
                consent.chmod(0o644)
                self.assertIsNone(history._registration_command("fixture"))
                consent.chmod(0o600)
                (root / "machine-id").write_text("another-machine")
                self.assertIsNone(history._registration_command("fixture"))

    @requires_the_cli_helpers
    @helpers_have_room_to_start
    def test_cli_export_requires_the_cli_account_server_and_preferences(self):
        helper_source = CLI_HELPERS / "pushary-transcript-register.ts"
        bun = shutil.which("bun")
        self.assertIsNotNone(bun)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(Path, "home", return_value=Path(directory)):
            helper = Path(directory) / "pushary-transcript-register"
            helper.write_text("#!/bin/sh\nexec " + shlex.quote(bun) + " " + shlex.quote(str(helper_source)) + ' "$@"\n')
            helper.chmod(0o700)
            config = Path(directory) / ".pushary" / "config.json"
            config.parent.mkdir()
            environment = {"HOME": directory, "PUSHARY_API_KEY": "another-profile", "PUSHARY_TRANSCRIPTS": "1",
                           "PUSHARY_BASE_URL": history.api.BASE_URL}
            with mock.patch.dict("os.environ", environment), \
                    mock.patch.object(history.shutil, "which", return_value=str(helper)):
                os.environ.pop("PUSHARY_CONFIG_FILE", None)
                self.assertIsNone(history._registration_command("profile-key"), "a computer without a CLI account cannot authorize")
                config.write_text(json.dumps({"apiKey": "another-account"}))
                self.assertIsNone(history._registration_command("profile-key"), "the Hermes key must be the CLI's account")
                config.write_text(json.dumps({"apiKey": "profile-key"}))
                self.assertIsNotNone(history._registration_command("profile-key"))
                with mock.patch.object(history.api, "BASE_URL", "https://staging.example"):
                    self.assertIsNone(history._registration_command("profile-key"), "the Hermes server must be the CLI's server")
                with mock.patch.dict("os.environ", {"PUSHARY_TRANSCRIPTS": "0"}):
                    self.assertIsNone(history._registration_command("profile-key"))
                for reply in ["", "{}", '{"version": 1, "authorized": false}', '{"version": 2, "authorized": true}']:
                    helper.write_text("#!/bin/sh\nprintf '%s' " + shlex.quote(reply) + "\n")
                    self.assertIsNone(history._registration_command("profile-key"))

    @requires_the_cli_helpers
    @helpers_have_room_to_start
    @unittest.skipIf(os.name == "nt", "Windows uses PATH for the CLI helper")
    def test_the_recorded_cli_helper_runs_the_real_contract_without_path(self):
        helper_source = CLI_HELPERS / "pushary-transcript-register.ts"
        bun = shutil.which("bun")
        self.assertIsNotNone(bun)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(Path, "home", return_value=Path(directory)):
            config = Path(directory) / ".pushary" / "config.json"
            config.parent.mkdir()
            config.write_text(json.dumps({"apiKey": "profile-key"}))
            _write_cli_helper_record(Path(directory) / ".pushary", [bun, str(helper_source)])
            environment = {"HOME": directory, "PUSHARY_TRANSCRIPTS": "1", "PUSHARY_BASE_URL": history.api.BASE_URL, "PATH": "/nonexistent"}
            with mock.patch.dict("os.environ", environment), \
                    mock.patch.object(history.shutil, "which", return_value=None):
                os.environ.pop("PUSHARY_CONFIG_FILE", None)
                self.assertEqual(history._registration_command("profile-key"), [bun, str(helper_source), "hermes"])
                self.assertIsNone(history._registration_command("another-key"), "the Hermes key must be the CLI's account")

    @unittest.skipUnless(os.name == "nt", "requires a native Windows filesystem")
    def test_windows_uses_the_real_absolute_path_entry_for_the_cli_helper(self):
        with tempfile.TemporaryDirectory(prefix="Hermes CLI with spaces ") as directory:
            root = Path(directory)
            executable = root / (history.CLI_HELPER_NAME + ".CMD")
            executable.touch()
            with mock.patch.dict(os.environ, {"PATH": "." + os.pathsep + directory, "PATHEXT": ".EXE;.CMD"}):
                self.assertEqual(history._cli_helper(root), [str(executable)])
                executable.unlink()
                self.assertIsNone(history._cli_helper(root))

    def test_windows_finds_the_helper_on_path_without_opening_the_saved_record(self):
        root = Path.home() / ".pushary"
        bin_dir = r"C:\Program Files\npm"
        helper = bin_dir + "\\" + history.CLI_HELPER_NAME + ".CMD"
        with mock.patch.object(history.os, "name", "nt"), \
             mock.patch.object(history, "_owner_only_record_text", return_value=None) as read_record, \
             mock.patch.dict(os.environ, {"PATHEXT": ".EXE;.CMD"}), \
             mock.patch.object(os, "get_exec_path", return_value=[".", "", r"\drive-relative", bin_dir]), \
             mock.patch.object(os.path, "isfile", side_effect=lambda path: path == helper) as exists:
            self.assertEqual(history._cli_helper(root), [helper])
            self.assertTrue(all(check.args[0].startswith(bin_dir) for check in exists.call_args_list))
            exists.side_effect = lambda path: False
            self.assertIsNone(history._cli_helper(root))
            read_record.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Windows uses PATH for the CLI helper")
    def test_the_recorded_cli_helper_comes_before_path(self):
        root = Path.home() / ".pushary"
        runtime = _write_authorizing_helper(Path.home() / "runtime" / "node")
        entry = Path.home() / "package" / "pushary-transcript-register.js"
        entry.parent.mkdir(parents=True)
        entry.write_text("")
        on_path = _write_authorizing_helper(Path.home() / "path-bin" / history.CLI_HELPER_NAME)
        with mock.patch.object(history.shutil, "which", return_value=str(on_path)):
            self.assertEqual(history._registration_command("fixture"), [str(on_path), "hermes"], "without a record PATH decides")
            _write_cli_helper_record(root, [str(runtime), str(entry)])
            self.assertEqual(history._registration_command("fixture"), [str(runtime), str(entry), "hermes"])

    @unittest.skipIf(os.name == "nt", "Windows ignores saved helper records")
    def test_a_record_that_others_could_change_or_that_points_nowhere_is_ignored(self):
        root = Path.home() / ".pushary"
        runtime = _write_authorizing_helper(Path.home() / "runtime" / "node")
        entry = Path.home() / "package" / "pushary-transcript-register.js"
        entry.parent.mkdir(parents=True)
        entry.write_text("")
        good = [str(runtime), str(entry)]
        record = root / history.CLI_HELPER_RECORD
        with mock.patch.object(history.shutil, "which", return_value=None):
            _write_cli_helper_record(root, good)
            self.assertIsNotNone(history._recorded_cli_helper(root))
            record.chmod(0o644)
            self.assertIsNone(history._recorded_cli_helper(root), "readable or writable by others")
            for command in (["node", str(entry)], [str(runtime)], [str(runtime), str(entry), "extra"],
                            [str(runtime), str(Path.home() / "missing.js")], [str(entry), str(entry)]):
                _write_cli_helper_record(root, command)
                self.assertIsNone(history._recorded_cli_helper(root), command)
            record.write_text(json.dumps({"version": 2, "command": good}))
            self.assertIsNone(history._recorded_cli_helper(root))
            record.write_text("not json")
            self.assertIsNone(history._recorded_cli_helper(root))
            record.unlink()
            elsewhere = Path.home() / "elsewhere.json"
            elsewhere.write_text(json.dumps({"version": 1, "command": good}))
            elsewhere.chmod(0o600)
            record.symlink_to(elsewhere)
            self.assertIsNone(history._recorded_cli_helper(root), "a linked record")
            self.assertIsNone(history._registration_command("fixture"))

    @unittest.skipIf(os.name == "nt", "ownership and FIFOs are POSIX")
    def test_a_record_owned_by_someone_else_oversized_or_not_a_file_is_ignored(self):
        root = Path.home() / ".pushary"
        runtime = _write_authorizing_helper(Path.home() / "runtime" / "node")
        entry = Path.home() / "package" / "pushary-transcript-register.js"
        entry.parent.mkdir(parents=True)
        entry.write_text("")
        record = _write_cli_helper_record(root, [str(runtime), str(entry)])
        self.assertIsNotNone(history._recorded_cli_helper(root))
        with mock.patch.object(history.os, "getuid", return_value=os.getuid() + 1):
            self.assertIsNone(history._recorded_cli_helper(root), "another user's record")
        record.write_text(json.dumps({"version": 1, "command": [str(runtime), str(entry)], "padding": "x" * history.CLI_HELPER_RECORD_MAX_BYTES}))
        self.assertIsNone(history._recorded_cli_helper(root), "larger than any record setup writes")
        record.write_text("[" * (history.CLI_HELPER_RECORD_MAX_BYTES - 1))
        self.assertIsNone(history._recorded_cli_helper(root), "nesting deep enough to exhaust the parser")
        record.unlink()
        os.mkfifo(record, 0o600)
        self.assertIsNone(history._recorded_cli_helper(root), "a FIFO must not block the gateway")

    @unittest.skipIf(os.name == "nt", "links and FIFOs are POSIX")
    def test_a_record_swapped_after_it_was_checked_is_refused_when_opened(self):
        root = Path.home() / ".pushary"
        runtime = _write_authorizing_helper(Path.home() / "runtime" / "node")
        entry = Path.home() / "package" / "pushary-transcript-register.js"
        entry.parent.mkdir(parents=True)
        entry.write_text("")
        elsewhere = _write_cli_helper_record(Path.home() / "elsewhere", [str(runtime), str(entry)])
        record = root / history.CLI_HELPER_RECORD
        root.mkdir(parents=True)
        regular_file = os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, os.getuid(), os.getgid(), 64, 0, 0, 0))
        with mock.patch.object(Path, "lstat", return_value=regular_file):
            record.symlink_to(elsewhere)
            self.assertIsNone(history._recorded_cli_helper(root), "a link swapped in after the check")
            record.unlink()
            os.mkfifo(record, 0o600)
            results = []
            reader = threading.Thread(target=lambda: results.append(history._recorded_cli_helper(root)), daemon=True)
            reader.start()
            reader.join(2.0)
            waited_for_a_writer = reader.is_alive()
            if waited_for_a_writer:
                os.close(os.open(record, os.O_WRONLY | os.O_NONBLOCK))
                reader.join(2.0)
            self.assertFalse(waited_for_a_writer, "opening a FIFO swapped in after the check must not wait for a writer")
            self.assertEqual(results, [None])

    @helpers_have_room_to_start
    def test_the_history_helper_receives_only_the_environment_it_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / "helper-calls.jsonl"
            helper = Path(directory) / "pushary-transcript-register"
            helper.write_text("\n".join([
                "#!" + sys.executable,
                "import json, os, sys",
                "received = sys.stdin.read()",
                f"with open({str(calls)!r}, 'a') as calls:",
                "    calls.write(json.dumps({'argv': sys.argv[1:], 'environment': dict(os.environ), 'input': received}) + '\\n')",
                "if sys.argv[1:] == ['authorize-export']:",
                "    print(json.dumps({'version': 1, 'authorized': True}))",
            ]) + "\n")
            helper.chmod(0o700)
            inherited = {"HOME": directory, "PATH": os.environ.get("PATH", os.defpath), "PUSHARY_TRANSCRIPTS": "1",
                         "PUSHARY_CONFIG_FILE": str(Path(directory) / "config.json"), "PUSHARY_API_KEY": "shell-key",
                         "OPENROUTER_API_KEY": "provider-secret", "ANTHROPIC_API_KEY": "provider-secret"}
            stop = threading.Event()
            stop.set()
            with mock.patch.object(Path, "home", return_value=Path(directory)), \
                    mock.patch.dict("os.environ", inherited, clear=True), \
                    mock.patch.object(history.shutil, "which", return_value=str(helper)):
                command = history._registration_command("profile-key")
                self.assertEqual(command, [str(helper), "hermes"],
                                 f"helper calls recorded: {calls.read_text() if calls.exists() else 'none, so the helper never ran'}")
                with mock.patch.object(history.api, "_get_api_key", return_value="profile-key"), \
                        mock.patch.object(history, "_authorized_command", return_value=command), \
                        mock.patch.object(history, "read_display_history", return_value=[]):
                    history._export_loop(Path(directory) / "state.db", "session", "profile-key", command, stop)
            authorization, registration = [json.loads(line) for line in calls.read_text().splitlines()]
            needed = {"HOME": directory, "PATH": inherited["PATH"], "PUSHARY_TRANSCRIPTS": "1",
                      "PUSHARY_CONFIG_FILE": inherited["PUSHARY_CONFIG_FILE"]}
            withheld = {"OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"}
            self.assertEqual(authorization["argv"], ["authorize-export"])
            self.assertEqual(json.loads(authorization["input"])["apiKey"], "profile-key")
            self.assertLessEqual(needed.items(), authorization["environment"].items())
            self.assertFalse((withheld | {"PUSHARY_API_KEY"}) & authorization["environment"].keys(),
                             "the key reaches the helper on stdin, never through its environment")
            self.assertEqual(registration["argv"], ["hermes"])
            self.assertLessEqual({**needed, "PUSHARY_API_KEY": "profile-key", "PUSHARY_BASE_URL": history.api.BASE_URL}.items(),
                                 registration["environment"].items())
            self.assertFalse(withheld & registration["environment"].keys())

    def test_revocation_stops_before_reading_or_exporting_local_history(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(Path, "home", return_value=Path(directory)), \
                mock.patch.object(history.api, "_get_api_key", return_value="fixture"), \
                mock.patch.object(history, "_registration_command", return_value=None), \
                mock.patch.object(history, "read_display_history") as read, \
                mock.patch.object(history, "write_snapshot") as write:
            history._export_loop(Path(directory) / "state.db", "session", "fixture", ["helper"], history.threading.Event())
            read.assert_not_called()
            write.assert_not_called()

    @unittest.skipIf(SessionDB is None, "requires the upstream Hermes runtime")
    def test_actual_read_only_database_projection_follows_rewind_and_compaction(self):
        with contextlib.ExitStack() as setup, tempfile.TemporaryDirectory() as directory:
            setup.enter_context(mock.patch("hermes_cli.venv_sync.prepare_launch", return_value=None))
            setup.enter_context(mock.patch("hermes_cli._early_recovery.recover_if_needed", return_value=False))
            setup.enter_context(mock.patch("pm.environments.activate_dependencies", return_value=None))
            path = Path(directory) / "state.db"
            database = SessionDB(db_path=path)
            try:
                database.create_session("fixture", "cli")
                first = database.append_message("fixture", "user", "First", timestamp=100)
                second = database.append_message("fixture", "user", "Second", timestamp=101)
                database._conn.execute("UPDATE messages SET active=0, compacted=1 WHERE id=?", (first,))
                before = history.read_display_history(path, "fixture")
                self.assertEqual([message["id"] for message in before], [f"hermes:{first}", f"hermes:{second}"])
                database.rewind_to_message("fixture", second)
                after = history.read_display_history(path, "fixture")
                self.assertEqual([message["id"] for message in after], [f"hermes:{first}"])
                with mock.patch.object(history, "MAX_DATABASE_BYTES", 1):
                    with self.assertRaises(ValueError):
                        history.read_display_history(path, "fixture")
                with self.assertRaises(ValueError):
                    history.read_display_history(path, "wrong-session")
            finally:
                database.close()

    @unittest.skipIf(SessionDB is None, "requires the upstream Hermes runtime")
    def test_long_display_history_ignores_tool_payloads_and_refuses_child_sources(self):
        with contextlib.ExitStack() as setup, tempfile.TemporaryDirectory() as directory:
            setup.enter_context(mock.patch("hermes_cli.venv_sync.prepare_launch", return_value=None))
            setup.enter_context(mock.patch("hermes_cli._early_recovery.recover_if_needed", return_value=False))
            setup.enter_context(mock.patch("pm.environments.activate_dependencies", return_value=None))
            path = Path(directory) / "state.db"
            database = SessionDB(db_path=path)
            try:
                database.create_session("root", "cli")
                database.create_session("child", "subagent", parent_session_id="root")
                database.append_message("child", "user", "delegated prompt", timestamp=1)
                expected = [f"turn {index} 🌱" for index in range(240)]
                for index, text in enumerate(expected):
                    database.append_message("root", "assistant" if index % 2 else "user", text, timestamp=500-index)
                database.append_message("root", "tool", "x" * 400_000, timestamp=501)
                with mock.patch.object(history, "MAX_SOURCE_BYTES", 300_000):
                    turns = history.read_display_history(path, "root")
                    history.write_snapshot(Path(directory) / "root.json", "root", turns)
                self.assertEqual([turn["content"][0]["text"] for turn in turns], expected)
                with self.assertRaises(ValueError):
                    history.read_display_history(path, "child")
            finally:
                database.close()

    @unittest.skipIf(SessionDB is None, "requires the upstream Hermes display and redaction helpers")
    def test_only_visible_text_is_exported(self):
        rows = [
            {"id": 1, "role": "user", "content": "Hello Bearer synthetic-test-credential-1234567890", "timestamp": 1792670400, "active": 1},
            {"id": 2, "role": "assistant", "content": "<think>private</think>Done", "timestamp": 1792670401, "compacted": 1,
             "reasoning_content": "Never export", "tool_calls": [{"id": "t", "function": {"name": "terminal", "arguments": '{"command":"pwd"}'}}]},
            {"id": 3, "role": "tool", "content": '{"output":"/work","exit_code":0}', "timestamp": 1792670402, "active": 1, "tool_call_id": "t"},
            {"id": 4, "role": "assistant", "content": "Rewound", "timestamp": 1792670403, "active": 0, "compacted": 0},
            {"id": 5, "role": "assistant", "content": "Hidden", "timestamp": 1792670404, "active": 1, "display_kind": "hidden"},
            {"id": 6, "role": "system", "content": "System", "timestamp": 1792670405, "active": 1},
        ]
        result = history.normalize_messages(rows)
        self.assertEqual([message["id"] for message in result], ["hermes:1", "hermes:2"])
        self.assertEqual(result[1]["content"], [{"type": "text", "text": "Done", "visibility": "hermes-visible-text-v1"}])
        self.assertNotIn("Never export", json.dumps(result))
        self.assertNotIn("synthetic-test-credential", json.dumps(result))
        self.assertTrue(result[0]["timestamp"].endswith("Z"))

    @unittest.skipIf(SessionDB is None, "requires the upstream Hermes display and redaction helpers")
    def test_plain_json_looking_text_is_not_reinterpreted(self):
        text = '{"text":"literal JSON"}'
        result = history.normalize_messages([{"id": 1, "role": "user", "content": text, "timestamp": 1, "active": 1}])
        self.assertEqual(result[0]["content"], [{"type": "text", "text": text, "visibility": "hermes-visible-text-v1"}])

    @unittest.skipIf(SessionDB is None, "requires the upstream Hermes display helpers")
    def test_authored_words_survive_steering_and_compaction(self):
        from agent.context_compressor import SUMMARY_PREFIX, HISTORICAL_TASK_HEADING, _SUMMARY_END_MARKER
        from agent.prompt_builder import format_steer_marker
        handoff = f"{SUMMARY_PREFIX}\n\n{HISTORICAL_TASK_HEADING}\nprivate old work\n\n{_SUMMARY_END_MARKER}"
        base = {"role": "user", "active": 1, "timestamp": 1}
        rows = [
            {**base, "id": 1, "display_kind": "steer", "content": format_steer_marker("stop here")},
            {**base, "id": 2, "display_kind": "steer", "content": "plain correction"},
            {**base, "id": 3, "display_kind": "hidden", "content": handoff + "\n\nshow the result"},
            {**base, "id": 4, "content": handoff},
            {**base, "id": 5, "role": "tool", "display_kind": "steer", "content": "private tool result"},
        ]
        result = history.normalize_messages(rows)
        self.assertEqual([row["content"][0]["text"] for row in result], ["stop here", "plain correction", "show the result"])

    def test_invalid_timestamp_or_id_cannot_become_a_complete_snapshot(self):
        for row in [{"id": 1, "timestamp": float("nan")}, {"id": None, "timestamp": 1}]:
            with self.assertRaises(ValueError):
                history.normalize_messages([{**row, "role": "user", "content": "text", "active": 1}])

    def test_export_is_owner_only_and_binds_the_provider_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            history.write_snapshot(path, "session", [])
            saved = json.loads(path.read_text())
            self.assertEqual(saved["sessionId"], "session")
            self.assertEqual(saved["version"], 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
