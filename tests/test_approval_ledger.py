import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from pushary_plugin import approval_ledger as ledger

CORRELATION_ID = "0b3f7a52-6c1d-4e8a-9f20-5d7c1b2a3e4f"
OTHER_CORRELATION_ID = "1c4e8b63-7d2e-4f9b-a031-6e8d2c3b4f50"
REQUEST_ID = "4f1d2c3b4a5e6f708192a3b4c5d6e7f8"
CHOICES = ledger.offered_choices((("once", "Allow once"), ("session", "Allow for this session"), ("deny", "Deny")))
OPENED_AT_MS = int(time.time() * 1000)
DEADLINE_AT_MS = OPENED_AT_MS + 300_000
NOW_MS = OPENED_AT_MS + 1_000


def running(pid):
    return True


def stopped(pid):
    return False


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="pushary-ledger-")
        self.home = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def open(self, correlation_id=CORRELATION_ID, request_id=REQUEST_ID, pid=None, deadline_at_ms=DEADLINE_AT_MS):
        return ledger.open_approval(
            self.home,
            correlation_id=correlation_id,
            request_id=request_id,
            subject="git push --force origin main",
            choices=CHOICES,
            pid=os.getpid() if pid is None else pid,
            opened_at_ms=OPENED_AT_MS,
            deadline_at_ms=deadline_at_ms,
        )

    def view(self, now_ms=NOW_MS, process_is_running=running):
        return ledger.read_approvals(self.home, now_ms, process_is_running)



