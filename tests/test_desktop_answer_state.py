import importlib.util
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from pushary_plugin import approval_ledger as ledger
from pushary_plugin import desktop_answers

CORRELATION_ID = "0b3f7a52-6c1d-4e8a-9f20-5d7c1b2a3e4f"
REQUEST_ID = "4f1d2c3b4a5e6f708192a3b4c5d6e7f8"
CHOICES = ledger.offered_choices((("once", "Allow once"), ("session", "Allow for this session"), ("deny", "Deny")))
OFFERED = ("once", "session", "deny")
NOW_MS = 1_793_900_000_000
PLUGIN_DIRECTORY = Path(__file__).resolve().parents[1] / "pushary_plugin"


def fresh_answers():
    return desktop_answers.DesktopAnswers(threading.Condition(), {})


class DesktopAnswerStateTest(unittest.TestCase):
    def setUp(self):
        self.answers = fresh_answers()

    def open(self, correlation_id=CORRELATION_ID, answerable_here=True, routed_to=None, deadline_at_ms=NOW_MS + 60_000):
        self.answers.open_slot(correlation_id, request_id=REQUEST_ID, subject="git push --force", choices=CHOICES,
                               opened_at_ms=NOW_MS - 1_000, deadline_at_ms=deadline_at_ms,
                               answerable_here=answerable_here, routed_to=routed_to)

    def claim(self, choice="once", now_ms=NOW_MS):
        return self.answers.claim(CORRELATION_ID, choice, now_ms=now_ms)

    def final_say(self, wait=3.0):
        return self.answers.take_final_say(CORRELATION_ID, OFFERED, claim_wait_seconds=wait)

    def test_only_an_approval_this_process_is_waiting_on_can_be_claimed(self):
        self.assertEqual(self.claim(), ledger.NOT_FOUND)
        self.open()
        self.assertEqual(self.claim("always"), ledger.NOT_OFFERED)
        self.assertEqual(self.claim("once"), ledger.CLAIMED)
        self.assertEqual(self.claim("deny"), ledger.ALREADY_CLAIMED)
        self.answers.close_slot(CORRELATION_ID)
        self.assertEqual(self.claim(), ledger.NOT_FOUND)

    def test_an_approval_past_its_deadline_or_ended_by_hermes_cannot_be_claimed(self):
        self.open()
        self.assertEqual(self.claim(now_ms=NOW_MS + 60_000), ledger.NOT_WAITING)
        self.answers.end_for_request(REQUEST_ID)
        self.assertTrue(self.answers.has_ended(CORRELATION_ID))
        self.assertEqual(self.claim(), ledger.NOT_WAITING)
        self.assertEqual(self.answers.waiting_here(NOW_MS), ())

    def test_an_approval_a_team_route_sends_to_someone_else_cannot_be_claimed_or_decided_here(self):
        self.open(answerable_here=False, routed_to="Sam Patel")
        waiting = self.answers.waiting_here(NOW_MS)[0]
        self.assertEqual((waiting.answerable_here, waiting.routed_to), (False, "Sam Patel"))
        self.assertEqual(self.claim(), ledger.NOT_ANSWERABLE_HERE)
        self.assertFalse(self.answers.decide_from_desktop(CORRELATION_ID, "once"))
        self.assertIsNone(self.final_say())

    def test_a_desktop_decision_needs_its_own_claim(self):
        self.open()
        self.assertFalse(self.answers.decide_from_desktop(CORRELATION_ID, "once"))
        self.assertIsNone(self.answers.desktop_decision(CORRELATION_ID, OFFERED))

    def test_a_claimed_desktop_answer_that_won_its_cancel_is_the_decision(self):
        self.open()
        self.claim("session")
        self.assertTrue(self.answers.decide_from_desktop(CORRELATION_ID, "session"))
        self.assertEqual(self.answers.desktop_decision(CORRELATION_ID, OFFERED), "session")
        self.assertEqual(self.final_say(), "session")
        self.assertEqual(self.answers.waiting_here(NOW_MS)[0].claim, ledger.WON)
        self.answers.close_slot(CORRELATION_ID)
        self.assertEqual(self.answers.waiting_here(NOW_MS), ())

    def test_the_transport_has_the_final_say_when_nobody_answered_on_desktop(self):
        self.open()
        started = time.monotonic()
        self.assertIsNone(self.final_say())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(self.claim(), ledger.NOT_WAITING)

    def test_a_desktop_answer_after_the_transport_took_the_final_say_is_not_used(self):
        self.open()
        self.claim()
        self.assertIsNone(self.final_say(wait=0.0))
        self.assertFalse(self.answers.decide_from_desktop(CORRELATION_ID, "once"))

    def test_only_one_side_ever_gets_the_final_say(self):
        for attempt in range(50):
            correlation_id = f"0b3f7a52-6c1d-4e8a-9f20-{attempt:012d}"
            self.open(correlation_id=correlation_id)
            self.assertEqual(self.answers.claim(correlation_id, "once", now_ms=NOW_MS), ledger.CLAIMED)
            barrier = threading.Barrier(2)
            results = {}

            def desktop():
                barrier.wait()
                results["desktop"] = self.answers.decide_from_desktop(correlation_id, "once")

            def transport():
                barrier.wait()
                results["transport"] = self.answers.take_final_say(correlation_id, OFFERED, claim_wait_seconds=0)

            threads = [threading.Thread(target=desktop), threading.Thread(target=transport)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(results["transport"], "once" if results["desktop"] else None, attempt)

    def test_a_claim_still_being_fenced_is_waited_for_within_the_budget(self):
        self.open()
        self.claim("once")
        threading.Timer(0.1, self.answers.decide_from_desktop, args=(CORRELATION_ID, "once")).start()
        started = time.monotonic()
        self.assertEqual(self.final_say(wait=3.0), "once")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_claim_that_lost_its_cancel_ends_the_wait_at_once(self):
        self.open()
        self.claim("once")
        threading.Timer(0.1, self.answers.lose_claim, args=(CORRELATION_ID,)).start()
        started = time.monotonic()
        self.assertIsNone(self.final_say(wait=3.0))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_claim_that_never_resolves_loses_at_the_budget(self):
        self.open()
        self.claim("once")
        started = time.monotonic()
        self.assertIsNone(self.final_say(wait=0.2))
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertFalse(self.answers.decide_from_desktop(CORRELATION_ID, "once"))

    def test_a_desktop_decision_for_a_choice_hermes_did_not_offer_is_ignored(self):
        self.open()
        self.claim("session")
        self.answers.decide_from_desktop(CORRELATION_ID, "session")
        self.assertIsNone(self.answers.take_final_say(CORRELATION_ID, ("once", "deny"), claim_wait_seconds=0))

    def test_waiting_approvals_carry_the_claim_state_the_panel_shows(self):
        self.open()
        self.assertIsNone(self.answers.waiting_here(NOW_MS)[0].claim)
        self.claim()
        self.assertEqual(self.answers.waiting_here(NOW_MS)[0].claim, ledger.CLAIMING)
        self.answers.lose_claim(CORRELATION_ID)
        self.assertEqual(self.answers.waiting_here(NOW_MS)[0].claim, ledger.LOST)


def load_plugin_copy(package_name):
    package = types.ModuleType(package_name)
    package.__path__ = [str(PLUGIN_DIRECTORY)]
    sys.modules[package_name] = package
    for name in ("approval_ledger", "desktop_answers"):
        spec = importlib.util.spec_from_file_location(f"{package_name}.{name}", PLUGIN_DIRECTORY / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{package_name}.desktop_answers"]


class SharedAcrossPluginCopiesTest(unittest.TestCase):
    def test_the_transport_and_the_desktop_backend_see_one_state_however_hermes_imported_them(self):
        with mock.patch.dict(sys.modules):
            sys.modules.pop(desktop_answers.SHARED_STATE_MODULE, None)
            transport_copy = load_plugin_copy("hermes_plugins_pushary_fixture")
            backend_copy = load_plugin_copy("pushary_plugin_desktop_backend_fixture")
            self.assertIsNot(transport_copy, backend_copy)
            transport_copy.shared_answers().open_slot(
                CORRELATION_ID, request_id=None, subject="git push", choices=(), opened_at_ms=0,
                deadline_at_ms=NOW_MS + 60_000, answerable_here=True, routed_to=None)
            self.assertEqual([item.correlation_id for item in backend_copy.shared_answers().waiting_here(NOW_MS)],
                             [CORRELATION_ID])

    def test_a_foreign_object_under_the_shared_name_is_refused(self):
        with mock.patch.dict(sys.modules, {desktop_answers.SHARED_STATE_MODULE: types.ModuleType("impostor")}):
            with self.assertRaises(Exception):
                desktop_answers.shared_answers()


if __name__ == "__main__":
    unittest.main()
