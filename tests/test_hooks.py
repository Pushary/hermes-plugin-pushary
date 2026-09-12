import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pushary_plugin as plugin
from pushary_plugin import api, approval, host, identity, tools


class FakeCtx:
    def __init__(self):
        self.tools = []
        self.hooks = []
        self.skills = []
        self.transport = None
        self.section = None

    def register_tool(self, name, toolset, schema, handler, description=""):
        self.tools.append((name, schema, handler))

    def register_hook(self, hook_name, callback):
        self.hooks.append(hook_name)

    def register_skill(self, name, path):
        self.skills.append(name)

    def register_approval_transport(self, name, present_fn):
        self.transport = (name, present_fn)

    def register_system_prompt_section(self, id, content, **kwargs):
        self.section = (id, content)


class LegacyCtx(FakeCtx):
    def register_approval_transport(self, name, present_fn):
        raise AttributeError("older Hermes")

    def register_system_prompt_section(self, id, content, **kwargs):
        raise AttributeError("older Hermes")


class FakeRequest:
    def __init__(self, allowed=("once", "session", "always", "deny"), timeout_seconds=300):
        self.command = "rm -rf build && git push --force origin main"
        self.description = "Deletes a directory and force-pushes"
        self.pattern_key = "destructive:rm"
        self.pattern_keys = ("destructive:rm",)
        self.surface = "cli"
        self.timeout_seconds = timeout_seconds
        self.allowed_choices = allowed
        self.responded = None

    def respond(self, choice):
        self.responded = choice
        return choice


def reset_state():
    plugin._session_totals.update(tools=0, errors=0, notified=0)
    plugin._last_turn.update(completed=False, interrupted=False)
    identity.forget_session()


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        reset_state()

    def test_every_tool_schema_matches_its_registered_name(self):
        ctx = FakeCtx()
        plugin.register(ctx)
        for name, schema, handler in ctx.tools:
            self.assertEqual(name, schema["name"])
            self.assertTrue(callable(handler))

    def test_registers_the_hooks_the_host_declares_valid(self):
        ctx = FakeCtx()
        plugin.register(ctx)
        self.assertEqual(
            ctx.hooks,
            ["pre_llm_call", "pre_tool_call", "post_tool_call", "on_session_start",
             "on_session_end", "on_session_finalize"],
        )

    def test_registers_the_approval_transport_and_prompt_section(self):
        ctx = FakeCtx()
        plugin.register(ctx)
        self.assertEqual(ctx.transport[0], "pushary")
        self.assertEqual(ctx.section[0], plugin.SYSTEM_PROMPT_SECTION_ID)
        self.assertLessEqual(len(ctx.section[1]), 4000)

    def test_loads_against_a_host_without_the_newer_registration_apis(self):
        ctx = LegacyCtx()
        plugin.register(ctx)
        self.assertIsNone(ctx.transport)
        self.assertEqual(len(ctx.tools), len(plugin.TOOL_REGISTRATIONS))


