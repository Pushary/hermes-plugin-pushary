import inspect
import unittest
from pathlib import Path

import pushary_plugin as plugin
from pushary_plugin import approval, host

# What this plugin assumes about the host it runs inside.
#
# The rest of the suite proves the plugin behaves; this proves the assumptions it
# behaves ON are still true. Every one of them was read out of Hermes' source by
# hand once, and a hand reading is only true on the day it happened. The gate
# waited 55s against a 30s host budget for as long as it did because nothing here
# compared the two.
#
# The same idea as `bun run conformance` for Claude and Codex, and much less
# work: those lock their surface in a minified bundle and a Rust string table, so
# reading it means scanning bytes. Hermes is Python on the same machine, so the
# contract can simply be imported.
#
# Skipped where Hermes is not installed, which includes CI. That is honest rather
# than useless: it runs on the machine of anyone who has Hermes, which is anyone
# who can break this plugin. Run it deliberately after a Hermes upgrade.

try:
    from hermes_cli import plugins as hermes_plugins

    HERMES_IMPORT_ERROR = None
except Exception as exc:
    hermes_plugins = None
    HERMES_IMPORT_ERROR = exc

requires_hermes = unittest.skipIf(
    hermes_plugins is None,
    f"Hermes is not importable from this interpreter ({HERMES_IMPORT_ERROR})",
)

# The hook-callback timeout arrived after 0.20.5. Both generations are live, so
# the contract is version-aware rather than pinned to the newer one.
BOUNDS_HOOKS = hermes_plugins is not None and hasattr(
    hermes_plugins, "_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS"
)

requires_bounded_hooks = unittest.skipUnless(
    BOUNDS_HOOKS, "this Hermes runs plugin hooks to completion"
)


@requires_hermes
class HookContractTest(unittest.TestCase):
    def test_every_hook_we_register_is_one_hermes_fires(self):
        registered = set(_registered_hooks())
        unknown = registered - set(hermes_plugins.VALID_HOOKS)
        self.assertEqual(unknown, set(), "Hermes no longer fires these")

    def test_we_agree_with_the_host_on_whether_it_bounds_the_hook(self):
        # This used to read `assertEqual(host.bounds_pre_tool_call(),
        # BOUNDS_HOOKS)`, and BOUNDS_HOOKS was the same hasattr on the same
        # private symbol that bounds_pre_tool_call asked. Both sides moved
        # together, so the one thing it existed to catch, that symbol being
        # renamed, was the one thing it could not fail on.
        #
        # Checked against the version instead, which moves independently of the
        # private name.
        version = host._hermes_version()
        if BOUNDS_HOOKS:
            self.assertTrue(host.bounds_pre_tool_call())
        elif version is not None and version > host.UNBOUNDED_THROUGH_VERSION:
            # No symbol on a Hermes newer than the one that gained the timeout.
            # That is a rename, and the plugin must still clamp.
            self.assertTrue(
                host.bounds_pre_tool_call(),
                "the private name moved and we concluded unbounded",
            )
        else:
            self.assertFalse(host.bounds_pre_tool_call())

    @requires_bounded_hooks
    def test_pre_tool_call_still_fails_closed_on_timeout(self):
        self.assertIn("pre_tool_call", hermes_plugins._HOOK_TIMEOUT_FAIL_CLOSED_HOOKS)

    @requires_bounded_hooks
    def test_session_finalize_is_still_unbounded(self):
        bounded = hermes_plugins._HOOK_TIMEOUT_BOUNDED_HOOKS
        fail_closed = hermes_plugins._HOOK_TIMEOUT_FAIL_CLOSED_HOOKS
        self.assertNotIn("on_session_finalize", bounded | fail_closed)

    @requires_bounded_hooks
    def test_the_hook_budget_we_clamp_to_matches_the_hosts_default(self):
        self.assertEqual(
            hermes_plugins._HOOK_CALLBACK_TIMEOUT_SECS,
            host.HOOK_TIMEOUT_DEFAULT_SECONDS,
        )
        self.assertEqual(
            hermes_plugins._MAX_HOOK_CALLBACK_TIMEOUT_SECS,
            host.HOOK_TIMEOUT_MAX_SECONDS,
        )

    @requires_bounded_hooks
    def test_our_default_wait_fits_inside_the_hosts_default_budget(self):
        self.assertLess(
            host.gate_budget_seconds(host.GATE_MAX_SECONDS),
            hermes_plugins._HOOK_CALLBACK_TIMEOUT_SECS,
        )

    @unittest.skipIf(BOUNDS_HOOKS, "this Hermes bounds plugin hooks")
    def test_an_unbounded_host_gets_the_whole_window(self):
        self.assertEqual(
            host.gate_budget_seconds(host.GATE_MAX_SECONDS), host.GATE_MAX_SECONDS
        )

    def test_the_directive_actions_we_return_are_still_honored(self):
        source = inspect.getsource(hermes_plugins._get_pre_tool_call_directive_details)
        self.assertIn('("block", "approve")', source)


