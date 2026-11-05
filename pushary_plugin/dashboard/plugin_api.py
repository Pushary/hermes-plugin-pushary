import importlib
import sys
import time
import types
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

PLUGIN_DIRECTORY = Path(__file__).resolve().parent.parent
BACKEND_PACKAGE = "pushary_plugin_desktop_backend"
INVALID_ANSWER = 'Send {"choice": one of the choices Hermes offered}'


def _register_plugin_package() -> None:
    package = types.ModuleType(BACKEND_PACKAGE)
    package.__path__ = [str(PLUGIN_DIRECTORY)]
    sys.modules[BACKEND_PACKAGE] = package


_register_plugin_package()
api = importlib.import_module(f"{BACKEND_PACKAGE}.api")
approval = importlib.import_module(f"{BACKEND_PACKAGE}.approval")
approval_ledger = importlib.import_module(f"{BACKEND_PACKAGE}.approval_ledger")
desktop_answers = importlib.import_module(f"{BACKEND_PACKAGE}.desktop_answers")
contract = importlib.import_module(f"{BACKEND_PACKAGE}.desktop_contract")
host = importlib.import_module(f"{BACKEND_PACKAGE}.host")
installation = importlib.import_module(f"{BACKEND_PACKAGE}.installation")

router = APIRouter()


def _now_ms() -> int:
    return int(time.time() * 1000)


@router.get(contract.APPROVALS_PATH)
def approvals():
    now_ms = _now_ms()
    recorded = approval_ledger.read_approvals(installation.hermes_home(), now_ms, installation.process_is_running)
    view = contract.view_with_answers_here(recorded, desktop_answers.shared_answers().waiting_here(now_ms))
    setup = contract.setup_wire(
        api_key_saved=api.is_configured(),
        transport_selected=host.pushary_is_selected_transport(),
        plugin_version=installation.LOADED_PLUGIN_VERSION,
    )
    return contract.approvals_response(view, setup)


@router.post(f"{contract.APPROVALS_PATH}/{{correlation_id}}{contract.ANSWER_PATH_SUFFIX}")
def answer(correlation_id: str, body: Any = Body(default=None)):
    choice = contract.parse_answer_request(body)
    if choice is None or not approval_ledger.is_correlation_id(correlation_id):
        raise HTTPException(status_code=400, detail=INVALID_ANSWER)
    response = contract.answer_from_desktop(
        desktop_answers.shared_answers(),
        correlation_id,
        choice,
        cancel_question=api.cancel_question,
        now_ms=_now_ms(),
    )
    approval.announce_approvals_changed()
    return response
