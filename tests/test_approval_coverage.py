import logging
import os
import tempfile
import unittest
from unittest import mock

import pushary_plugin as plugin
from pushary_plugin import approval_coverage, host

SANDBOX_HERMES_HOME = tempfile.TemporaryDirectory(prefix="pushary-hermes-home-")

TERMINAL_ENV_NAMES = (
    "TERMINAL_ENV",
    "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE",
    "TERMINAL_DOCKER_VOLUMES",
    "PUSHARY_GATE_TOOLS",
)


def setUpModule():
    os.environ["HERMES_HOME"] = SANDBOX_HERMES_HOME.name


def tearDownModule():
    os.environ.pop("HERMES_HOME", None)
    SANDBOX_HERMES_HOME.cleanup()


class FakeCtx:
    def register_tool(self, name, toolset, schema, handler, description=""):
        pass

    def register_hook(self, hook_name, callback):
        pass

    def register_skill(self, name, path):
        pass

    def register_approval_transport(self, name, present_fn):
        pass

    def register_system_prompt_section(self, id, content, **kwargs):
        pass


def warning_for(environ=None, terminal=None, gated=frozenset()):
    return approval_coverage.shell_gate_warning(environ or {}, terminal or {}, gated)


class BackendTest(unittest.TestCase):
    def test_the_environment_names_the_backend_hermes_bridged_from_config(self):
        self.assertEqual(approval_coverage.terminal_backend({"TERMINAL_ENV": "docker"}, {"backend": "local"}), "docker")

    def test_config_names_it_when_no_launcher_bridged_it(self):
        self.assertEqual(approval_coverage.terminal_backend({}, {"backend": "modal"}), "modal")

    def test_nothing_named_is_the_local_default(self):
        self.assertEqual(approval_coverage.terminal_backend({}, {}), "local")
        self.assertEqual(approval_coverage.terminal_backend({"TERMINAL_ENV": "  "}, {"backend": ""}), "local")

    def test_a_malformed_backend_is_not_guessed(self):
        self.assertIsNone(approval_coverage.terminal_backend({"TERMINAL_ENV": "Docker"}, {}))
        self.assertIsNone(approval_coverage.terminal_backend({}, {"backend": 3}))
        self.assertIsNone(approval_coverage.terminal_backend({}, {"backend": "docker && rm -rf /"}))


class SkipTest(unittest.TestCase):
    def skips(self, environ=None, terminal=None):
        return approval_coverage.skips_dangerous_command_approvals(environ or {}, terminal or {})

    def test_hermes_asks_on_local_and_ssh(self):
        self.assertFalse(self.skips({"TERMINAL_ENV": "local"}))
        self.assertFalse(self.skips({"TERMINAL_ENV": "ssh"}))

    def test_hermes_skips_on_its_sandbox_backends(self):
        for backend in ("modal", "daytona", "vercel_sandbox"):
            self.assertTrue(self.skips({"TERMINAL_ENV": backend}), backend)

    def test_backends_hermes_does_not_ship_are_not_claimed(self):
        for backend in ("singularity", "sprites"):
            self.assertFalse(self.skips({"TERMINAL_ENV": backend}), backend)

    def test_docker_skips_until_a_host_folder_is_mounted(self):
        self.assertTrue(self.skips({"TERMINAL_ENV": "docker"}))
        self.assertFalse(self.skips({"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE": "True"}))
        self.assertFalse(self.skips({"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_VOLUMES": '["/srv:/srv"]'}))
        self.assertFalse(self.skips({"TERMINAL_ENV": "docker"}, {"docker_volumes": ["C:\\work:/work"]}))
        self.assertFalse(self.skips({"TERMINAL_ENV": "docker"}, {"docker_mount_cwd_to_workspace": True}))

    def test_a_named_volume_or_a_broken_setting_is_no_host_access(self):
        self.assertTrue(self.skips({"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_VOLUMES": '["cache:/cache"]'}))
        self.assertTrue(self.skips({"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_VOLUMES": "not json"}))
        self.assertTrue(self.skips({"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_VOLUMES": '{"a": "/srv"}'}))
        self.assertTrue(self.skips({"TERMINAL_ENV": "docker"}, {"docker_volumes": "/srv:/srv"}))

    def test_an_unknown_or_unreadable_backend_is_not_claimed(self):
        self.assertFalse(self.skips({"TERMINAL_ENV": "bubblewrap"}))
        self.assertFalse(self.skips({"TERMINAL_ENV": "Docker"}))