class GateBudgetTest(unittest.TestCase):
    """Clamping is only correct against a host that bounds the callback.

    Pinned here rather than inherited from whatever Hermes happens to be on the
    machine running the suite, because both generations are live: the timeout
    arrived after 0.20.5, and on 0.20.5 the full window is the right answer.
    """

    def setUp(self):
        bounded = mock.patch.object(host, "bounds_pre_tool_call", return_value=True)
        bounded.start()
        self.addCleanup(bounded.stop)

    def test_default_request_fits_inside_the_default_hook_timeout(self):
        with mock.patch.object(host, "_config", return_value={}):
            self.assertEqual(host.hook_callback_timeout_seconds(), 30.0)
            budget = host.gate_budget_seconds(55.0)
        self.assertLess(budget, 30.0)
        self.assertEqual(budget, 25.0)

    def test_a_raised_host_timeout_raises_the_budget(self):
        config = {"plugins": {"hook_callback_timeout": 120}}
        with mock.patch.object(host, "_config", return_value=config):
            self.assertEqual(host.gate_budget_seconds(55.0), 55.0)

    def test_a_disabled_host_timeout_allows_the_full_window(self):
        config = {"plugins": {"hook_callback_timeout": 0}}
        with mock.patch.object(host, "_config", return_value=config):
            self.assertEqual(host.gate_budget_seconds(55.0), 55.0)

    def test_a_lowered_host_timeout_shrinks_the_budget(self):
        config = {"plugins": {"hook_callback_timeout": 10}}
        with mock.patch.object(host, "_config", return_value=config):
            self.assertEqual(host.gate_budget_seconds(55.0), 5.0)

    def test_unreadable_config_falls_back_to_the_documented_default(self):
        config = {"plugins": {"hook_callback_timeout": "not a number"}}
        with mock.patch.object(host, "_config", return_value=config):
            self.assertEqual(host.hook_callback_timeout_seconds(), 30.0)

    def test_a_host_that_never_cuts_the_hook_off_gets_the_whole_window(self):
        with mock.patch.object(host, "bounds_pre_tool_call", return_value=False), \
                mock.patch.object(host, "_config", return_value={}):
            self.assertEqual(host.gate_budget_seconds(55.0), host.GATE_MAX_SECONDS)

    def test_an_unreadable_host_is_assumed_to_bound_the_hook(self):
        with mock.patch.dict("sys.modules", {"hermes_cli": None}):
            self.assertTrue(host.bounds_pre_tool_call())

    def test_selected_transport_is_read_from_hermes_config(self):
        config = {"security": {"approval": {"transport": "Pushary"}}}
        with mock.patch.object(host, "_config", return_value=config):
            self.assertTrue(host.pushary_is_selected_transport())
        with mock.patch.object(host, "_config", return_value={}):
            self.assertFalse(host.pushary_is_selected_transport())


