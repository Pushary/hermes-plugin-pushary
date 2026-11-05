from __future__ import annotations

from typing import Callable, List, Literal, Mapping, Optional, Tuple, TypedDict, Union

from . import approval_ledger as ledger
from . import cancellation
from .desktop_answers import DesktopAnswers

CONTRACT_VERSION = 1
PLUGIN_ID = "pushary"
APPROVALS_CHANGED_EVENT = "approvals.changed"
APPROVALS_CHANGED_WIRE_EVENT = f"plugin.{PLUGIN_ID}.{APPROVALS_CHANGED_EVENT}"
APPROVALS_PATH = "/approvals"
ANSWER_PATH_SUFFIX = "/answer"

FenceOutcome = Literal["answered_here", "already_closed", "unreachable"]
AnswerOutcome = Union[FenceOutcome, ledger.RefusedClaim]

ANSWERED_HERE: FenceOutcome = "answered_here"
ALREADY_CLOSED: FenceOutcome = "already_closed"
UNREACHABLE: FenceOutcome = "unreachable"
FENCE_OUTCOMES: Tuple[FenceOutcome, ...] = (ANSWERED_HERE, ALREADY_CLOSED, UNREACHABLE)
ANSWER_OUTCOMES: Tuple[AnswerOutcome, ...] = (*FENCE_OUTCOMES, *ledger.REFUSED_CLAIMS)

HOST_FAILURE_OUTCOMES: Tuple[str, ...] = (
    "transport_busy",
    "transport_error",
    "transport_interrupted",
    "transport_invalid",
    "transport_stale",
    "transport_timeout",
)

CancelQuestion = Callable[[str], object]


class OfferedChoiceWire(TypedDict):
    choice: ledger.ApprovalChoice
    label: str


class WaitingApprovalWire(TypedDict):
    correlationId: str
    subject: str
    choices: List[OfferedChoiceWire]
    openedAtMs: int
    deadlineAtMs: int
    answerableHere: bool
    routedTo: Optional[str]
    claim: Optional[ledger.ClaimState]


class RecentApprovalWire(TypedDict):
    correlationId: str
    subject: str
    openedAtMs: int
    settledAtMs: int
    outcome: ledger.RecentOutcome
    answerSource: Optional[str]
    hostOutcome: Optional[str]


class SetupWire(TypedDict):
    apiKey: bool
    transport: bool
    pluginVersion: Optional[str]


class ApprovalsResponse(TypedDict):
    version: int
    waiting: List[WaitingApprovalWire]
    recent: List[RecentApprovalWire]
    setup: SetupWire


class AnswerResponse(TypedDict):
    version: int
    outcome: AnswerOutcome


class ContractFacts(TypedDict):
    version: int
    approvalsChangedWireEvent: str
    approvalsPath: str
    answerPathSuffix: str
    correlationIdPattern: str
    tokenPattern: str
    approvalChoices: List[str]
    claimStates: List[str]
    recentOutcomes: List[str]
    answerOutcomes: List[str]
    hostFailureOutcomes: List[str]


def _offered_wire(offered: ledger.OfferedChoice) -> OfferedChoiceWire:
    return {"choice": offered.choice, "label": offered.label}


def _waiting_wire(item: ledger.WaitingApproval) -> WaitingApprovalWire:
    return {
        "correlationId": item.correlation_id,
        "subject": item.subject,
        "choices": [_offered_wire(offered) for offered in item.choices],
        "openedAtMs": item.opened_at_ms,
        "deadlineAtMs": item.deadline_at_ms,
        "answerableHere": item.answerable_here,
        "routedTo": item.routed_to,
        "claim": item.claim,
    }


def _recent_wire(item: ledger.RecentApproval) -> RecentApprovalWire:
    return {
        "correlationId": item.correlation_id,
        "subject": item.subject,
        "openedAtMs": item.opened_at_ms,
        "settledAtMs": item.settled_at_ms,
        "outcome": item.outcome,
        "answerSource": item.answer_source,
        "hostOutcome": item.host_outcome,
    }


def setup_wire(*, api_key_saved: bool, transport_selected: bool, plugin_version: Optional[str]) -> SetupWire:
    return {"apiKey": api_key_saved, "transport": transport_selected, "pluginVersion": plugin_version}


def view_with_answers_here(view: ledger.ApprovalsView, here: Tuple[ledger.WaitingApproval, ...]) -> ledger.ApprovalsView:
    answered_here = {item.correlation_id for item in here}
    elsewhere = tuple(item for item in view.waiting if item.correlation_id not in answered_here)
    waiting = tuple(sorted((*here, *elsewhere), key=lambda item: item.opened_at_ms))
    return ledger.ApprovalsView(waiting=waiting, recent=view.recent)


def approvals_response(view: ledger.ApprovalsView, setup: SetupWire) -> ApprovalsResponse:
    return {
        "version": CONTRACT_VERSION,
        "waiting": [_waiting_wire(item) for item in view.waiting],
        "recent": [_recent_wire(item) for item in view.recent],
        "setup": setup,
    }


def answer_response(outcome: AnswerOutcome) -> AnswerResponse:
    return {"version": CONTRACT_VERSION, "outcome": outcome}


def parse_answer_request(body: object) -> Optional[ledger.ApprovalChoice]:
    if not isinstance(body, Mapping):
        return None
    fields: Mapping[object, object] = body
    if set(fields) != {"choice"}:
        return None
    return ledger.approval_choice(fields.get("choice"))


def _refused(result: ledger.ClaimResult) -> Optional[ledger.RefusedClaim]:
    for refused in ledger.REFUSED_CLAIMS:
        if result == refused:
            return refused
    return None


def _cancel_from_desktop(cancel_question: CancelQuestion, correlation_id: str) -> cancellation.Cancellation:
    try:
        return cancellation.classify_cancellation(cancel_question(correlation_id))
    except Exception:
        return cancellation.UNREACHABLE


def answer_from_desktop(
    answers: DesktopAnswers,
    correlation_id: str,
    choice: ledger.ApprovalChoice,
    *,
    cancel_question: CancelQuestion,
    now_ms: int,
) -> AnswerResponse:
    refused = _refused(answers.claim(correlation_id, choice, now_ms=now_ms))
    if refused is not None:
        return answer_response(refused)
    withdrawal = _cancel_from_desktop(cancel_question, correlation_id)
    if withdrawal == cancellation.WITHDRAWN_BY_THIS_CALL and answers.decide_from_desktop(correlation_id, choice):
        return answer_response(ANSWERED_HERE)
    answers.lose_claim(correlation_id)
    return answer_response(UNREACHABLE if withdrawal == cancellation.UNREACHABLE else ALREADY_CLOSED)


def contract_facts() -> ContractFacts:
    return {
        "version": CONTRACT_VERSION,
        "approvalsChangedWireEvent": APPROVALS_CHANGED_WIRE_EVENT,
        "approvalsPath": APPROVALS_PATH,
        "answerPathSuffix": ANSWER_PATH_SUFFIX,
        "correlationIdPattern": ledger.CORRELATION_ID_PATTERN.pattern,
        "tokenPattern": ledger.TOKEN_PATTERN.pattern,
        "approvalChoices": list(ledger.APPROVAL_CHOICES),
        "claimStates": list(ledger.CLAIM_STATES),
        "recentOutcomes": list(ledger.RECENT_OUTCOMES),
        "answerOutcomes": list(ANSWER_OUTCOMES),
        "hostFailureOutcomes": list(HOST_FAILURE_OUTCOMES),
    }