class WarningTest(unittest.TestCase):
    def test_says_plainly_what_hermes_skips_and_how_to_gate_the_shell(self):
        warning = warning_for({"TERMINAL_ENV": "docker"})
        self.assertIn("Hermes skips its dangerous-command approvals on the docker terminal backend", warning)
        self.assertIn("PUSHARY_GATE_TOOLS", warning)
        self.assertIn("hermes config set PUSHARY_GATE_TOOLS terminal", warning)

    def test_keeps_the_tools_already_listed_in_the_fix(self):
        warning = warning_for({"TERMINAL_ENV": "modal", "PUSHARY_GATE_TOOLS": "write_file"}, gated=frozenset({"write_file"}))
        self.assertIn("hermes config set PUSHARY_GATE_TOOLS write_file,terminal", warning)

    def test_is_silent_once_the_shell_is_gated(self):
        self.assertIsNone(warning_for({"TERMINAL_ENV": "docker"}, gated=frozenset({"terminal"})))

    def test_is_silent_where_hermes_checks_dangerous_commands(self):
        self.assertIsNone(warning_for({"TERMINAL_ENV": "local"}))
        self.assertIsNone(warning_for({"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE": "yes"}))
        self.assertIsNone(warning_for({"TERMINAL_ENV": "bubblewrap"}))

    def test_never_uses_an_em_or_en_dash(self):
        warning = warning_for({"TERMINAL_ENV": "docker"})
        self.assertNotIn("\u2014", warning)
        self.assertNotIn("\u2013", warning)


class LoadWarningTest(unittest.TestCase):
    def setUp(self):
        saved = {name: os.environ.get(name) for name in TERMINAL_ENV_NAMES}

        def restore():
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)
        for name in TERMINAL_ENV_NAMES:
            os.environ.pop(name, None)

    def register(self, config=None):
        with mock.patch.object(host, "_config", return_value=config or {}):
            plugin.register(FakeCtx())

    def test_logs_one_warning_when_hermes_will_skip_the_shell(self):
        os.environ["TERMINAL_ENV"] = "docker"
        with self.assertLogs("pushary_plugin", level=logging.WARNING) as logs:
            self.register()
        warnings = [record for record in logs.records if "dangerous-command approvals" in record.getMessage()]
        self.assertEqual(len(warnings), 1)

    def test_logs_nothing_about_it_on_the_local_backend(self):
        os.environ["TERMINAL_ENV"] = "local"
        with self.assertLogs("pushary_plugin", level=logging.INFO) as logs:
            self.register()
        self.assertFalse(any("dangerous-command approvals" in record.getMessage() for record in logs.records))

    def test_logs_nothing_about_it_once_terminal_is_gated(self):
        os.environ["TERMINAL_ENV"] = "docker"
        os.environ["PUSHARY_GATE_TOOLS"] = "terminal"
        with self.assertLogs("pushary_plugin", level=logging.INFO) as logs:
            self.register()
        self.assertFalse(any("dangerous-command approvals" in record.getMessage() for record in logs.records))

    def test_reads_the_backend_from_hermes_config_when_no_launcher_bridged_it(self):
        with self.assertLogs("pushary_plugin", level=logging.WARNING) as logs:
            self.register({"terminal": {"backend": "daytona"}})
        self.assertTrue(any("daytona terminal backend" in record.getMessage() for record in logs.records))


if __name__ == "__main__":
    unittest.main()
