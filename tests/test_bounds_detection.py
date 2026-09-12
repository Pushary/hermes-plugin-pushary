import sys
import types
import unittest
from unittest import mock

from pushary_plugin import host


class BoundsDetectionTest(unittest.TestCase):
    """How the plugin decides whether Hermes will cut its hook off.

    Every other test of this lives in test_hermes_contract.py and is skipped
    unless Hermes is importable, which it is not in CI and was not on the machine
    where the detection was written. So the branch that matters most had never
    run anywhere. These stub the host instead, and run everywhere.

    What matters is which way it fails. Concluding "bounded" when the host is
    not costs the user a few seconds of window. Concluding "unbounded" when the
    host is bounded spends 55 seconds against a 30 second budget, and Hermes
    abandons the callback and fails it closed, so the answer the user already
    gave on their phone is thrown away.
    """

    FAIL_CLOSED = "_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS"

    def _hermes(self, **attrs):
        """A stub hermes_cli.plugins, installed for the duration of a test."""
        pkg = types.ModuleType("hermes_cli")
        plugins = types.ModuleType("hermes_cli.plugins")
        for key, value in attrs.items():
            setattr(plugins, key, value)
        pkg.plugins = plugins
        return mock.patch.dict(
            sys.modules, {"hermes_cli": pkg, "hermes_cli.plugins": plugins}
        )

    def _version(self, value):
        return mock.patch.object(host, "_hermes_version", return_value=value)

    def test_the_symbol_is_proof_the_host_bounds_the_hook(self):
        with self._hermes(**{self.FAIL_CLOSED: {"pre_tool_call"}}):
            self.assertTrue(host.bounds_pre_tool_call())

    def test_a_renamed_private_symbol_does_not_read_as_unbounded(self):
        # The bug. A private name is the one thing a host is free to change, and
        # the old detection was a bare hasattr on it, so a rename read exactly
        # like "this Hermes runs hooks to completion" and bought the full window
        # against a host that would cut it off.
        with self._hermes(_HOOK_TIMEOUT_ENFORCED_HOOKS={"pre_tool_call"}):
            with self._version((0, 21, 0)):
                self.assertTrue(host.bounds_pre_tool_call())

    def test_the_generation_that_really_is_unbounded_still_gets_the_full_window(self):
        # The behaviour worth keeping: 0.20.5 runs hooks to completion, so
        # clamping there shortens the window for a deadline that does not exist.
        with self._hermes():
            with self._version(host.UNBOUNDED_THROUGH_VERSION):
                self.assertFalse(host.bounds_pre_tool_call())

    def test_a_version_we_cannot_read_falls_to_bounded(self):
        with self._hermes():
            with self._version(None):
                self.assertTrue(host.bounds_pre_tool_call())

    def test_a_hermes_we_cannot_import_falls_to_bounded(self):
        with mock.patch.dict(sys.modules, {"hermes_cli": None}):
            self.assertTrue(host.bounds_pre_tool_call())

    def test_the_budget_follows_the_conclusion(self):
        # The reason any of this matters: what the gate actually waits.
        with mock.patch.object(host, "bounds_pre_tool_call", return_value=False):
            self.assertEqual(host.gate_budget_seconds(55.0), host.GATE_MAX_SECONDS)
        with mock.patch.object(host, "bounds_pre_tool_call", return_value=True):
            with mock.patch.object(
                host, "hook_callback_timeout_seconds", return_value=30.0
            ):
                self.assertEqual(
                    host.gate_budget_seconds(55.0),
                    30.0 - host.GATE_HEADROOM_SECONDS,
                )


class VersionParsingTest(unittest.TestCase):
    def _installed(self, raw):
        return mock.patch("importlib.metadata.version", return_value=raw)

    def test_reads_a_plain_release(self):
        with self._installed("0.20.5"):
            self.assertEqual(host._hermes_version(), (0, 20, 5))

    def test_reads_a_prerelease_without_choking_on_the_suffix(self):
        with self._installed("0.21.0rc1"):
            self.assertEqual(host._hermes_version(), (0, 21, 0))

    def test_pads_a_short_version(self):
        with self._installed("1.0"):
            self.assertEqual(host._hermes_version(), (1, 0, 0))

    def test_an_unparseable_version_is_none_rather_than_a_guess(self):
        # None means "cannot say", which the caller turns into bounded. A guess
        # here would be a guess about whether to throw away the user's answer.
        with self._installed("main"):
            self.assertIsNone(host._hermes_version())

    def test_a_version_that_compares_after_the_cutoff_is_newer(self):
        self.assertGreater((0, 21, 0), host.UNBOUNDED_THROUGH_VERSION)
        self.assertGreater((0, 20, 6), host.UNBOUNDED_THROUGH_VERSION)
        self.assertLessEqual((0, 20, 5), host.UNBOUNDED_THROUGH_VERSION)


if __name__ == "__main__":
    unittest.main()