class PreToolCallTest(unittest.TestCase):
    def setUp(self):
        reset_state()
        os.environ["PUSHARY_API_KEY"] = "pk_test.sk_test"
        os.environ["PUSHARY_GATE_TOOLS"] = "terminal,write_file"
        os.environ.pop("PUSHARY_GATE_TIMEOUT_MS", None)
        self.addCleanup(os.environ.pop, "PUSHARY_API_KEY", None)
        self.addCleanup(os.environ.pop, "PUSHARY_GATE_TOOLS", None)

    def _ask(self, response):
        captured = {}

        def fake_ask(params, **kwargs):
            captured.update(params)
            return json.dumps({"correlationId": "c1", **response})

        return captured, fake_ask

    def test_ungated_and_pushary_tools_are_never_gated(self):
        self.assertIsNone(plugin._on_pre_tool_call(tool_name="read_file", args={}))
        self.assertIsNone(plugin._on_pre_tool_call(tool_name="pushary_ask", args={}))

    def test_no_gate_tools_configured_means_no_opinion(self):
        os.environ["PUSHARY_GATE_TOOLS"] = ""
        self.assertIsNone(plugin._on_pre_tool_call(tool_name="terminal", args={}))

    def test_the_wait_never_exceeds_the_host_hook_budget(self):
        captured, fake_ask = self._ask({"answered": True, "value": "yes"})
        with mock.patch.object(tools, "pushary_ask", fake_ask), \
                mock.patch.object(host, "bounds_pre_tool_call", return_value=True), \
                mock.patch.object(host, "_config", return_value={}):
            plugin._on_pre_tool_call(tool_name="terminal", args={"command": "rm -rf x"})
        self.assertEqual(captured["timeout_ms"], 25000)
        self.assertLess(captured["timeout_ms"], host.HOOK_TIMEOUT_DEFAULT_SECONDS * 1000)

    def test_the_question_is_asked_in_one_round_trip(self):
        captured, fake_ask = self._ask({"answered": True, "value": "yes"})
        with mock.patch.object(tools, "pushary_ask", fake_ask), \
                mock.patch.object(api, "wait_for_answer", side_effect=AssertionError("server already waited")):
            plugin._on_pre_tool_call(tool_name="terminal", args={})
        self.assertTrue(captured["wait"])

    def test_an_approval_lets_the_tool_run(self):
        _, fake_ask = self._ask({"answered": True, "value": "yes"})
        with mock.patch.object(tools, "pushary_ask", fake_ask):
            self.assertIsNone(plugin._on_pre_tool_call(tool_name="terminal", args={}))

    def test_a_denial_blocks_and_says_who_denied_it(self):
        _, fake_ask = self._ask({"answered": True, "value": "no"})
        with mock.patch.object(tools, "pushary_ask", fake_ask):
            result = plugin._on_pre_tool_call(tool_name="terminal", args={})
        self.assertEqual(result["action"], "block")
        self.assertIn("Denied from Pushary", result["message"])

    def test_no_answer_blocks(self):
        _, fake_ask = self._ask({"answered": False, "timedOut": True})
        with mock.patch.object(tools, "pushary_ask", fake_ask), \
                mock.patch.object(api, "cancel_question", return_value={"cancelled": True}):
            result = plugin._on_pre_tool_call(tool_name="terminal", args={})
        self.assertEqual(result["action"], "block")

    def test_no_connected_device_says_so_instead_of_reading_as_a_refusal(self):
        _, fake_ask = self._ask({
            "answered": False,
            "noDevices": True,
            "nextAction": "ask_in_current_client",
            "handoffAction": "cancel_then_ask_in_current_client",
        })
        with mock.patch.object(tools, "pushary_ask", fake_ask), \
                mock.patch.object(api, "cancel_question", return_value={"cancelled": True}) as cancel:
            result = plugin._on_pre_tool_call(tool_name="terminal", args={})
        self.assertEqual(result["action"], "block")
        self.assertIn("no phone, browser, or Slack channel is connected", result["message"])
        cancel.assert_called_once_with("c1")

    def test_a_stop_handoff_never_reopens_the_question(self):
        _, fake_ask = self._ask({
            "answered": False,
            "handoffAction": "cancel_then_ask_in_current_client",
        })
        with mock.patch.object(tools, "pushary_ask", fake_ask), \
                mock.patch.object(api, "cancel_question", return_value={"handoffAction": "stop"}), \
                mock.patch.object(api, "wait_for_answer", side_effect=AssertionError("must not re-poll")):
            result = plugin._on_pre_tool_call(tool_name="terminal", args={})
        self.assertEqual(result["action"], "block")

    def test_an_answer_that_wins_the_cancel_race_is_honored(self):
        _, fake_ask = self._ask({
            "answered": False,
            "handoffAction": "cancel_then_ask_in_current_client",
        })
        with mock.patch.object(tools, "pushary_ask", fake_ask), \
                mock.patch.object(api, "cancel_question", return_value={"cancelled": False}), \
                mock.patch.object(api, "wait_for_answer", return_value={"answered": True, "value": "yes"}):
            self.assertIsNone(plugin._on_pre_tool_call(tool_name="terminal", args={}))

    def test_a_missing_api_key_blocks_rather_than_calling_pushary(self):
        os.environ.pop("PUSHARY_API_KEY")
        with mock.patch.object(tools, "pushary_ask", side_effect=AssertionError("must not call")):
            result = plugin._on_pre_tool_call(tool_name="terminal", args={})
        self.assertEqual(result["action"], "block")

    def test_the_selected_transport_takes_over_the_gate(self):
        config = {"security": {"approval": {"transport": "pushary"}}}
        with mock.patch.object(host, "_config", return_value=config), \
                mock.patch.object(tools, "pushary_ask", side_effect=AssertionError("must not call")):
            result = plugin._on_pre_tool_call(tool_name="terminal", args={})
        self.assertEqual(result["action"], "approve")
        self.assertEqual(result["rule_key"], "terminal")

    def test_the_escalation_carries_the_args_the_host_gate_cannot_see(self):
        config = {"security": {"approval": {"transport": "pushary"}}}
        with mock.patch.object(host, "_config", return_value=config):
            result = plugin._on_pre_tool_call(
                tool_name="terminal", args={"command": "rm -rf /tmp/build"}
            )
        self.assertIn("rm -rf /tmp/build", result["message"])
        self.assertLessEqual(len(result["message"]), 500)

    def test_the_gate_sends_the_fields_the_decision_layer_needs(self):
        captured, fake_ask = self._ask({"answered": True, "value": "yes"})
        with mock.patch.object(tools, "pushary_ask", fake_ask):
            plugin._on_pre_tool_call(
                tool_name="terminal",
                args={"command": "git push --force"},
                session_id="sess-abc",
            )
        self.assertEqual(captured["tool_name"], "terminal")
        self.assertEqual(captured["tool_target"], "git push")
        self.assertEqual(identity.session_id(), "sess-abc")


