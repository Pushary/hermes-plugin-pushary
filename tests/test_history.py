import json
import tempfile
import contextlib
import unittest
import shutil
import shlex
from unittest import mock
from pathlib import Path

from pushary_plugin import history

try:
    from hermes_state import SessionDB
except ImportError:
    SessionDB = None


class HistoryTests(unittest.TestCase):
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

    def test_cli_export_uses_the_profile_key_and_canonical_preferences(self):
        helper_source = Path(__file__).resolve().parents[2] / "agent-hooks/bin/pushary-transcript-register.ts"
        bun = shutil.which("bun")
        self.assertIsNotNone(bun)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(Path, "home", return_value=Path(directory)):
            helper = Path(directory) / "pushary-transcript-register"
            helper.write_text("#!/bin/sh\nexec " + shlex.quote(bun) + " " + shlex.quote(str(helper_source)) + ' "$@"\n')
            helper.chmod(0o700)
            with mock.patch.dict("os.environ", {"HOME": directory, "PUSHARY_API_KEY": "another-profile", "PUSHARY_TRANSCRIPTS": "1"}), \
                    mock.patch.object(history.shutil, "which", return_value=str(helper)):
                self.assertIsNotNone(history._registration_command("profile-key"))
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
