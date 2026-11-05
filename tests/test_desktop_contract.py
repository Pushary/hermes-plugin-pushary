import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from pushary_plugin import approval_ledger as ledger
from pushary_plugin import cancellation
from pushary_plugin import desktop_answers
from pushary_plugin import desktop_contract as contract

FIXTURES = Path(__file__).resolve().parents[1] / "desktop-src" / "contract-fixtures"
UPDATE_FIXTURES = os.environ.get("PUSHARY_UPDATE_CONTRACT_FIXTURES") == "1"

NOW_MS = 1_793_900_000_000
OWNER_PID = 4242
CHOICES = ledger.offered_choices((("once", "Allow once"), ("session", "Allow for this session"),
                                  ("always", "Always allow"), ("deny", "Deny")))
CONFIRM = ledger.offered_choices((("once", "Allow once"), ("deny", "Deny")))

WITHDRAWN_HERE = {"cancelled": True, "cancelledByThisCall": True, "correlationId": "x"}
CANCELLED_BEFORE = {"cancelled": True, "cancelledByThisCall": False, "correlationId": "x"}
CANCELLED_BY_AN_OLD_SERVER = {"cancelled": True, "correlationId": "x"}
NOT_PENDING = {"cancelled": False, "cancelledByThisCall": False, "correlationId": "x"}
UNAVAILABLE = {"cancelled": False, "status": "unavailable", "handoffAction": "stop", "correlationId": "x"}


def correlation(index):
    return f"6f1c2d3e-4b5a-4c6d-8e7f-{index:012d}"


def request(index):
    return f"req{index:029d}"


def owner_running(pid):
    return pid == OWNER_PID


def subject(index):
    return f"git push --force origin release-{index}"


def open_approval(home, index, choices=CHOICES, opened_offset_ms=0):
    ledger.open_approval(home, correlation_id=correlation(index), request_id=request(index), subject=subject(index),
                         choices=choices, pid=OWNER_PID, opened_at_ms=NOW_MS - 60_000 + opened_offset_ms,
                         deadline_at_ms=NOW_MS + 240_000)


def fresh_answers():
    return desktop_answers.DesktopAnswers(threading.Condition(), {})


def open_here(answers, home, index, choices=CHOICES, opened_offset_ms=0, answerable_here=True, routed_to=None):
    open_approval(home, index, choices=choices, opened_offset_ms=opened_offset_ms)
    answers.open_slot(correlation(index), request_id=request(index), subject=subject(index), choices=choices,
                      opened_at_ms=NOW_MS - 60_000 + opened_offset_ms, deadline_at_ms=NOW_MS + 240_000,
                      answerable_here=answerable_here, routed_to=routed_to)


def every_wire_variant(home):
    answers = fresh_answers()
    open_here(answers, home, 1)
    for index, state in ((2, ledger.CLAIMING), (3, ledger.WON), (4, ledger.LOST)):
        open_here(answers, home, index, choices=CONFIRM, opened_offset_ms=5_000 * index)
        answers.claim(correlation(index), "once", now_ms=NOW_MS - 1_000)
        if state == ledger.WON:
            answers.decide_from_desktop(correlation(index), "once")
        if state == ledger.LOST:
            answers.lose_claim(correlation(index))
    open_here(answers, home, 5, opened_offset_ms=30_000, answerable_here=False, routed_to="Sam Patel")
    open_approval(home, 6, opened_offset_ms=35_000)
    settled = ((10, "once", "mobile_app"), (11, "session", ledger.DESKTOP_ANSWER_SOURCE), (12, "always", "mac_app"),
               (13, "deny", ledger.UNNAMED_ANSWER_SOURCE), (14, ledger.HANDED_TO_HERMES, None))
    for index, outcome, source in settled:
        open_approval(home, index)
        ledger.settle_approval(home, correlation(index), outcome=outcome, answer_source=source,
                               settled_at_ms=NOW_MS - 1_000 * index)
    open_approval(home, 15)
    ledger.record_host_outcome(home, request(15), "transport_timeout", NOW_MS - 15_500)
    recorded = ledger.read_approvals(home, NOW_MS, owner_running)
    return contract.view_with_answers_here(recorded, answers.waiting_here(NOW_MS))


def golden(name, value):
    path = FIXTURES / name
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if UPDATE_FIXTURES:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
    return path.read_text() == rendered


class DesktopContractTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="pushary-contract-")
        self.home = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def test_the_approvals_response_matches_the_fixture_the_desktop_half_is_tested_against(self):
        setup = contract.setup_wire(api_key_saved=True, transport_selected=False, plugin_version="fixture")
        response = contract.approvals_response(every_wire_variant(self.home), setup)
        self.assertTrue(golden("approvals-response.json", response),
                        "Run with PUSHARY_UPDATE_CONTRACT_FIXTURES=1 after a deliberate contract change")

    def test_every_answer_outcome_has_a_fixture(self):
        responses = [contract.answer_response(outcome) for outcome in contract.ANSWER_OUTCOMES]
        self.assertTrue(golden("answer-responses.json", responses))

    def test_the_names_and_enumerations_the_desktop_half_mirrors_match_the_fixture(self):
        self.assertTrue(golden("contract-facts.json", contract.contract_facts()))

    def test_every_variant_is_present_in_the_fixture_scenario(self):
        view = every_wire_variant(self.home)
        self.assertEqual({item.claim for item in view.waiting}, {None, *ledger.CLAIM_STATES})
        self.assertEqual({item.answerable_here for item in view.waiting}, {True, False})
        self.assertEqual({item.routed_to for item in view.waiting}, {None, "Sam Patel"})
        self.assertEqual([item.correlation_id for item in view.waiting], [correlation(index) for index in range(1, 7)])
        self.assertEqual({item.outcome for item in view.recent}, set(ledger.RECENT_OUTCOMES))
        self.assertEqual(len(view.recent), ledger.RECENT_LIMIT)

    def test_the_wire_event_is_the_name_hermes_broadcasts(self):
        self.assertEqual(contract.APPROVALS_CHANGED_WIRE_EVENT, f"plugin.pushary.{contract.APPROVALS_CHANGED_EVENT}")

    def test_an_answer_request_is_exactly_one_offered_kind_of_choice(self):
        self.assertEqual(contract.parse_answer_request({"choice": "once"}), "once")
        for body in (None, [], "once", {}, {"choice": "yes"}, {"choice": 1}, {"choice": "once", "extra": True},
                     {"choice": None}):
            self.assertIsNone(contract.parse_answer_request(body), body)


class CancellationTest(unittest.TestCase):
    def test_only_a_cancel_the_server_attributes_to_this_call_is_a_withdrawal_by_it(self):
        self.assertEqual(cancellation.classify_cancellation(WITHDRAWN_HERE), cancellation.WITHDRAWN_BY_THIS_CALL)
        self.assertEqual(cancellation.classify_cancellation(CANCELLED_BEFORE), cancellation.ALREADY_CANCELLED)
        self.assertEqual(cancellation.classify_cancellation(CANCELLED_BY_AN_OLD_SERVER), cancellation.CANCELLED_UNPROVEN)
        self.assertEqual(cancellation.classify_cancellation(NOT_PENDING), cancellation.NOT_PENDING)
        self.assertEqual(cancellation.classify_cancellation({"cancelled": False}), cancellation.NOT_PENDING)

    def test_anything_unreadable_is_unreachable_never_a_closed_question(self):
        for response in (UNAVAILABLE, {"error": "HTTP 503"}, {"raw": "Error: Question does not belong to this site"},
                         None, "cancelled", [], {"cancelled": "true"}, {"cancelled": True, "cancelledByThisCall": "yes"},
                         {"cancelled": False, "cancelledByThisCall": True}, {"cancelled": True, "handoffAction": "stop"}):
            self.assertEqual(cancellation.classify_cancellation(response), cancellation.UNREACHABLE, response)