class ApprovalTransportTest(unittest.TestCase):
    def setUp(self):
        reset_state()

    def test_offers_every_choice_the_host_allows(self):
        request = FakeRequest()
        captured = {}
        with mock.patch.object(api, "ask_user", side_effect=lambda **kw: captured.update(kw) or {"correlationId": "c1"}), \
                mock.patch.object(api, "wait_for_answer", return_value={"answered": True, "value": "Always allow"}):
            self.assertEqual(approval.present(request), "always")
        self.assertEqual(captured["question_type"], "select")
        self.assertEqual(
            captured["options"],
            ["Allow once", "Allow for this session", "Always allow", "Deny"],
        )

    def test_falls_back_to_confirm_when_only_once_and_deny_are_allowed(self):
        request = FakeRequest(allowed=("once", "deny"))
        captured = {}
        with mock.patch.object(api, "ask_user", side_effect=lambda **kw: captured.update(kw) or {"correlationId": "c1"}), \
                mock.patch.object(api, "wait_for_answer", return_value={"answered": True, "value": "yes"}):
            self.assertEqual(approval.present(request), "once")
        self.assertEqual(captured["question_type"], "confirm")
        self.assertIsNone(captured["options"])

    def test_an_unrecognised_answer_denies(self):
        request = FakeRequest()
        with mock.patch.object(api, "ask_user", return_value={"correlationId": "c1"}), \
                mock.patch.object(api, "wait_for_answer", return_value={"answered": True, "value": "maybe"}):
            self.assertEqual(approval.present(request), "deny")

    def test_no_answer_before_the_host_deadline_denies_and_retracts(self):
        request = FakeRequest(timeout_seconds=0)
        with mock.patch.object(api, "ask_user", return_value={"correlationId": "c1"}), \
                mock.patch.object(api, "cancel_question", return_value={"cancelled": True}) as cancel:
            self.assertEqual(approval.present(request), "deny")
        cancel.assert_called_once_with("c1")

    def test_no_connected_device_raises_so_the_host_can_fall_back(self):
        request = FakeRequest()
        with mock.patch.object(api, "ask_user", return_value={"correlationId": "c1", "noDevices": True}):
            with self.assertRaises(approval.TransportUnavailable):
                approval.present(request)

    def test_an_api_error_raises_so_the_host_can_fall_back(self):
        request = FakeRequest()
        with mock.patch.object(api, "ask_user", return_value={"error": "HTTP 401"}):
            with self.assertRaises(approval.TransportUnavailable):
                approval.present(request)

    def test_a_plugin_rule_asks_about_the_tool_not_the_synthetic_label(self):
        request = FakeRequest()
        request.command = "<terminal> (plugin approval rule)"
        request.description = "Run terminal?\ncommand: rm -rf /tmp/build"
        captured = {}
        with mock.patch.object(api, "ask_user", side_effect=lambda **kw: captured.update(kw) or {"correlationId": "c1"}), \
                mock.patch.object(api, "wait_for_answer", return_value={"answered": True, "value": "Deny"}):
            approval.present(request)
        self.assertIn("Run terminal?", captured["question"])
        self.assertNotIn("plugin approval rule", captured["question"])

    def test_a_dangerous_command_still_asks_about_the_command(self):
        request = FakeRequest()
        captured = {}
        with mock.patch.object(api, "ask_user", side_effect=lambda **kw: captured.update(kw) or {"correlationId": "c1"}), \
                mock.patch.object(api, "wait_for_answer", return_value={"answered": True, "value": "Deny"}):
            approval.present(request)
        self.assertIn("git push", captured["question"])

    def test_carries_the_command_as_the_decision_body(self):
        request = FakeRequest()
        captured = {}
        with mock.patch.object(api, "ask_user", side_effect=lambda **kw: captured.update(kw) or {"correlationId": "c1"}), \
                mock.patch.object(api, "wait_for_answer", return_value={"answered": True, "value": "Deny"}):
            approval.present(request)
        self.assertEqual(captured["tool_name"], "terminal")
        self.assertEqual(captured["tool_target"], "rm -rf")
        self.assertEqual(captured["action_body"], request.command)
        self.assertIn("destructive:rm", captured["blocker"])
        self.assertIn("git push", captured["question"])


