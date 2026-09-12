import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import pushary_plugin
from pushary_plugin import api, schemas, tools


class PackageMetadataTests(unittest.TestCase):
    def test_embedded_plugin_version_matches_distribution(self):
        root = Path(__file__).parent
        project = (root / "pyproject.toml").read_text()
        plugin = (root / "pushary_plugin" / "plugin.yaml").read_text()

        self.assertEqual(
            re.search(r'^version = "([^"]+)"$', project, re.MULTILINE).group(1),
            re.search(r"^version: (\S+)$", plugin, re.MULTILINE).group(1),
        )


class PublishedSkillTests(unittest.TestCase):
    def test_documented_json_examples_match_native_schema_and_preserve_fields(self):
        skill = Path(__file__).parent / "pushary_plugin/skills/pushary/SKILL.md"
        examples = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", skill.read_text(), re.S)]
        self.assertEqual(len(examples), 2)
        notify, ask = examples
        for example, schema in ((notify, schemas.PUSHARY_NOTIFY), (ask, schemas.PUSHARY_ASK)):
            params = schema["parameters"]
            self.assertFalse(set(example) - set(params["properties"]))
            self.assertTrue(set(params["required"]) <= set(example))
            for name, value in example.items():
                field = params["properties"][name]
                expected = {"string": str, "array": list, "integer": int}[field["type"]]
                self.assertIsInstance(value, expected)
                if "enum" in field:
                    self.assertIn(value, field["enum"])
        with patch.object(api, "send_notification", return_value={}) as send:
            tools.pushary_notify(notify)
        sent = send.call_args.kwargs
        self.assertEqual(sent["agent_name"], "Hermes - daily-briefing")
        self.assertEqual(sent["context"], {
            "type": "task_complete",
            "summary": notify["summary"],
            "details": notify["details"],
            "nextSteps": notify["next_steps"],
        })
        with patch.object(api, "ask_user", return_value={"answered": True, "value": "yes"}) as send:
            tools.pushary_ask(ask)
        self.assertEqual(send.call_args.kwargs["agent_name"], "Hermes - server-maintenance")
        self.assertEqual(send.call_args.kwargs["timeout_ms"], 12000)

    def test_customer_answers_authorize_only_an_affirmative_confirm(self):
        for kind, value, approved in (("confirm", "yes", True), ("confirm", "no", False), ("select", "yes", False), ("input", "yes", False)):
            with self.subTest(kind=kind, value=value), patch.object(api, "create_end_user_decision", return_value={
                "decisionId": "d1", "status": "answered", "value": value,
            }), patch.object(api, "get_end_user_decision") as poll:
                result = json.loads(tools.pushary_ask_end_user({
                    "question": "Test answer", "external_id": "customer-1", "type": kind,
                }))
            self.assertEqual(result["value"], value)
            self.assertTrue(result["answered"])
            self.assertEqual(result["approved"], approved)
            poll.assert_not_called()


class PusharyAskTests(unittest.TestCase):
    def test_wait_false_is_forwarded_and_returns_without_polling(self):
        pending = {"correlationId": "q1", "answered": False, "status": "pending"}
        with patch.object(tools.api, "ask_user", return_value=pending) as ask, patch.object(
            tools.api, "wait_for_answer"
        ) as poll:
            result = json.loads(tools.pushary_ask({
                "question": "Deploy?",
                "wait": False,
                "timeout_ms": 12000,
            }))

        ask.assert_called_once_with(
            "Deploy?",
            question_type="confirm",
            options=None,
            placeholder=None,
            context=None,
            agent_name=None,
            wait=False,
            timeout_ms=12000,
        )
        poll.assert_not_called()
        self.assertEqual(result, pending)

    def test_polls_once_and_preserves_the_server_fallback(self):
        pending = {"correlationId": "q1", "answered": False, "status": "pending"}
        fallback = {
            "answered": False,
            "status": "pending",
            "nextAction": "ask_in_current_client",
            "handoffAction": "cancel_then_ask_in_current_client",
            "hint": "cancel first",
        }
        with patch.object(tools.api, "ask_user", return_value=pending), patch.object(
            tools.api, "wait_for_answer", return_value=fallback
        ) as wait:
            result = json.loads(tools.pushary_ask({"question": "Deploy?"}))

        wait.assert_called_once_with("q1", 30000)
        self.assertEqual(result["nextAction"], "ask_in_current_client")
        self.assertEqual(result["handoffAction"], "cancel_then_ask_in_current_client")

    def test_preserves_immediate_server_actions_without_polling(self):
        for next_action in ("stop", "cancel_then_ask_in_current_client"):
            with self.subTest(next_action=next_action), patch.object(
                tools.api,
                "ask_user",
                return_value={
                    "correlationId": "q1",
                    "answered": False,
                    "status": "pending",
                    "handoffAction": next_action,
                },
            ), patch.object(tools.api, "wait_for_answer") as wait:
                result = json.loads(tools.pushary_ask({"question": "Deploy?"}))

            wait.assert_not_called()
            self.assertEqual(result["handoffAction"], next_action)


