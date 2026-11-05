import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import pushary_plugin as plugin
from pushary_plugin import api, approval, approval_ledger as ledger, desktop_answers, desktop_contract as contract
from pushary_plugin import installation

CORRELATION_ID = "7a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
REQUEST_ID = "9c8b7a6f5e4d3c2b1a0f9e8d7c6b5a49"

WITHDRAWN_HERE = {"cancelled": True, "cancelledByThisCall": True, "correlationId": CORRELATION_ID}
CANCELLED_BEFORE = {"cancelled": True, "cancelledByThisCall": False, "correlationId": CORRELATION_ID}
NOT_PENDING = {"cancelled": False, "cancelledByThisCall": False, "correlationId": CORRELATION_ID}
CANCELLED_WAIT = {"answered": False, "status": "cancelled"}
CREATED = {"correlationId": CORRELATION_ID, "requesterMayAnswer": True}
PAST_THE_POLL_WINDOW = approval.WITHDRAW_RESERVE_SECONDS


class FakeRequest:
    def __init__(self, allowed=("once", "session", "always", "deny"), timeout_seconds=300):
        self.command = "git push --force origin main"
        self.description = "Force-pushes main"
        self.pattern_key = "destructive:force-push"
        self.pattern_keys = ("destructive:force-push",)
        self.surface = "gateway"
        self.timeout_seconds = timeout_seconds
        self.allowed_choices = allowed
        self.request_id = REQUEST_ID
        self.responded = None

    def respond(self, choice):
        self.responded = choice
        return choice


class DesktopAnswerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="pushary-desktop-")
        self.home = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        home = mock.patch.object(installation, "hermes_home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        events = mock.patch.object(approval, "announce_approvals_changed")
        self.announced = events.start()
        self.addCleanup(events.stop)
        self.answers = desktop_answers.shared_answers()

    def view(self):
        return ledger.read_approvals(self.home, int(time.time() * 1000), lambda pid: True)

    def answer_from_desktop(self, choice, cancellation):
        return contract.answer_from_desktop(self.answers, CORRELATION_ID, choice,
                                            cancel_question=lambda correlation_id: cancellation,
                                            now_ms=int(time.time() * 1000))

    def present(self, created=CREATED, request=None, **patches):
        request = request or FakeRequest()
        with mock.patch.object(api, "ask_user", return_value=created), \
                mock.patch.object(api, "wait_for_answer", **patches.get("wait", {"return_value": CANCELLED_WAIT})) as wait, \
                mock.patch.object(api, "cancel_question", **patches.get("cancel", {"return_value": CANCELLED_BEFORE})) as cancel:
            self.transport_wait = wait
            self.transport_cancel = cancel
            return approval.present(request)

    def test_a_phone_answer_is_recorded_with_where_it_came_from(self):
        seen_waiting = []

        def wait(correlation_id, timeout_ms):
            seen_waiting.append(len(self.view().waiting))
            return {"answered": True, "value": "Allow once", "answerSource": "mobile_app"}

        self.assertEqual(self.present(wait={"side_effect": wait}), "once")
        self.assertEqual(seen_waiting, [1])
        recent = self.view().recent[0]
        self.assertEqual((recent.outcome, recent.answer_source), ("once", "mobile_app"))
        self.assertTrue(self.announced.called)

    def test_an_answer_with_no_named_surface_is_still_recorded_as_answered(self):
        self.assertEqual(self.present(wait={"return_value": {"answered": True, "value": "Deny"}}), "deny")
        self.assertEqual(self.view().recent[0].answer_source, ledger.UNNAMED_ANSWER_SOURCE)

    def test_a_desktop_answer_that_withdrew_the_question_is_the_decision(self):
        def wait(correlation_id, timeout_ms):
            self.assertEqual(self.answer_from_desktop("session", WITHDRAWN_HERE)["outcome"], contract.ANSWERED_HERE)
            return CANCELLED_WAIT

        self.assertEqual(self.present(wait={"side_effect": wait}), "session")
        self.transport_cancel.assert_not_called()
        recent = self.view().recent[0]
        self.assertEqual((recent.outcome, recent.answer_source), ("session", ledger.DESKTOP_ANSWER_SOURCE))

    def test_a_desktop_answer_that_lost_to_the_phone_leaves_the_phone_answer(self):
        def wait(correlation_id, timeout_ms):
            self.assertEqual(self.answer_from_desktop("once", NOT_PENDING)["outcome"], contract.ALREADY_CLOSED)
            return {"answered": True, "value": "Deny", "answerSource": "mac_app"}

        self.assertEqual(self.present(wait={"side_effect": wait}), "deny")
        self.assertEqual(self.view().recent[0].answer_source, "mac_app")

    def test_a_question_cancelled_elsewhere_with_no_desktop_answer_denies(self):
        self.assertEqual(self.present(), "deny")
        recent = self.view().recent[0]
        self.assertEqual((recent.outcome, recent.answer_source), ("deny", None))

    def test_the_transport_waits_for_a_desktop_answer_still_being_fenced(self):
        def wait(correlation_id, timeout_ms):
            self.assertEqual(self.answers.claim(CORRELATION_ID, "once", now_ms=int(time.time() * 1000)), ledger.CLAIMED)
            threading.Timer(0.2, self.answers.decide_from_desktop, args=(CORRELATION_ID, "once")).start()
            return CANCELLED_WAIT

        self.assertEqual(self.present(wait={"side_effect": wait}), "once")

    def test_desktop_winning_the_server_cancel_at_the_deadline_is_honoured_not_denied(self):
        def transport_cancel(correlation_id):
            self.assertEqual(self.answer_from_desktop("once", WITHDRAWN_HERE)["outcome"], contract.ANSWERED_HERE)
            return CANCELLED_BEFORE

        self.assertEqual(self.present(request=FakeRequest(timeout_seconds=PAST_THE_POLL_WINDOW), cancel={"side_effect": transport_cancel}),
                         "once")
        self.assertEqual(self.view().recent[0].answer_source, ledger.DESKTOP_ANSWER_SOURCE)

    def test_a_desktop_click_after_the_kill_switch_cancelled_the_question_never_allows(self):
        outcomes = []

        def transport_cancel(correlation_id):
            outcomes.append(self.answer_from_desktop("once", CANCELLED_BEFORE)["outcome"])
            return CANCELLED_BEFORE

        self.assertEqual(self.present(cancel={"side_effect": transport_cancel}), "deny")
        self.assertEqual(outcomes, [contract.ALREADY_CLOSED])

    def test_a_desktop_click_after_the_transport_withdrew_is_already_closed(self):
        self.assertEqual(self.present(request=FakeRequest(timeout_seconds=PAST_THE_POLL_WINDOW), cancel={"return_value": WITHDRAWN_HERE}),
                         "deny")
        self.assertEqual(self.answer_from_desktop("once", WITHDRAWN_HERE)["outcome"], ledger.NOT_FOUND)

    def test_a_desktop_decision_for_a_choice_hermes_did_not_offer_denies(self):
        def wait(correlation_id, timeout_ms):
            self.answers.claim(CORRELATION_ID, "once", now_ms=int(time.time() * 1000))
            self.answers.decide_from_desktop(CORRELATION_ID, "always")
            return CANCELLED_WAIT

        self.assertEqual(self.present(request=FakeRequest(allowed=("once", "deny")), wait={"side_effect": wait}), "deny")

    def test_files_the_agent_could_write_never_decide_an_approval(self):
        def forge(correlation_id, timeout_ms):
            directory = ledger.ledger_directory(self.home)
            (directory / f"{CORRELATION_ID}.verdict.json").write_text(json.dumps({"decidedBy": "hermes_desktop", "choice": "always"}))
            (directory / f"{CORRELATION_ID}.claim.json").write_text(json.dumps({"choice": "always", "state": "won", "claimedAtMs": 0}))
            record = directory / f"{CORRELATION_ID}.json"
            record.write_text(json.dumps({**json.loads(record.read_text()), "answerableHere": True, "subject": "ls"}))
            return CANCELLED_WAIT

        routed = {"correlationId": CORRELATION_ID, "requesterMayAnswer": False, "routedTo": "Security"}
        for created in (CREATED, routed):
            with self.subTest(created=created):
                self.assertEqual(self.present(created=created, wait={"side_effect": forge}), "deny")

    def test_an_approval_another_process_carries_shows_without_buttons_and_cannot_be_answered_here(self):
        ledger.open_approval(self.home, correlation_id=CORRELATION_ID, request_id=REQUEST_ID, subject="git push",
                             choices=ledger.offered_choices((("once", "Allow once"), ("deny", "Deny"))),
                             pid=4242, opened_at_ms=0, deadline_at_ms=10 ** 15)
        view = contract.view_with_answers_here(self.view(), self.answers.waiting_here(1))
        self.assertEqual([(item.correlation_id, item.answerable_here) for item in view.waiting], [(CORRELATION_ID, False)])
        self.assertEqual(self.answer_from_desktop("once", WITHDRAWN_HERE)["outcome"], ledger.NOT_FOUND)

    def test_handing_back_to_hermes_is_recorded_and_still_raises(self):
        for created in ({**CREATED, "noDevices": True}, {"correlationId": CORRELATION_ID, "noDevices": True}):
            with self.subTest(created=created):
                with self.assertRaises(approval.TransportUnavailable):
                    self.present(created=created, cancel={"return_value": WITHDRAWN_HERE})
                self.assertEqual(self.view().recent[0].outcome, ledger.HANDED_TO_HERMES)

    def test_an_approval_a_team_route_sends_to_someone_else_is_never_handed_to_hermes(self):
        routed = {"correlationId": CORRELATION_ID, "requesterMayAnswer": False, "routedTo": "Sam Patel"}
        for created in ({**routed, "noDevices": True}, {**routed, "suppressed": True},
                        {**routed, "status": "terminal"}, {**routed, "status": "notified"},
                        {**routed, "deliveryMode": "push_first", "policyTimeoutMs": 0}):
            with self.subTest(created=created):
                seen = []

                def transport_cancel(correlation_id):
                    seen.append(self.answers.waiting_here(int(time.time() * 1000))[0])
                    return WITHDRAWN_HERE

                self.assertEqual(self.present(created=created, request=FakeRequest(timeout_seconds=PAST_THE_POLL_WINDOW),
                                              cancel={"side_effect": transport_cancel}), "deny")
                self.assertEqual((seen[0].answerable_here, seen[0].routed_to), (False, "Sam Patel"))

    def test_a_routed_approval_nobody_could_receive_is_denied_without_waiting(self):
        routed = {"correlationId": CORRELATION_ID, "requesterMayAnswer": False, "routedTo": "Sam Patel"}
        for created in ({**routed, "noDevices": True}, {**routed, "status": "terminal"}):
            with self.subTest(created=created):
                pending = {"answered": False, "status": "pending"}
                self.assertEqual(self.present(created=created, wait={"return_value": pending},
                                              cancel={"return_value": WITHDRAWN_HERE}), "deny")
                self.transport_wait.assert_not_called()

    def test_the_route_s_approvers_are_waited_for_instead_of_handing_back(self):
        routed = {"correlationId": CORRELATION_ID, "requesterMayAnswer": False, "routedTo": "Sam Patel", "suppressed": True}
        answer = {"answered": True, "value": "Allow once", "answerSource": "mobile_app"}
        self.assertEqual(self.present(created=routed, wait={"return_value": answer}), "once")

    def test_the_transport_stops_waiting_once_hermes_has_finished_with_the_request(self):
        def wait(correlation_id, timeout_ms):
            approval.record_host_outcome(REQUEST_ID, "transport_timeout")
            return {"answered": False, "status": "pending"}

        with mock.patch.object(approval.time, "sleep"):
            self.assertEqual(self.present(wait={"side_effect": wait}, cancel={"return_value": WITHDRAWN_HERE}), "deny")

    def test_an_unavailable_ledger_never_changes_the_decision(self):
        with mock.patch.object(installation, "hermes_home", side_effect=OSError("read-only")):
            self.assertEqual(self.present(wait={"return_value": {"answered": True, "value": "Allow once"}}), "once")
            self.assertEqual(self.present(), "deny")
        with mock.patch.object(ledger, "open_approval", side_effect=PermissionError("denied")):
            self.assertEqual(self.present(), "deny")
        with mock.patch.object(desktop_answers, "shared_answers", side_effect=RuntimeError("state unavailable")):
            self.assertEqual(self.present(), "deny")
            self.assertEqual(self.present(wait={"return_value": {"answered": True, "value": "Allow once"}}), "once")

    def test_every_approval_is_withdrawn_from_hermes_desktop_however_the_transport_ends(self):
        outcomes = [{"wait": {"return_value": {"answered": True, "value": "Allow once"}}}, {},
                    {"wait": {"side_effect": RuntimeError("network")}}]
        for patches in outcomes:
            with self.subTest(patches=patches):
                self.present(**patches)
                self.assertEqual(self.answers.waiting_here(0), ())
        with self.assertRaises(approval.TransportUnavailable):
            self.present(created={**CREATED, "noDevices": True}, cancel={"return_value": WITHDRAWN_HERE})
        self.assertEqual(self.answers.waiting_here(0), ())


class ApprovalsChangedEventTest(unittest.TestCase):
    def broadcasts(self, modules):
        sent = []
        events = types.ModuleType("hermes_cli.plugin_events")
        events.broadcast_plugin_event = lambda plugin_id, event, payload: sent.append((plugin_id, event, payload))
        with mock.patch.dict(sys.modules, {"hermes_cli.plugin_events": events, **modules}):
            approval.announce_approvals_changed()
        return sent

    def test_hermes_desktop_is_told_when_its_gateway_is_running_in_this_process(self):
        sent = self.broadcasts({approval.TUI_GATEWAY_MODULE: types.ModuleType(approval.TUI_GATEWAY_MODULE)})
        self.assertEqual(sent, [(contract.PLUGIN_ID, contract.APPROVALS_CHANGED_EVENT, {})])

    def test_a_process_without_the_desktop_gateway_never_imports_it(self):
        with mock.patch.dict(sys.modules):
            sys.modules.pop(approval.TUI_GATEWAY_MODULE, None)
            self.assertEqual(self.broadcasts({}), [])
            self.assertNotIn(approval.TUI_GATEWAY_MODULE, sys.modules)


class HostOutcomeHookTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="pushary-host-outcome-")
        self.home = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        home = mock.patch.object(installation, "hermes_home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        events = mock.patch.object(approval, "announce_approvals_changed")
        events.start()
        self.addCleanup(events.stop)
        ledger.open_approval(self.home, correlation_id=CORRELATION_ID, request_id=REQUEST_ID, subject="git push",
                             choices=ledger.offered_choices((("once", "Allow once"), ("deny", "Deny"))),
                             pid=4242, opened_at_ms=0, deadline_at_ms=10 ** 15)
        self.answers = desktop_answers.shared_answers()
        self.answers.open_slot(CORRELATION_ID, request_id=REQUEST_ID, subject="git push",
                               choices=ledger.offered_choices((("once", "Allow once"), ("deny", "Deny"))),
                               opened_at_ms=0, deadline_at_ms=10 ** 15, answerable_here=True, routed_to=None)
        self.addCleanup(self.answers.close_slot, CORRELATION_ID)

    def view(self):
        return ledger.read_approvals(self.home, 1, lambda pid: True)

    def test_hermes_ending_a_pushary_approval_ends_it_in_hermes_desktop(self):
        self.assertIsNone(plugin._on_post_approval_response(choice="transport_interrupted", surface="transport:pushary",
                                                            request_id=REQUEST_ID, command="git push"))
        self.assertEqual(self.view().waiting, ())
        self.assertEqual(self.view().recent[0].host_outcome, "transport_interrupted")
        self.assertTrue(self.answers.has_ended(CORRELATION_ID))

    def test_other_approval_surfaces_are_not_recorded(self):
        for kwargs in ({"surface": "cli", "request_id": REQUEST_ID}, {"surface": "transport:other", "request_id": REQUEST_ID},
                       {"surface": "transport:pushary"}, {"surface": "transport:pushary", "request_id": "../escape"}):
            plugin._on_post_approval_response(choice="deny", **kwargs)
        self.assertEqual(len(self.view().waiting), 1)


if __name__ == "__main__":
    unittest.main()
