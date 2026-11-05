from __future__ import annotations

import sys
import threading
import types
from typing import Dict, Iterable, List, Optional, Tuple, TypedDict

from . import approval_ledger as ledger

SHARED_STATE_MODULE = "pushary_plugin_desktop_answers"
CLAIM_RESOLVE_SECONDS = 3.0


class AnswerSlot(TypedDict):
    request_id: Optional[str]
    subject: str
    choices: List[Tuple[ledger.ApprovalChoice, str]]
    opened_at_ms: int
    deadline_at_ms: int
    answerable_here: bool
    routed_to: Optional[str]
    claim: Optional[ledger.ClaimState]
    decided_by: Optional[ledger.Decider]
    desktop_choice: Optional[ledger.ApprovalChoice]


class DesktopAnswers:
    def __init__(self, condition: threading.Condition, slots: Dict[str, AnswerSlot]) -> None:
        self._condition = condition
        self._slots = slots

    def open_slot(
        self,
        correlation_id: str,
        *,
        request_id: Optional[str],
        subject: str,
        choices: Tuple[ledger.OfferedChoice, ...],
        opened_at_ms: int,
        deadline_at_ms: int,
        answerable_here: bool,
        routed_to: Optional[str],
    ) -> None:
        slot: AnswerSlot = {
            "request_id": request_id,
            "subject": subject,
            "choices": [(offered.choice, offered.label) for offered in choices],
            "opened_at_ms": opened_at_ms,
            "deadline_at_ms": deadline_at_ms,
            "answerable_here": answerable_here,
            "routed_to": routed_to,
            "claim": None,
            "decided_by": None,
            "desktop_choice": None,
        }
        with self._condition:
            self._slots[correlation_id] = slot
            self._condition.notify_all()

    def close_slot(self, correlation_id: str) -> None:
        with self._condition:
            self._slots.pop(correlation_id, None)
            self._condition.notify_all()

    def end_for_request(self, request_id: str) -> None:
        with self._condition:
            for slot in self._slots.values():
                if slot["request_id"] == request_id and slot["decided_by"] is None:
                    slot["decided_by"] = ledger.DECIDED_BY_TRANSPORT
            self._condition.notify_all()

    def has_ended(self, correlation_id: str) -> bool:
        with self._condition:
            slot = self._slots.get(correlation_id)
            return slot is not None and slot["decided_by"] is not None

    def claim(self, correlation_id: str, choice: ledger.ApprovalChoice, *, now_ms: int) -> ledger.ClaimResult:
        with self._condition:
            slot = self._slots.get(correlation_id)
            if slot is None:
                return ledger.NOT_FOUND
            if slot["decided_by"] is not None or now_ms >= slot["deadline_at_ms"]:
                return ledger.NOT_WAITING
            if not slot["answerable_here"]:
                return ledger.NOT_ANSWERABLE_HERE
            if all(offered != choice for offered, _ in slot["choices"]):
                return ledger.NOT_OFFERED
            if slot["claim"] is not None:
                return ledger.ALREADY_CLAIMED
            slot["claim"] = ledger.CLAIMING
            return ledger.CLAIMED

    def decide_from_desktop(self, correlation_id: str, choice: ledger.ApprovalChoice) -> bool:
        with self._condition:
            slot = self._slots.get(correlation_id)
            won = slot is not None and slot["decided_by"] is None and slot["claim"] == ledger.CLAIMING
            if slot is not None and won:
                slot["decided_by"] = ledger.DECIDED_BY_DESKTOP
                slot["desktop_choice"] = choice
                slot["claim"] = ledger.WON
            elif slot is not None and slot["claim"] == ledger.CLAIMING:
                slot["claim"] = ledger.LOST
            self._condition.notify_all()
            return won

    def lose_claim(self, correlation_id: str) -> None:
        with self._condition:
            slot = self._slots.get(correlation_id)
            if slot is not None and slot["claim"] == ledger.CLAIMING:
                slot["claim"] = ledger.LOST
            self._condition.notify_all()

    def desktop_decision(self, correlation_id: str, offered: Iterable[ledger.ApprovalChoice]) -> Optional[ledger.ApprovalChoice]:
        with self._condition:
            return self._desktop_choice_locked(correlation_id, tuple(offered))

    def take_final_say(
        self,
        correlation_id: str,
        offered: Iterable[ledger.ApprovalChoice],
        *,
        claim_wait_seconds: float,
    ) -> Optional[ledger.ApprovalChoice]:
        allowed = tuple(offered)
        with self._condition:
            self._condition.wait_for(lambda: not self._claim_is_being_fenced(correlation_id),
                                     timeout=max(0.0, claim_wait_seconds))
            slot = self._slots.get(correlation_id)
            if slot is not None and slot["decided_by"] is None:
                slot["decided_by"] = ledger.DECIDED_BY_TRANSPORT
                self._condition.notify_all()
                return None
            return self._desktop_choice_locked(correlation_id, allowed)

    def waiting_here(self, now_ms: int) -> Tuple[ledger.WaitingApproval, ...]:
        with self._condition:
            live = [(correlation_id, slot.copy()) for correlation_id, slot in self._slots.items()
                    if slot["decided_by"] != ledger.DECIDED_BY_TRANSPORT and now_ms < slot["deadline_at_ms"]]
        return tuple(_waiting(correlation_id, slot) for correlation_id, slot in live)

    def _claim_is_being_fenced(self, correlation_id: str) -> bool:
        slot = self._slots.get(correlation_id)
        return slot is not None and slot["decided_by"] is None and slot["claim"] == ledger.CLAIMING

    def _desktop_choice_locked(
        self, correlation_id: str, allowed: Tuple[ledger.ApprovalChoice, ...]
    ) -> Optional[ledger.ApprovalChoice]:
        slot = self._slots.get(correlation_id)
        if slot is None or slot["decided_by"] != ledger.DECIDED_BY_DESKTOP or not slot["answerable_here"]:
            return None
        choice = slot["desktop_choice"]
        return choice if choice in allowed else None


def _waiting(correlation_id: str, slot: AnswerSlot) -> ledger.WaitingApproval:
    return ledger.WaitingApproval(
        correlation_id=correlation_id,
        subject=slot["subject"],
        choices=tuple(ledger.OfferedChoice(choice, label) for choice, label in slot["choices"]),
        opened_at_ms=slot["opened_at_ms"],
        deadline_at_ms=slot["deadline_at_ms"],
        answerable_here=slot["answerable_here"],
        routed_to=slot["routed_to"],
        claim=slot["claim"],
    )


def shared_answers() -> DesktopAnswers:
    holder = sys.modules.get(SHARED_STATE_MODULE)
    if holder is None:
        created = types.ModuleType(SHARED_STATE_MODULE)
        setattr(created, "condition", threading.Condition())
        setattr(created, "slots", {})
        holder = sys.modules.setdefault(SHARED_STATE_MODULE, created)
    condition = getattr(holder, "condition")
    slots = getattr(holder, "slots")
    if not isinstance(condition, threading.Condition) or not isinstance(slots, dict):
        raise RuntimeError(f"{SHARED_STATE_MODULE} in sys.modules is not Pushary's Desktop answer state")
    return DesktopAnswers(condition, slots)