class SessionLifecycleTest(unittest.TestCase):
    def setUp(self):
        reset_state()
        os.environ["PUSHARY_API_KEY"] = "pk_test.sk_test"
        os.environ["PUSHARY_AUTO_NOTIFY_SESSION_END"] = "1"
        events = mock.patch.object(api, "agent_event", return_value={})
        events.start()
        self.addCleanup(events.stop)
        self.addCleanup(os.environ.pop, "PUSHARY_API_KEY", None)
        self.addCleanup(os.environ.pop, "PUSHARY_AUTO_NOTIFY_SESSION_END", None)

    def test_a_turn_boundary_never_notifies(self):
        with mock.patch.object(api, "send_notification") as notify:
            plugin._on_session_start(session_id="s1")
            for _ in range(3):
                plugin._on_post_tool_call("terminal", {}, "{}", 1, status="ok")
                plugin._on_session_end(session_id="s1", completed=True)
        notify.assert_not_called()

    def test_the_session_boundary_notifies_once_with_the_whole_session(self):
        with mock.patch.object(api, "send_notification") as notify:
            plugin._on_session_start(session_id="s1")
            for _ in range(3):
                plugin._on_post_tool_call("terminal", {}, "{}", 1, status="ok")
                plugin._on_session_end(session_id="s1", completed=True)
            plugin._on_session_finalize(session_id="s1")
        notify.assert_called_once()
        self.assertIn("3 tool calls", notify.call_args.kwargs["body"])
        self.assertEqual(notify.call_args.kwargs["context"]["type"], "task_complete")

    def test_a_session_that_ran_no_tools_stays_quiet(self):
        with mock.patch.object(api, "send_notification") as notify:
            plugin._on_session_start(session_id="s1")
            plugin._on_session_finalize(session_id="s1")
        notify.assert_not_called()

    def test_opt_out_stays_quiet(self):
        os.environ.pop("PUSHARY_AUTO_NOTIFY_SESSION_END")
        with mock.patch.object(api, "send_notification") as notify:
            plugin._on_session_start(session_id="s1")
            plugin._on_post_tool_call("terminal", {}, "{}", 1, status="ok")
            plugin._on_session_finalize(session_id="s1")
        notify.assert_not_called()

    def test_the_session_id_does_not_leak_into_the_next_session(self):
        plugin._on_session_start(session_id="s1")
        self.assertEqual(identity.session_id(), "s1")
        with mock.patch.object(api, "send_notification"):
            plugin._on_session_finalize(session_id="s1")
        self.assertIsNone(identity.session_id())