class ApprovalRecordTest(LedgerTestCase):
    def test_an_open_approval_is_waiting_with_the_choices_hermes_offered(self):
        self.assertTrue(self.open())
        waiting = self.view().waiting
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0].correlation_id, CORRELATION_ID)
        self.assertEqual(waiting[0].subject, "git push --force origin main")
        self.assertEqual([offered.choice for offered in waiting[0].choices], ["once", "session", "deny"])

    def test_a_recorded_approval_is_never_answerable_whatever_its_file_says(self):
        self.open()
        path = ledger.ledger_directory(self.home) / f"{CORRELATION_ID}.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "answerableHere": True, "routedTo": None}))
        waiting = self.view().waiting[0]
        self.assertEqual((waiting.answerable_here, waiting.routed_to, waiting.claim), (False, None, None))

    def test_ledger_files_are_private_to_the_user(self):
        self.open()
        ledger.record_host_outcome(self.home, REQUEST_ID, "transport_timeout", NOW_MS)
        directory = ledger.ledger_directory(self.home)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)

    def test_an_identifier_that_could_leave_the_ledger_folder_is_refused(self):
        for hostile in ("../../config", "a/b", "", None, CORRELATION_ID + "/..", CORRELATION_ID + "\n"):
            self.assertFalse(self.open(correlation_id=hostile))
            self.assertIsNone(ledger.read_record(self.home, hostile))
        self.assertFalse(ledger.record_host_outcome(self.home, "../host", "once", OPENED_AT_MS))
        self.assertFalse(ledger.record_host_outcome(self.home, REQUEST_ID + "\n", "once", OPENED_AT_MS))
        self.assertFalse((self.home / "config.json").exists())

    def test_settling_moves_an_approval_to_recent_with_where_it_was_answered(self):
        self.open()
        self.assertTrue(ledger.settle_approval(self.home, CORRELATION_ID, outcome="once",
                                               answer_source="mobile_app", settled_at_ms=OPENED_AT_MS + 5_000))
        view = self.view()
        self.assertEqual(view.waiting, ())
        self.assertEqual(view.recent[0].outcome, "once")
        self.assertEqual(view.recent[0].answer_source, "mobile_app")

    def test_an_approval_settles_once(self):
        self.open()
        ledger.settle_approval(self.home, CORRELATION_ID, outcome="once", answer_source="mobile_app", settled_at_ms=1)
        self.assertFalse(ledger.settle_approval(self.home, CORRELATION_ID, outcome="deny", answer_source=None, settled_at_ms=2))
        self.assertEqual(self.view().recent[0].outcome, "once")

    def test_hermes_ending_the_request_ends_the_wait_even_while_the_transport_still_runs(self):
        self.open()
        self.assertFalse(ledger.host_has_finished(self.home, REQUEST_ID))
        ledger.record_host_outcome(self.home, REQUEST_ID, "transport_interrupted", OPENED_AT_MS + 2_000)
        self.assertTrue(ledger.host_has_finished(self.home, REQUEST_ID))
        view = self.view()
        self.assertEqual(view.waiting, ())
        self.assertEqual(view.recent[0].host_outcome, "transport_interrupted")

    def test_an_approval_whose_process_died_or_deadline_passed_is_not_shown_as_waiting(self):
        self.open()
        self.assertEqual(self.view(process_is_running=stopped).waiting, ())
        self.assertEqual(self.view(now_ms=DEADLINE_AT_MS).waiting, ())
        self.assertEqual(self.view(now_ms=DEADLINE_AT_MS).recent[0].outcome, ledger.ENDED)

    def test_a_record_without_a_real_process_is_never_opened_or_live(self):
        for pid in (0, -1):
            self.assertFalse(self.open(pid=pid))
        self.open()
        path = ledger.ledger_directory(self.home) / f"{CORRELATION_ID}.json"
        stored = json.loads(path.read_text())
        for pid in (0, -1, True, "1"):
            path.write_text(json.dumps({**stored, "pid": pid}))
            self.assertEqual(self.view().waiting, (), pid)

    def test_a_record_with_any_malformed_field_is_ignored_whole(self):
        self.open()
        path = ledger.ledger_directory(self.home) / f"{CORRELATION_ID}.json"
        stored = json.loads(path.read_text())
        for field, value in (("subject", "x" * 201), ("subject", 7), ("choices", []), ("pid", "1"),
                             ("choices", [{"choice": "maybe", "label": "Maybe"}]), ("state", "open"), ("openedAtMs", -1)):
            path.write_text(json.dumps({**stored, field: value}))
            self.assertEqual(self.view(), ledger.ApprovalsView(waiting=(), recent=()), field)

    def test_unreadable_files_are_skipped_not_fatal(self):
        self.open()
        directory = ledger.ledger_directory(self.home)
        (directory / f"{OTHER_CORRELATION_ID}.json").write_text("{not json")
        (directory / "host-broken.json").write_text("[]")
        self.assertEqual(len(self.view().waiting), 1)

    def test_a_missing_ledger_reads_as_empty(self):
        self.assertEqual(self.view(), ledger.ApprovalsView(waiting=(), recent=()))

    def test_recent_is_newest_first_and_bounded(self):
        for index in range(ledger.RECENT_LIMIT + 3):
            correlation_id = f"0b3f7a52-6c1d-4e8a-9f20-{index:012d}"
            self.open(correlation_id=correlation_id, request_id=None)
            ledger.settle_approval(self.home, correlation_id, outcome="once", answer_source=None, settled_at_ms=index)
        recent = self.view().recent
        self.assertEqual(len(recent), ledger.RECENT_LIMIT)
        self.assertEqual(recent[0].settled_at_ms, ledger.RECENT_LIMIT + 2)


class PruneTest(LedgerTestCase):
    def age_everything(self):
        old = OPENED_AT_MS / 1000 - ledger.RETENTION_SECONDS - 60
        for path in ledger.ledger_directory(self.home).iterdir():
            os.utime(path, (old, old))

    def test_old_entries_and_abandoned_staging_files_are_pruned(self):
        self.open(deadline_at_ms=OPENED_AT_MS)
        directory = ledger.ledger_directory(self.home)
        (directory / f".{CORRELATION_ID}.json.dead.tmp").write_text("{}")
        ledger.record_host_outcome(self.home, REQUEST_ID, "transport_timeout", OPENED_AT_MS)
        self.age_everything()
        ledger.prune(self.home, OPENED_AT_MS / 1000)
        self.assertEqual(list(directory.iterdir()), [])

    def test_an_approval_still_waiting_inside_a_long_timeout_is_kept(self):
        self.open(deadline_at_ms=OPENED_AT_MS + 2 * ledger.RETENTION_SECONDS * 1000)
        self.age_everything()
        ledger.prune(self.home, OPENED_AT_MS / 1000)
        self.assertEqual(len(self.view().waiting), 1)


if __name__ == "__main__":
    unittest.main()
