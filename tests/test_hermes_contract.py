import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pushary_plugin as plugin
from pushary_plugin import approval, approval_ledger, desktop_contract, host, installation

SANDBOX_HERMES_HOME = tempfile.TemporaryDirectory(prefix="pushary-hermes-home-")


HERMES_REQUIRED = os.environ.get("PUSHARY_REQUIRE_HERMES") == "1"


def setUpModule():
    if HERMES_REQUIRED and hermes_plugins is None:
        raise RuntimeError(f"PUSHARY_REQUIRE_HERMES=1 but Hermes is not importable: {HERMES_IMPORT_ERROR}")
    os.environ["HERMES_HOME"] = SANDBOX_HERMES_HOME.name


def tearDownModule():
    os.environ.pop("HERMES_HOME", None)
    SANDBOX_HERMES_HOME.cleanup()

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
def _hook_timeout_module():
    if hermes_plugins is None:
        return None
    try:
        from hermes_cli import plugins_dispatch
    except Exception:
        plugins_dispatch = None
    for module in (plugins_dispatch, hermes_plugins):
        if module is not None and hasattr(module, "_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS"):
            return module
    return None


HOOK_TIMEOUTS = _hook_timeout_module()
BOUNDS_HOOKS = HOOK_TIMEOUTS is not None

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
        self.assertIn("pre_tool_call", HOOK_TIMEOUTS._HOOK_TIMEOUT_FAIL_CLOSED_HOOKS)

    @requires_bounded_hooks
    def test_session_finalize_is_still_unbounded(self):
        bounded = HOOK_TIMEOUTS._HOOK_TIMEOUT_BOUNDED_HOOKS
        fail_closed = HOOK_TIMEOUTS._HOOK_TIMEOUT_FAIL_CLOSED_HOOKS
        self.assertNotIn("on_session_finalize", bounded | fail_closed)

    @requires_bounded_hooks
    def test_the_hook_budget_we_clamp_to_matches_the_hosts_default(self):
        self.assertEqual(
            HOOK_TIMEOUTS._HOOK_CALLBACK_TIMEOUT_SECS,
            host.HOOK_TIMEOUT_DEFAULT_SECONDS,
        )
        self.assertEqual(
            HOOK_TIMEOUTS._MAX_HOOK_CALLBACK_TIMEOUT_SECS,
            host.HOOK_TIMEOUT_MAX_SECONDS,
        )

    @requires_bounded_hooks
    def test_our_default_wait_fits_inside_the_hosts_default_budget(self):
        self.assertLess(
            host.gate_budget_seconds(host.GATE_MAX_SECONDS),
            HOOK_TIMEOUTS._HOOK_CALLBACK_TIMEOUT_SECS,
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
            len(plugin.SYSTEM_PROMPT_SECTION + plugin.QUESTION_ROUTING_PROMPT),
            hermes_plugins.MAX_SYSTEM_PROMPT_SECTION_CHARS,
        )

    def test_the_question_tool_we_redirect_is_hermes_own(self):
        import toolsets

        self.assertIn(plugin.HERMES_QUESTION_TOOL, toolsets.TOOLSETS[plugin.HERMES_QUESTION_TOOL]["tools"])

    def test_load_records_live_in_hermes_plugin_data(self):
        from hermes_constants import get_hermes_home

        plugin_data = Path(get_hermes_home()) / "plugin-data" / installation.PLUGIN_DATA_NAMESPACE
        self.assertEqual(installation.load_records_directory().parent, plugin_data)

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
        from tools.approval_context import _get_approval_transport_config

        config = {"security": {"approval": {"transport": "pushary", "transport_fallback": "builtin"}}}
        with mock.patch("hermes_cli.config.load_config_readonly", return_value=config):
            self.assertEqual(_get_approval_transport_config(), ("pushary", "builtin"))

    def test_outbound_detail_uses_the_hosts_strict_credential_redaction(self):
        from agent import redact

        vendor_key = "sk-" + "abcdefghij" * 30
        opaque = "opaque-test-value-0123456789"
        url = f"https://a.test/?token={opaque}"
        with mock.patch.object(redact, "_REDACT_ENABLED", False):
            for text, secret in ((vendor_key, vendor_key[:100]), (url, opaque), (f"Bearer {opaque}", opaque)):
                with self.subTest(text=text):
                    self.assertNotIn(secret, plugin._redact_outbound_text(text))
                    self.assertNotIn(secret, plugin._summarize_args({"url": text}))
                    self.assertNotIn(secret, plugin._arg_target({"url": text}))


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



def _present_through_hermes(present):
    from types import SimpleNamespace

    from tools import approval_context, approval_prompt

    calls = []
    manager = SimpleNamespace(get_approval_transport=lambda name: SimpleNamespace(present=present))
    with mock.patch.object(approval_context, "_get_approval_transport_config", return_value=(host.TRANSPORT_NAME, None)), \
            mock.patch.object(approval_context, "_get_approval_timeout", return_value=5), \
            mock.patch.object(approval_context, "_fire_approval_hook", side_effect=lambda name, **kw: calls.append((name, kw))), \
            mock.patch.object(approval_prompt, "get_plugin_manager", return_value=manager):
        approval_prompt._present_with_selected_transport(
            command="git push --force", description="", pattern_key="k", pattern_keys=["k"], session_key="s",
            surface="cli", allow_session=False, allow_permanent=False,
        )
    return [kwargs for name, kwargs in calls if name == "post_approval_response"]