class SessionCommandTest(unittest.TestCase):
    def setUp(self):
        reset_state()
        os.environ["PUSHARY_API_KEY"] = "pk_test.sk_test"
        self.addCleanup(os.environ.pop, "PUSHARY_API_KEY", None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        pending = mock.patch.object(api, "PENDING_DIR", Path(directory.name))
        pending.start()
        self.addCleanup(pending.stop)

    def test_session_start_advertises_the_command_consumer(self):
        with mock.patch.object(api, "agent_event", return_value={}) as report:
            plugin._on_session_start(session_id="s1")
        report.assert_called_once_with("session_start", "s1", can_drain=True)

    def test_a_notch_instruction_is_injected_into_the_next_turn(self):
        with mock.patch.object(
            api, "agent_event", return_value={"pendingCommand": "Review the failing test"}
        ) as report:
            result = plugin._on_pre_llm_call(session_id="s1")
        report.assert_called_once_with("user_prompt", "s1", can_drain=True)
        self.assertIn("Review the failing test", result["context"])

    def test_no_queued_instruction_adds_no_context(self):
        with mock.patch.object(api, "agent_event", return_value={}):
            self.assertIsNone(plugin._on_pre_llm_call(session_id="s1"))

    def test_agent_event_acknowledges_a_claimed_instruction(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "pendingCommand": "run",
            "pendingCommandId": "00000000-0000-4000-8000-000000000001",
        }).encode("utf-8")
        ack_response = mock.MagicMock()
        ack_response.__enter__.return_value.status = 200
        with mock.patch.object(api.urllib.request, "urlopen", side_effect=[response, ack_response]) as send:
            result = api.agent_event("user_prompt", "s1", can_drain=True)

        self.assertEqual(result["pendingCommand"], "run")
        ack = json.loads(send.call_args_list[1].args[0].data.decode("utf-8"))
        self.assertEqual(ack["commandId"], "00000000-0000-4000-8000-000000000001")
        self.assertEqual(ack["sessionId"], "s1")
        self.assertEqual(ack["machineId"], identity.machine_id())

    def test_agent_event_waits_for_a_failed_ack_without_reinjecting(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "pendingCommand": "run",
            "pendingCommandId": "00000000-0000-4000-8000-000000000001",
        }).encode("utf-8")
        with mock.patch.object(api.urllib.request, "urlopen", side_effect=[response, OSError("offline")]):
            result = api.agent_event("user_prompt", "s1", can_drain=True)
        self.assertNotIn("pendingCommand", result)
        self.assertEqual(api._read_pending("s1")["delivered"], False)

    def test_only_one_hook_creates_a_command_receipt(self):
        receipt = {
            "id": "00000000-0000-4000-8000-000000000001",
            "text": "run",
            "expiresAtMs": 9_999_999_999_999,
            "delivered": False,
        }
        self.assertTrue(api._write_pending("s1", receipt, exclusive=True))
        self.assertFalse(api._write_pending("s1", receipt, exclusive=True))

    def test_session_finalize_withdraws_the_command_consumer(self):
        with mock.patch.object(api, "agent_event", return_value={}) as report:
            plugin._on_session_start(session_id="s1")
            plugin._on_session_finalize(session_id="s1")
        self.assertEqual(report.call_args_list[-1], mock.call("session_closed", "s1", can_drain=False))


class SharedKeyTest(unittest.TestCase):
    def test_uses_the_agent_hooks_key_without_a_shell_export(self):
        with self.subTest(), mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(Path, "home", return_value=Path("/tmp/pushary-test-home")), \
                mock.patch.object(Path, "read_text", return_value='{"apiKey":"pk_shared.sk_shared"}'):
            self.assertEqual(api._get_api_key(), "pk_shared.sk_shared")


