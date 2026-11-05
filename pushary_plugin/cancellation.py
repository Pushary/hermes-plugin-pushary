from __future__ import annotations

from typing import Literal, Mapping, Tuple

Cancellation = Literal[
    "withdrawn_by_this_call",
    "cancelled_unproven",
    "already_cancelled",
    "not_pending",
    "unreachable",
]

WITHDRAWN_BY_THIS_CALL: Cancellation = "withdrawn_by_this_call"
CANCELLED_UNPROVEN: Cancellation = "cancelled_unproven"
ALREADY_CANCELLED: Cancellation = "already_cancelled"
NOT_PENDING: Cancellation = "not_pending"
UNREACHABLE: Cancellation = "unreachable"
CANCELLATIONS: Tuple[Cancellation, ...] = (
    WITHDRAWN_BY_THIS_CALL,
    CANCELLED_UNPROVEN,
    ALREADY_CANCELLED,
    NOT_PENDING,
    UNREACHABLE,
)

UNAVAILABLE_STATUS = "unavailable"
STOP_HANDOFF = "stop"


def classify_cancellation(response: object) -> Cancellation:
    if not isinstance(response, Mapping):
        return UNREACHABLE
    fields: Mapping[object, object] = response
    cancelled = fields.get("cancelled")
    by_this_call = fields.get("cancelledByThisCall")
    if (
        not isinstance(cancelled, bool)
        or fields.get("status") == UNAVAILABLE_STATUS
        or fields.get("handoffAction") == STOP_HANDOFF
        or (by_this_call is not None and not isinstance(by_this_call, bool))
        or (by_this_call is True and not cancelled)
    ):
        return UNREACHABLE
    if not cancelled:
        return NOT_PENDING
    if by_this_call is None:
        return CANCELLED_UNPROVEN
    return WITHDRAWN_BY_THIS_CALL if by_this_call else ALREADY_CANCELLED
