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
import shlex
import sqlite3
from unittest import mock
from pathlib import Path

from pushary_plugin import history

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


class HistoryTests(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory(prefix="pushary-history-home-")
        self.addCleanup(sandbox.cleanup)
        for patcher in (mock.patch.object(Path, "home", return_value=Path(sandbox.name)),
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
                "content": [{"type": "text", "text": "\u6f22" * 400}]}
        self.assertLess(len(json.dumps(wide, ensure_ascii=False).encode("utf-8")), 2_000)
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
            consent.chmod(0o644)
            history._authorized_command("fixture")
            self.assertEqual(ask.call_count, 4)
            clock.now += history.AUTHORIZATION_LIFETIME_SECONDS
            history._authorized_command("fixture")
            self.assertEqual(ask.call_count, 5)
            history._authorized_command("another-key")
            self.assertEqual(ask.call_count, 6)
            with mock.patch.dict("os.environ", {"PUSHARY_TRANSCRIPTS": "0"}):
                history._authorized_command("fixture")
            self.assertEqual(ask.call_count, 7)

    def test_a_computer_without_a_cli_account_says_once_per_process_that_history_is_not_saved(self):
        helper_source = Path(__file__).resolve().parents[2] / "agent-hooks/bin/pushary-transcript-register.ts"
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
            expired = time.time() - history.SNAPSHOT_RETENTION_SECONDS - 1

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

    def test_export_authorization_uses_the_real_native_grant_contract(self):
        helper_source = Path(__file__).resolve().parents[2] / "agent-hooks/bin/pushary-native-transcripts.ts"
        bun = shutil.which("bun")
        self.assertIsNotNone(bun)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(Path, "home", return_value=Path(directory)):
            root = Path(directory) / ".pushary"
            (root / "bridge-auth").mkdir(parents=True, mode=0o700)
            (root / "bin").mkdir()
            (root / "bin/pushary-bridge").write_text("")
            helper = root / "bin/pushary-native-transcripts"
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

    def test_cli_export_requires_the_cli_account_server_and_preferences(self):
        helper_source = Path(__file__).resolve().parents[2] / "agent-hooks/bin/pushary-transcript-register.ts"
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
                with mock.patch.object(history, "MAX_SOURCE_BYTES", 1):
                    with self.assertRaises(ValueError):
                        history.read_display_history(path, "fixture")
                with self.assertRaises(ValueError):
                    history.read_display_history(path, "wrong-session")
            finally:
                database.close()

    def test_only_display_history_and_tool_evidence_are_exported(self):
        rows = [
            {"id": 1, "role": "user", "content": "Hello", "timestamp": 1792670400, "active": 1},
            {"id": 2, "role": "assistant", "content": "Done", "timestamp": 1792670401, "compacted": 1,
             "reasoning_content": "Never export", "tool_calls": [{"id": "t", "function": {"name": "terminal", "arguments": '{"command":"pwd"}'}}]},
            {"id": 3, "role": "tool", "content": '{"output":"/work","exit_code":0}', "timestamp": 1792670402, "active": 1, "tool_call_id": "t"},
            {"id": 4, "role": "assistant", "content": "Rewound", "timestamp": 1792670403, "active": 0, "compacted": 0},
            {"id": 5, "role": "assistant", "content": "Hidden", "timestamp": 1792670404, "active": 1, "display_kind": "hidden"},
            {"id": 6, "role": "system", "content": "System", "timestamp": 1792670405, "active": 1},
        ]
        result = history.normalize_messages(rows)
        self.assertEqual([message["id"] for message in result], ["hermes:1", "hermes:2", "hermes:3"])
        self.assertEqual(result[1]["content"][1]["name"], "terminal")
        self.assertEqual(result[2]["content"][0]["tool_use_id"], "t")
        self.assertNotIn("Never export", json.dumps(result))
        self.assertTrue(result[0]["timestamp"].endswith("Z"))

    def test_plain_json_looking_text_is_not_reinterpreted(self):
        text = '{"text":"literal JSON"}'
        result = history.normalize_messages([{"id": 1, "role": "user", "content": text, "timestamp": 1, "active": 1}])
        self.assertEqual(result[0]["content"], [{"type": "text", "text": text}])

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