class PostToolCallTest(unittest.TestCase):
    def setUp(self):
        reset_state()
        os.environ["PUSHARY_API_KEY"] = "pk_test.sk_test"
        self.addCleanup(os.environ.pop, "PUSHARY_API_KEY", None)

    def test_uses_the_hosts_status_for_a_failure_that_is_not_json(self):
        with mock.patch.object(api, "send_notification") as notify:
            plugin._on_post_tool_call(
                "terminal", {}, "bash: no such file", 12,
                status="error", error_message="exit 1",
            )
        notify.assert_called_once()
        self.assertIn("exit 1", notify.call_args.kwargs["body"])

    def test_a_host_reported_success_is_never_reported_as_an_error(self):
        with mock.patch.object(api, "send_notification") as notify:
            plugin._on_post_tool_call("terminal", {}, '{"error": null}', 1, status="ok")
        notify.assert_not_called()

    def test_falls_back_to_parsing_when_the_host_sends_no_status(self):
        with mock.patch.object(api, "send_notification") as notify:
            plugin._on_post_tool_call("read_file", {}, '{"error": "ENOENT"}', 3)
        notify.assert_called_once()

    def test_caps_error_pushes_but_keeps_counting(self):
        with mock.patch.object(api, "send_notification") as notify:
            for index in range(6):
                plugin._on_post_tool_call("terminal", {}, "x", 1, status="error",
                                          error_message=f"boom {index}")
        self.assertEqual(notify.call_count, plugin.MAX_ERROR_NOTIFICATIONS)
        self.assertEqual(plugin._session_totals["errors"], 6)

    def test_counts_tools_without_an_api_key_and_sends_nothing(self):
        os.environ.pop("PUSHARY_API_KEY")
        with mock.patch.object(api, "send_notification", side_effect=AssertionError("must not call")):
            plugin._on_post_tool_call("terminal", {}, "x", 1, status="error", error_message="boom")
        self.assertEqual(plugin._session_totals["tools"], 1)


class DecisionFieldTest(unittest.TestCase):
    def setUp(self):
        reset_state()

    def test_a_camelcase_typo_is_rejected_rather_than_silently_dropped(self):
        with self.assertRaises(TypeError):
            api._with_decision_fields({}, {"toolName": "terminal"})

    def test_a_notification_may_not_send_fields_that_tool_does_not_accept(self):
        with self.assertRaises(TypeError):
            api._with_decision_fields({}, {"tool_name": "terminal"}, api.IDENTITY_PARAM_NAMES)

    def test_error_notifications_send_only_what_send_notification_accepts(self):
        os.environ["PUSHARY_API_KEY"] = "pk_test.sk_test"
        self.addCleanup(os.environ.pop, "PUSHARY_API_KEY", None)
        captured = {}

        def fake_notify(title, body, **kwargs):
            captured.update(kwargs)
            return {"sent": 1}

        with mock.patch.object(api, "send_notification", fake_notify):
            plugin._on_post_tool_call("terminal", {}, "x", 1, status="error", error_message="boom")
        self.assertEqual(
            set(captured) - {"agent_name", "context"},
            set(),
            "send_notification only takes agentName, context, sessionId and machineId",
        )

    def test_identity_is_attached_without_the_caller_asking(self):
        identity.remember_session("sess-1")
        params = api._with_decision_fields({}, {"tool_name": "terminal"})
        self.assertEqual(params["toolName"], "terminal")
        self.assertEqual(params["sessionId"], "sess-1")
        self.assertTrue(params["machineId"])

    def test_an_explicit_session_wins_over_the_remembered_one(self):
        identity.remember_session("sess-1")
        params = api._with_decision_fields({}, {"session_id": "sess-2"})
        self.assertEqual(params["sessionId"], "sess-2")

    def test_tools_forward_decision_fields_from_their_schema_names(self):
        captured = {}
        with mock.patch.object(api, "ask_user", side_effect=lambda *a, **kw: captured.update(kw) or {"correlationId": "c1", "answered": True}):
            tools.pushary_ask({"question": "?", "tool_name": "terminal", "intent": "ship it"})
        self.assertEqual(captured["tool_name"], "terminal")
        self.assertEqual(captured["intent"], "ship it")

    def test_propose_scope_refuses_without_a_session_rather_than_faking_one(self):
        result = json.loads(tools.pushary_propose_scope({"done_when": "tests pass"}))
        self.assertIn("error", result)

    def test_propose_scope_uses_the_running_hermes_session(self):
        identity.remember_session("sess-9")
        captured = {}
        with mock.patch.object(api, "propose_scope", side_effect=lambda *a, **kw: captured.update(done=a, kw=kw) or {"ratified": True}):
            result = json.loads(tools.pushary_propose_scope({"done_when": "tests pass"}))
        self.assertTrue(result["ratified"])
        self.assertEqual(captured["done"], ("tests pass", "sess-9"))


if __name__ == "__main__":
    unittest.main()