class PusharyApiTests(unittest.TestCase):
    def test_ask_user_sends_wait_and_timeout_to_mcp(self):
        with patch.object(api, "_mcp_call", return_value={}) as call, patch.object(
            api, "_with_identity", side_effect=lambda params: params
        ):
            api.ask_user("Deploy?", wait=False, timeout_ms=12000)

        call.assert_called_once_with("ask_user", {
            "question": "Deploy?",
            "type": "confirm",
            "wait": False,
            "timeoutMs": 12000,
        })

    def test_wait_for_answer_clamps_timeout_to_the_server_contract(self):
        with patch.object(api, "_mcp_call", return_value={}) as call:
            api.wait_for_answer("q1", 500)

        call.assert_called_once_with("wait_for_answer", {
            "correlationId": "q1",
            "timeoutMs": 1000,
        })


class PusharySchemaTests(unittest.TestCase):
    def test_tool_descriptions_expose_the_bounded_handoff_contract(self):
        ask = schemas.PUSHARY_ASK["description"]
        wait = schemas.PUSHARY_WAIT["description"]

        self.assertIn("handoffAction", ask)
        self.assertIn("at most one", ask)
        self.assertIn("wait=false", wait)
        self.assertIn("Do not loop", wait)


class PusharyGateTests(unittest.TestCase):
    def test_configured_gate_blocks_when_no_api_key_is_available(self):
        with patch.dict(os.environ, {"PUSHARY_GATE_TOOLS": "shell"}, clear=True):
            result = pushary_plugin._on_pre_tool_call(tool_name="shell", args={"cmd": "deploy"})

        self.assertEqual(result["action"], "block")

    def test_gate_blocks_when_the_approval_service_errors(self):
        env = {"PUSHARY_GATE_TOOLS": "shell", "PUSHARY_API_KEY": "test-key"}
        failures = (
            json.dumps({"error": "service unavailable"}),
            json.dumps({"answered": False, "status": "pending"}),
        )
        for failure in failures:
            with self.subTest(failure=failure), patch.dict(os.environ, env), patch.object(
                pushary_plugin.tools, "pushary_ask", return_value=failure
            ):
                result = pushary_plugin._on_pre_tool_call(tool_name="shell", args={"cmd": "deploy"})

            self.assertEqual(result["action"], "block")

    def test_gate_blocks_when_cancellation_reconciliation_errors(self):
        handoff = json.dumps({
            "correlationId": "q1",
            "answered": False,
            "status": "pending",
            "handoffAction": "cancel_then_ask_in_current_client",
        })
        env = {"PUSHARY_GATE_TOOLS": "shell", "PUSHARY_API_KEY": "test-key"}
        with patch.dict(os.environ, env), patch.object(
            pushary_plugin.tools, "pushary_ask", return_value=handoff
        ), patch.object(
            pushary_plugin.api, "cancel_question", return_value={"cancelled": False}
        ), patch.object(
            pushary_plugin.api, "wait_for_answer", side_effect=RuntimeError("service unavailable")
        ):
            result = pushary_plugin._on_pre_tool_call(tool_name="shell", args={"cmd": "deploy"})

        self.assertEqual(result["action"], "block")

    def test_stop_handoff_blocks_without_cancelling_or_polling(self):
        stopped = json.dumps({
            "correlationId": "q1",
            "answered": False,
            "status": "unavailable",
            "handoffAction": "stop",
        })
        env = {"PUSHARY_GATE_TOOLS": "shell", "PUSHARY_API_KEY": "test-key"}
        with patch.dict(os.environ, env), patch.object(
            pushary_plugin.tools, "pushary_ask", return_value=stopped
        ), patch.object(pushary_plugin.api, "cancel_question") as cancel, patch.object(
            pushary_plugin.api, "wait_for_answer"
        ) as poll:
            result = pushary_plugin._on_pre_tool_call(tool_name="shell", args={"cmd": "deploy"})

        cancel.assert_not_called()
        poll.assert_not_called()
        self.assertEqual(result["action"], "block")

    def test_immediate_handoff_cancels_before_blocking_the_gated_tool(self):
        handoff = json.dumps({
            "correlationId": "q1",
            "answered": False,
            "status": "pending",
            "handoffAction": "cancel_then_ask_in_current_client",
        })
        env = {"PUSHARY_GATE_TOOLS": "shell", "PUSHARY_API_KEY": "test-key"}
        with patch.dict(os.environ, env), patch.object(
            pushary_plugin.tools, "pushary_ask", return_value=handoff
        ) as ask, patch.object(
            pushary_plugin.api, "cancel_question", return_value={"cancelled": True}
        ) as cancel, patch.object(pushary_plugin.api, "wait_for_answer") as poll:
            result = pushary_plugin._on_pre_tool_call(tool_name="shell", args={"cmd": "deploy"})

        ask.assert_called_once()
        cancel.assert_called_once_with("q1")
        poll.assert_not_called()
        self.assertEqual(result["action"], "block")
        self.assertIn("blocked", result["message"])

    def test_cancellation_race_honors_the_winning_phone_approval(self):
        handoff = json.dumps({
            "correlationId": "q1",
            "answered": False,
            "status": "pending",
            "handoffAction": "cancel_then_ask_in_current_client",
        })
        env = {"PUSHARY_GATE_TOOLS": "shell", "PUSHARY_API_KEY": "test-key"}
        with patch.dict(os.environ, env), patch.object(
            pushary_plugin.tools, "pushary_ask", return_value=handoff
        ), patch.object(
            pushary_plugin.api, "cancel_question", return_value={"cancelled": False}
        ), patch.object(
            pushary_plugin.api,
            "wait_for_answer",
            return_value={"answered": True, "status": "answered", "value": "yes"},
        ) as poll:
            result = pushary_plugin._on_pre_tool_call(tool_name="shell", args={"cmd": "deploy"})

        poll.assert_called_once_with("q1", 1000)
        self.assertIsNone(result)

    def test_terminal_cancellation_handoff_blocks_without_polling(self):
        handoff = json.dumps({
            "correlationId": "q1",
            "answered": False,
            "status": "pending",
            "handoffAction": "cancel_then_ask_in_current_client",
        })
        env = {"PUSHARY_GATE_TOOLS": "shell", "PUSHARY_API_KEY": "test-key"}
        with patch.dict(os.environ, env), patch.object(
            pushary_plugin.tools, "pushary_ask", return_value=handoff
        ), patch.object(
            pushary_plugin.api,
            "cancel_question",
            return_value={"cancelled": False, "status": "unavailable", "handoffAction": "stop"},
        ), patch.object(pushary_plugin.api, "wait_for_answer") as poll:
            result = pushary_plugin._on_pre_tool_call(tool_name="shell", args={"cmd": "deploy"})

        poll.assert_not_called()
        self.assertEqual(result["action"], "block")


if __name__ == "__main__":
    unittest.main()
