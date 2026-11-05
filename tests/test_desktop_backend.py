import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    FASTAPI_IMPORT_ERROR = None
except Exception as exc:
    FastAPI = None
    FASTAPI_IMPORT_ERROR = exc

HERMES_REQUIRED = os.environ.get("PUSHARY_REQUIRE_HERMES") == "1"


def setUpModule():
    if HERMES_REQUIRED and FastAPI is None:
        raise RuntimeError(f"PUSHARY_REQUIRE_HERMES=1 but FastAPI is not importable: {FASTAPI_IMPORT_ERROR}")


requires_fastapi = unittest.skipIf(
    FastAPI is None,
    f"FastAPI is Hermes's dashboard dependency and is not importable here ({FASTAPI_IMPORT_ERROR})",
)

PLUGIN_API = Path(__file__).resolve().parents[1] / "pushary_plugin" / "dashboard" / "plugin_api.py"
CORRELATION_ID = "3d4e5f6a-7b8c-4d9e-af01-23456789abcd"
ANSWER_PATH = f"/api/plugins/pushary/approvals/{CORRELATION_ID}/answer"


def load_backend_like_hermes():
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_pushary", PLUGIN_API)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@requires_fastapi
class DesktopBackendRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = load_backend_like_hermes()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="pushary-backend-")
        self.home = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        backend = self.backend
        self.cancel = mock.Mock(return_value={"cancelled": True, "cancelledByThisCall": True})
        for patcher in (
            mock.patch.object(backend.installation, "hermes_home", return_value=self.home),
            mock.patch.object(backend.api, "is_configured", return_value=True),
            mock.patch.object(backend.api, "cancel_question", self.cancel),
            mock.patch.object(backend.host, "pushary_is_selected_transport", return_value=True),
            mock.patch.object(backend.approval, "announce_approvals_changed"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(backend.router, prefix="/api/plugins/pushary")
        self.client = TestClient(app)
        ledger = backend.approval_ledger
        now_ms = int(time.time() * 1000)
        choices = ledger.offered_choices((("once", "Allow once"), ("deny", "Deny")))
        ledger.open_approval(self.home, correlation_id=CORRELATION_ID, request_id=None, subject="git push --force",
                             choices=choices, pid=os.getpid(), opened_at_ms=now_ms, deadline_at_ms=now_ms + 60_000)
        self.answers = backend.desktop_answers.shared_answers()
        self.answers.open_slot(CORRELATION_ID, request_id=None, subject="git push --force", choices=choices,
                               opened_at_ms=now_ms, deadline_at_ms=now_ms + 60_000, answerable_here=True, routed_to=None)
        self.addCleanup(self.answers.close_slot, CORRELATION_ID)

    def test_the_approvals_route_returns_the_versioned_contract(self):
        response = self.client.get("/api/plugins/pushary/approvals")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["version"], self.backend.contract.CONTRACT_VERSION)
        self.assertEqual([(item["correlationId"], item["answerableHere"]) for item in body["waiting"]],
                         [(CORRELATION_ID, True)])
        self.assertEqual(body["setup"], {"apiKey": True, "transport": True,
                                         "pluginVersion": self.backend.installation.LOADED_PLUGIN_VERSION})

    def test_an_offered_answer_is_fenced_against_pushary_and_reported(self):
        response = self.client.post(ANSWER_PATH, json={"choice": "once"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"version": 1, "outcome": "answered_here"})
        self.cancel.assert_called_once_with(CORRELATION_ID)
        self.assertEqual(self.answers.desktop_decision(CORRELATION_ID, ("once", "deny")), "once")

    def test_an_idempotent_cancel_that_someone_else_made_is_never_reported_as_answered(self):
        self.cancel.return_value = {"cancelled": True, "cancelledByThisCall": False}
        self.assertEqual(self.client.post(ANSWER_PATH, json={"choice": "once"}).json()["outcome"], "already_closed")
        self.assertIsNone(self.answers.desktop_decision(CORRELATION_ID, ("once", "deny")))

    def test_an_approval_only_on_disk_is_listed_without_answers_and_refused(self):
        self.answers.close_slot(CORRELATION_ID)
        body = self.client.get("/api/plugins/pushary/approvals").json()
        self.assertEqual([(item["correlationId"], item["answerableHere"]) for item in body["waiting"]],
                         [(CORRELATION_ID, False)])
        self.assertEqual(self.client.post(ANSWER_PATH, json={"choice": "once"}).json()["outcome"], "not_found")
        self.cancel.assert_not_called()

    def test_a_malformed_answer_is_refused_before_anything_is_touched(self):
        for body in (None, {}, {"choice": "yes"}, {"choice": "once", "note": "x"}, ["once"], "once"):
            with self.subTest(body=body):
                response = self.client.post(ANSWER_PATH, json=body)
                self.assertEqual(response.status_code, 400)
        for path in ("/api/plugins/pushary/approvals/not-a-uuid/answer",
                     "/api/plugins/pushary/approvals/3D4E5F6A-7B8C-4D9E-AF01-23456789ABCD/answer"):
            self.assertEqual(self.client.post(path, json={"choice": "once"}).status_code, 400)
        self.cancel.assert_not_called()
        self.assertIsNone(self.answers.waiting_here(int(time.time() * 1000))[0].claim)

    def test_a_choice_hermes_did_not_offer_is_a_refusal_not_a_cancel(self):
        response = self.client.post(ANSWER_PATH, json={"choice": "always"})
        self.assertEqual(response.json()["outcome"], "not_offered")
        self.cancel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