class AnswerFromDesktopTest(unittest.TestCase):
    def setUp(self):
        self.answers = fresh_answers()
        self.open(1)
        self.cancelled = []

    def open(self, index, choices=CHOICES, answerable_here=True, routed_to=None):
        self.answers.open_slot(correlation(index), request_id=request(index), subject=subject(index), choices=choices,
                               opened_at_ms=NOW_MS - 60_000, deadline_at_ms=NOW_MS + 240_000,
                               answerable_here=answerable_here, routed_to=routed_to)

    def answer(self, response, choice="once", index=1):
        def cancel(correlation_id):
            self.cancelled.append(correlation_id)
            if isinstance(response, Exception):
                raise response
            return response

        return contract.answer_from_desktop(self.answers, correlation(index), choice, cancel_question=cancel,
                                            now_ms=NOW_MS)

    def claim_state(self):
        return self.answers.waiting_here(NOW_MS)[0].claim

    def test_withdrawing_the_question_itself_answers_here(self):
        self.assertEqual(self.answer(WITHDRAWN_HERE)["outcome"], contract.ANSWERED_HERE)
        self.assertEqual(self.claim_state(), ledger.WON)
        self.assertEqual(self.answers.desktop_decision(correlation(1), ("once", "deny")), "once")
        self.assertEqual(self.cancelled, [correlation(1)])

    def test_a_question_someone_else_already_cancelled_is_never_answered_here(self):
        for response in (CANCELLED_BEFORE, CANCELLED_BY_AN_OLD_SERVER, NOT_PENDING):
            with self.subTest(response=response):
                self.setUp()
                self.assertEqual(self.answer(response)["outcome"], contract.ALREADY_CLOSED)
                self.assertEqual(self.claim_state(), ledger.LOST)
                self.assertIsNone(self.answers.desktop_decision(correlation(1), ("once", "deny")))

    def test_an_unreadable_or_failed_cancel_is_unreachable_and_never_counts(self):
        for response in (UNAVAILABLE, {"error": "HTTP 503"}, {"raw": "Error: nope"}, None, RuntimeError("offline")):
            with self.subTest(response=response):
                self.setUp()
                self.assertEqual(self.answer(response)["outcome"], contract.UNREACHABLE)
                self.assertEqual(self.claim_state(), ledger.LOST)
                self.assertIsNone(self.answers.desktop_decision(correlation(1), ("once", "deny")))

    def test_a_desktop_answer_after_hermes_took_the_final_say_is_refused_before_pushary_is_called(self):
        self.answers.take_final_say(correlation(1), ("once", "deny"), claim_wait_seconds=0)
        self.assertEqual(self.answer(WITHDRAWN_HERE)["outcome"], ledger.NOT_WAITING)
        self.assertEqual(self.cancelled, [])

    def test_the_final_say_taken_between_the_cancel_and_the_decision_wins(self):
        def cancel_then_hermes_decides(correlation_id):
            self.answers.take_final_say(correlation_id, ("once", "deny"), claim_wait_seconds=0)
            return WITHDRAWN_HERE

        outcome = contract.answer_from_desktop(self.answers, correlation(1), "once",
                                               cancel_question=cancel_then_hermes_decides, now_ms=NOW_MS)
        self.assertEqual(outcome["outcome"], contract.ALREADY_CLOSED)

    def test_a_refused_claim_never_calls_pushary(self):
        self.open(2, choices=CONFIRM)
        self.assertEqual(self.answer(WITHDRAWN_HERE, choice="always", index=2)["outcome"], ledger.NOT_OFFERED)
        self.assertEqual(self.answer(WITHDRAWN_HERE)["outcome"], contract.ANSWERED_HERE)
        self.assertEqual(self.answer(WITHDRAWN_HERE)["outcome"], ledger.NOT_WAITING)
        self.assertEqual(self.cancelled, [correlation(1)])

    def test_an_approval_routed_to_someone_else_is_refused_before_pushary_is_called(self):
        self.open(3, answerable_here=False, routed_to="Sam Patel")
        self.assertEqual(self.answer(WITHDRAWN_HERE, index=3)["outcome"], ledger.NOT_ANSWERABLE_HERE)
        self.assertEqual(self.cancelled, [])

    def test_an_approval_this_process_is_not_waiting_on_is_refused_before_pushary_is_called(self):
        self.assertEqual(self.answer(WITHDRAWN_HERE, index=9)["outcome"], ledger.NOT_FOUND)
        self.answers.end_for_request(request(1))
        self.assertEqual(self.answer(WITHDRAWN_HERE)["outcome"], ledger.NOT_WAITING)
        self.assertEqual(self.cancelled, [])

    def test_responses_carry_the_contract_version(self):
        self.assertEqual(self.answer(WITHDRAWN_HERE)["version"], contract.CONTRACT_VERSION)


if __name__ == "__main__":
    unittest.main()