@requires_hermes
class DesktopHalfContractTest(unittest.TestCase):
    def test_the_hook_that_ends_a_desktop_wait_is_one_the_host_fires(self):
        self.assertIn("post_approval_response", hermes_plugins.VALID_HOOKS)

    def test_the_host_names_our_transport_and_request_when_it_settles_one(self):
        responses = _present_through_hermes(lambda request: request.respond("once"))
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["surface"], plugin.PUSHARY_TRANSPORT_SURFACE)
        self.assertTrue(approval_ledger.is_request_id(responses[0]["request_id"]))
        self.assertEqual(responses[0]["choice"], "once")

    def test_a_failed_transport_settles_with_a_choice_the_ledger_accepts(self):
        def failing(request):
            raise approval.TransportUnavailable("handed back")

        choice = _present_through_hermes(failing)[0]["choice"]
        self.assertIn(choice, desktop_contract.HOST_FAILURE_OUTCOMES)
        self.assertTrue(approval_ledger.is_token(choice))

    def test_every_way_hermes_fails_a_transport_is_one_the_desktop_half_names(self):
        import time as clock

        from hermes_cli import approval_transport as transport

        def request():
            return transport.ApprovalRequest.create(
                command="git push --force", description="", pattern_key="k", pattern_keys=("k",), session_key="s",
                surface="cli", allow_session=True, allow_permanent=True, timeout_seconds=5,
            )

        stale = request()

        def run(present, timeout_seconds=2.0, is_interrupted=None):
            return transport.invoke_approval_transport(present, request(), timeout_seconds=timeout_seconds,
                                                       poll_interval=0.01, is_interrupted=is_interrupted).failure

        def raising(req):
            raise approval.TransportUnavailable("handed back")

        failures = {
            run(raising),
            run(lambda req: clock.sleep(0.5), timeout_seconds=0.05),
            run(lambda req: clock.sleep(0.5), is_interrupted=lambda: True),
            run(lambda req: "once"),
            run(lambda req: stale.respond("once")),
        }
        held = []
        while transport._transport_worker_slots.acquire(blocking=False):
            held.append(True)
        try:
            failures.add(run(lambda req: req.respond("once")))
        finally:
            for _ in held:
                transport._transport_worker_slots.release()
        self.assertEqual({f"transport_{failure}" for failure in failures}, set(desktop_contract.HOST_FAILURE_OUTCOMES))

    def test_the_event_the_desktop_half_listens_for_is_the_name_hermes_broadcasts(self):
        from hermes_cli.plugin_events import broadcast_plugin_event, plugin_event_name

        self.assertTrue(callable(broadcast_plugin_event))
        self.assertEqual(
            plugin_event_name(desktop_contract.PLUGIN_ID, desktop_contract.APPROVALS_CHANGED_EVENT),
            desktop_contract.APPROVALS_CHANGED_WIRE_EVENT,
        )

    def test_the_backend_serves_through_the_hosts_own_mount(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except Exception as exc:
            self.skipTest(f"FastAPI is not importable here ({exc})")
        import json
        import sys
        import time

        from hermes_cli.web_server_dashboard import _dashboard_plugin_entry, _mount_one_plugin_api

        dashboard = Path(plugin.__file__).parent / "dashboard"
        entry = _dashboard_plugin_entry(json.loads((dashboard / "manifest.json").read_text()), desktop_contract.PLUGIN_ID,
                                        dashboard, "bundled")
        app = FastAPI()
        self.assertTrue(_mount_one_plugin_api(app, entry, {desktop_contract.PLUGIN_ID}, set()))
        backend_ledger = sys.modules["pushary_plugin_desktop_backend.approval_ledger"]
        backend_installation = sys.modules["pushary_plugin_desktop_backend.installation"]
        with tempfile.TemporaryDirectory(prefix="pushary-mount-") as directory, \
                mock.patch.object(backend_installation, "hermes_home", return_value=Path(directory)):
            now = int(time.time() * 1000)
            backend_ledger.open_approval(Path(directory), correlation_id="5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d",
                                         request_id=None, subject="git push --force",
                                         choices=backend_ledger.offered_choices((("once", "Allow once"),)),
                                         pid=os.getpid(), opened_at_ms=now, deadline_at_ms=now + 60_000)
            response = TestClient(app).get(f"/api/plugins/{desktop_contract.PLUGIN_ID}{desktop_contract.APPROVALS_PATH}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version"], desktop_contract.CONTRACT_VERSION)
        self.assertEqual(len(response.json()["waiting"]), 1)

    def test_the_backend_mounts_with_the_hosts_own_loader_rules(self):
        from hermes_cli.web_server_dashboard import _dashboard_plugin_entry
        import json

        dashboard = Path(plugin.__file__).parent / "dashboard"
        entry = _dashboard_plugin_entry(json.loads((dashboard / "manifest.json").read_text()), desktop_contract.PLUGIN_ID,
                                        dashboard, "user")
        self.assertTrue(entry["has_api"])
        self.assertTrue(entry["tab"].get("hidden"))
        self.assertTrue((dashboard / entry["entry"]).is_file())


if __name__ == "__main__":
    unittest.main()