@requires_hermes
class RegistrationContractTest(unittest.TestCase):
    def test_the_entry_point_group_we_publish_under_is_unchanged(self):
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        declared = pyproject.read_text(encoding="utf-8")
        self.assertIn(
            f'[project.entry-points."{hermes_plugins.ENTRY_POINTS_GROUP}"]', declared
        )

    def test_the_context_still_offers_every_registration_we_call(self):
        context = hermes_plugins.PluginContext
        for name in (
            "register_tool",
            "register_hook",
            "register_skill",
            "register_approval_transport",
            "register_system_prompt_section",
        ):
            self.assertTrue(callable(getattr(context, name, None)), name)

    def test_our_transport_name_is_one_hermes_accepts(self):
        self.assertNotEqual(host.TRANSPORT_NAME, "builtin")
        self.assertRegex(host.TRANSPORT_NAME, r"^[a-z0-9][a-z0-9_-]{0,63}$")

    def test_our_prompt_section_fits_the_position_and_size_hermes_allows(self):
        self.assertIn("after_memory", hermes_plugins.SYSTEM_PROMPT_SECTION_POSITIONS)
        self.assertLessEqual(
            len(plugin.SYSTEM_PROMPT_SECTION),
            hermes_plugins.MAX_SYSTEM_PROMPT_SECTION_CHARS,
        )

    def test_we_declare_no_capability_we_would_have_to_be_granted(self):
        from hermes_cli.plugin_capabilities import VALID_CAPABILITY_IDS

        self.assertNotIn("tools.override", _registered_tool_overrides())
        self.assertTrue(VALID_CAPABILITY_IDS)


@requires_hermes
class ApprovalTransportContractTest(unittest.TestCase):
    def test_the_request_still_carries_everything_the_transport_reads(self):
        from hermes_cli.approval_transport import ApprovalRequest

        fields = set(ApprovalRequest.__dataclass_fields__)
        for name in (
            "command",
            "description",
            "pattern_key",
            "timeout_seconds",
            "allowed_choices",
        ):
            self.assertIn(name, fields)
        self.assertTrue(callable(ApprovalRequest.respond))

    def test_every_choice_we_offer_is_one_the_host_can_return(self):
        from hermes_cli.approval_transport import ApprovalRequest

        request = ApprovalRequest.create(
            command="rm -rf build",
            description="",
            pattern_key="destructive:rm",
            pattern_keys=("destructive:rm",),
            session_key="s",
            surface="cli",
            allow_session=True,
            allow_permanent=True,
        )
        offered = {choice for choice, _ in approval.CHOICE_LABELS}
        self.assertEqual(offered - set(request.allowed_choices), set())

    def test_a_transport_answer_is_correlated_the_way_we_build_it(self):
        from hermes_cli.approval_transport import ApprovalRequest

        request = ApprovalRequest.create(
            command="rm -rf build",
            description="",
            pattern_key="k",
            pattern_keys=("k",),
            session_key="s",
            surface="cli",
            allow_session=False,
            allow_permanent=False,
        )
        decision = request.respond("once")
        self.assertEqual(decision.request_id, request.request_id)
        self.assertEqual(decision.request_digest, request.digest)

    def test_the_plugin_rule_label_we_unwrap_is_still_the_hosts_wording(self):
        from tools import approval as hermes_approval

        source = inspect.getsource(hermes_approval.request_tool_approval)
        self.assertIn(approval.PLUGIN_RULE_MARKER, source)

    def test_the_config_keys_we_read_are_the_ones_the_host_reads(self):
        from tools.approval import _get_approval_transport_config

        source = inspect.getsource(_get_approval_transport_config)
        self.assertIn('"transport"', source)
        self.assertIn('"approval"', source)
        self.assertIn('"security"', source)


def _registered_hooks():
    class HookRecorder:
        def __init__(self):
            self.hooks = []

        def register_tool(self, **kwargs):
            pass

        def register_hook(self, hook_name, callback):
            self.hooks.append(hook_name)

        def register_skill(self, *args, **kwargs):
            pass

        def register_approval_transport(self, *args, **kwargs):
            pass

        def register_system_prompt_section(self, *args, **kwargs):
            pass

    recorder = HookRecorder()
    plugin.register(recorder)
    return recorder.hooks


def _registered_tool_overrides():
    class OverrideRecorder:
        def __init__(self):
            self.overrides = set()

        def register_tool(self, override=False, **kwargs):
            if override:
                self.overrides.add("tools.override")

        def register_hook(self, *args, **kwargs):
            pass

        def register_skill(self, *args, **kwargs):
            pass

        def register_approval_transport(self, *args, **kwargs):
            pass

        def register_system_prompt_section(self, *args, **kwargs):
            pass

    recorder = OverrideRecorder()
    plugin.register(recorder)
    return recorder.overrides


if __name__ == "__main__":
    unittest.main()
