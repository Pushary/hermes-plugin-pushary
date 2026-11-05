from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Literal, Mapping, Optional, Set, Tuple, Union

if TYPE_CHECKING:
    from typing_extensions import TypeGuard

ApprovalChoice = Literal["once", "session", "always", "deny"]
RecordState = Literal["waiting", "settled"]
ClaimState = Literal["claiming", "won", "lost"]
HandedBack = Literal["handed_to_hermes"]
Ended = Literal["ended"]
SettledOutcome = Union[ApprovalChoice, HandedBack]
RecentOutcome = Union[SettledOutcome, Ended]
Decider = Literal["hermes_desktop", "pushary_transport"]
RefusedClaim = Literal["not_found", "not_waiting", "not_answerable_here", "not_offered", "already_claimed"]
ClaimResult = Union[Literal["claimed"], RefusedClaim]
ProcessIsRunning = Callable[[int], bool]

APPROVAL_CHOICES: Tuple[ApprovalChoice, ...] = ("once", "session", "always", "deny")
WAITING: RecordState = "waiting"
SETTLED: RecordState = "settled"
CLAIMING: ClaimState = "claiming"
WON: ClaimState = "won"
LOST: ClaimState = "lost"
CLAIM_STATES: Tuple[ClaimState, ...] = (CLAIMING, WON, LOST)
HANDED_TO_HERMES: HandedBack = "handed_to_hermes"
ENDED: Ended = "ended"
SETTLED_OUTCOMES: Tuple[SettledOutcome, ...] = (*APPROVAL_CHOICES, HANDED_TO_HERMES)
RECENT_OUTCOMES: Tuple[RecentOutcome, ...] = (*SETTLED_OUTCOMES, ENDED)
DECIDED_BY_DESKTOP: Decider = "hermes_desktop"
DECIDED_BY_TRANSPORT: Decider = "pushary_transport"

CLAIMED: Literal["claimed"] = "claimed"
NOT_FOUND: RefusedClaim = "not_found"
NOT_WAITING: RefusedClaim = "not_waiting"
NOT_ANSWERABLE_HERE: RefusedClaim = "not_answerable_here"
NOT_OFFERED: RefusedClaim = "not_offered"
ALREADY_CLAIMED: RefusedClaim = "already_claimed"
REFUSED_CLAIMS: Tuple[RefusedClaim, ...] = (NOT_FOUND, NOT_WAITING, NOT_ANSWERABLE_HERE, NOT_OFFERED, ALREADY_CLAIMED)

DESKTOP_ANSWER_SOURCE = "hermes_desktop"
UNNAMED_ANSWER_SOURCE = "pushary"

LEDGER_PATH = ("plugin-data", "pushary", "approvals")
RECORD_SUFFIX = ".json"
HOST_OUTCOME_PREFIX = "host-"
TEMPORARY_PREFIX = "."

CORRELATION_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TOKEN_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

RETENTION_SECONDS = 24 * 60 * 60
RECENT_LIMIT = 6
SUBJECT_MAX = 200
LABEL_MAX = 80


@dataclass(frozen=True)
class OfferedChoice:
    choice: ApprovalChoice
    label: str


@dataclass(frozen=True)
class OpenedApproval:
    correlation_id: str
    request_id: Optional[str]
    subject: str
    choices: Tuple[OfferedChoice, ...]
    pid: int
    opened_at_ms: int
    deadline_at_ms: int


@dataclass(frozen=True)
class WaitingRecord:
    opened: OpenedApproval


@dataclass(frozen=True)
class SettledRecord:
    opened: OpenedApproval
    outcome: SettledOutcome
    answer_source: Optional[str]
    settled_at_ms: int


ApprovalRecord = Union[WaitingRecord, SettledRecord]


@dataclass(frozen=True)
class HostOutcome:
    request_id: str
    choice: str
    at_ms: int


@dataclass(frozen=True)
class WaitingApproval:
    correlation_id: str
    subject: str
    choices: Tuple[OfferedChoice, ...]
    opened_at_ms: int
    deadline_at_ms: int
    answerable_here: bool
    routed_to: Optional[str]
    claim: Optional[ClaimState]


@dataclass(frozen=True)
class RecentApproval:
    correlation_id: str
    subject: str
    opened_at_ms: int
    settled_at_ms: int
    outcome: RecentOutcome
    answer_source: Optional[str]
    host_outcome: Optional[str]


@dataclass(frozen=True)
class ApprovalsView:
    waiting: Tuple[WaitingApproval, ...]
    recent: Tuple[RecentApproval, ...]


def is_correlation_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and CORRELATION_ID_PATTERN.fullmatch(value) is not None


def is_request_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and REQUEST_ID_PATTERN.fullmatch(value) is not None


def is_token(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and TOKEN_PATTERN.fullmatch(value) is not None


def approval_choice(value: object) -> Optional[ApprovalChoice]:
    for choice in APPROVAL_CHOICES:
        if value == choice:
            return choice
    return None


def _record_state(value: object) -> Optional[RecordState]:
    for state in (WAITING, SETTLED):
        if value == state:
            return state
    return None


def _settled_outcome(value: object) -> Optional[SettledOutcome]:
    for outcome in SETTLED_OUTCOMES:
        if value == outcome:
            return outcome
    return None


def _instant(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _bounded_text(value: object, limit: int) -> Optional[str]:
    return value if isinstance(value, str) and len(value) <= limit else None


def _fields(value: object) -> Optional[Mapping[object, object]]:
    return value if isinstance(value, dict) else None


def offered_choices(pairs: Iterable[Tuple[str, str]]) -> Tuple[OfferedChoice, ...]:
    offered = []
    for raw_choice, raw_label in pairs:
        choice = approval_choice(raw_choice)
        if choice is not None:
            offered.append(OfferedChoice(choice, str(raw_label)[:LABEL_MAX]))
    return tuple(offered)


def _parse_offered(value: object) -> Optional[OfferedChoice]:
    fields = _fields(value)
    if fields is None:
        return None
    choice = approval_choice(fields.get("choice"))
    label = _bounded_text(fields.get("label"), LABEL_MAX)
    return OfferedChoice(choice, label) if choice is not None and label is not None else None


def _parse_choices(value: object) -> Optional[Tuple[OfferedChoice, ...]]:
    if not isinstance(value, list):
        return None
    parsed: List[OfferedChoice] = []
    for item in value:
        offered = _parse_offered(item)
        if offered is None:
            return None
        parsed.append(offered)
    return tuple(parsed)


def _parse_opened(fields: Mapping[object, object]) -> Optional[OpenedApproval]:
    correlation_id = fields.get("correlationId")
    request_id = fields.get("requestId")
    subject = _bounded_text(fields.get("subject"), SUBJECT_MAX)
    choices = _parse_choices(fields.get("choices"))
    pid = fields.get("pid")
    opened_at_ms = _instant(fields.get("openedAtMs"))
    deadline_at_ms = _instant(fields.get("deadlineAtMs"))
    if not is_correlation_id(correlation_id):
        return None
    if request_id is not None and not is_request_id(request_id):
        return None
    if (
        subject is None
        or not choices
        or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
        or opened_at_ms is None
        or deadline_at_ms is None
    ):
        return None
    return OpenedApproval(
        correlation_id=correlation_id,
        request_id=request_id,
        subject=subject,
        choices=choices,
        pid=pid,
        opened_at_ms=opened_at_ms,
        deadline_at_ms=deadline_at_ms,
    )


def parse_record(value: object) -> Optional[ApprovalRecord]:
    fields = _fields(value)
    if fields is None:
        return None
    opened = _parse_opened(fields)
    state = _record_state(fields.get("state"))
    if opened is None or state is None:
        return None
    if state == WAITING:
        return WaitingRecord(opened)
    outcome = _settled_outcome(fields.get("outcome"))
    settled_at_ms = _instant(fields.get("settledAtMs"))
    answer_source = fields.get("answerSource")
    if outcome is None or settled_at_ms is None:
        return None
    if answer_source is not None and not is_token(answer_source):
        return None
    return SettledRecord(opened, outcome, answer_source, settled_at_ms)


def parse_host_outcome(value: object) -> Optional[HostOutcome]:
    fields = _fields(value)
    if fields is None:
        return None
    request_id = fields.get("requestId")
    choice = fields.get("choice")
    at_ms = _instant(fields.get("atMs"))
    if not is_request_id(request_id) or not is_token(choice) or at_ms is None:
        return None
    return HostOutcome(request_id, choice, at_ms)


def _offered_json(choice: OfferedChoice) -> Dict[str, object]:
    return {"choice": choice.choice, "label": choice.label}


def _opened_json(opened: OpenedApproval) -> Dict[str, object]:
    return {
        "correlationId": opened.correlation_id,
        "requestId": opened.request_id,
        "subject": opened.subject,
        "choices": [_offered_json(choice) for choice in opened.choices],
        "pid": opened.pid,
        "openedAtMs": opened.opened_at_ms,
        "deadlineAtMs": opened.deadline_at_ms,
    }


def _record_json(record: ApprovalRecord) -> Dict[str, object]:
    stored = _opened_json(record.opened)
    if isinstance(record, WaitingRecord):
        stored["state"] = WAITING
        return stored
    stored.update(state=SETTLED, outcome=record.outcome, answerSource=record.answer_source,
                  settledAtMs=record.settled_at_ms)
    return stored


def _host_outcome_json(outcome: HostOutcome) -> Dict[str, object]:
    return {"requestId": outcome.request_id, "choice": outcome.choice, "atMs": outcome.at_ms}


def ledger_directory(home: Path) -> Path:
    return Path(home).joinpath(*LEDGER_PATH)


def _record_path(home: Path, correlation_id: str) -> Path:
    return ledger_directory(home) / f"{correlation_id}{RECORD_SUFFIX}"


def _host_outcome_path(home: Path, request_id: str) -> Path:
    return ledger_directory(home) / f"{HOST_OUTCOME_PREFIX}{request_id}{RECORD_SUFFIX}"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)


def _staged_json(path: Path, value: Dict[str, object]) -> Path:
    _ensure_parent(path)
    staged = path.with_name(f"{TEMPORARY_PREFIX}{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with open(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
            json.dump(value, handle)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def _replace_json(path: Path, value: Dict[str, object]) -> None:
    staged = _staged_json(path, value)
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_record(home: Path, correlation_id: str) -> Optional[ApprovalRecord]:
    if not is_correlation_id(correlation_id):
        return None
    record = parse_record(_read_json(_record_path(home, correlation_id)))
    return record if record is not None and record.opened.correlation_id == correlation_id else None


def read_host_outcome(home: Path, request_id: Optional[str]) -> Optional[HostOutcome]:
    if not is_request_id(request_id):
        return None
    outcome = parse_host_outcome(_read_json(_host_outcome_path(home, request_id)))
    return outcome if outcome is not None and outcome.request_id == request_id else None


def open_approval(
    home: Path,
    *,
    correlation_id: str,
    request_id: Optional[str],
    subject: str,
    choices: Tuple[OfferedChoice, ...],
    pid: int,
    opened_at_ms: int,
    deadline_at_ms: int,
) -> bool:
    if not is_correlation_id(correlation_id) or pid <= 0 or not choices:
        return False
    opened = OpenedApproval(
        correlation_id=correlation_id,
        request_id=request_id if is_request_id(request_id) else None,
        subject=subject[:SUBJECT_MAX],
        choices=choices,
        pid=pid,
        opened_at_ms=opened_at_ms,
        deadline_at_ms=deadline_at_ms,
    )
    _replace_json(_record_path(home, correlation_id), _record_json(WaitingRecord(opened)))
    return True


def settle_approval(
    home: Path,
    correlation_id: str,
    *,
    outcome: SettledOutcome,
    answer_source: Optional[str],
    settled_at_ms: int,
) -> bool:
    record = read_record(home, correlation_id)
    if not isinstance(record, WaitingRecord):
        return False
    source = answer_source if is_token(answer_source) else None
    _replace_json(_record_path(home, correlation_id), _record_json(SettledRecord(record.opened, outcome, source,
                                                                                 settled_at_ms)))
    return True


def record_host_outcome(home: Path, request_id: str, choice: str, at_ms: int) -> bool:
    if not is_request_id(request_id) or not is_token(choice):
        return False
    _replace_json(_host_outcome_path(home, request_id), _host_outcome_json(HostOutcome(request_id, choice, at_ms)))
    return True


def host_has_finished(home: Path, request_id: Optional[str]) -> bool:
    return read_host_outcome(home, request_id) is not None


def _owner_is_running(opened: OpenedApproval, process_is_running: ProcessIsRunning) -> bool:
    return opened.pid > 0 and process_is_running(opened.pid)


def _is_live(record: ApprovalRecord, host_outcome: Optional[HostOutcome], now_ms: int,
             process_is_running: ProcessIsRunning) -> bool:
    return (
        isinstance(record, WaitingRecord)
        and host_outcome is None
        and now_ms < record.opened.deadline_at_ms
        and _owner_is_running(record.opened, process_is_running)
    )


def _waiting_elsewhere(record: WaitingRecord) -> WaitingApproval:
    opened = record.opened
    return WaitingApproval(
        correlation_id=opened.correlation_id,
        subject=opened.subject,
        choices=opened.choices,
        opened_at_ms=opened.opened_at_ms,
        deadline_at_ms=opened.deadline_at_ms,
        answerable_here=False,
        routed_to=None,
        claim=None,
    )


def _recent(record: ApprovalRecord, host_outcome: Optional[HostOutcome]) -> RecentApproval:
    opened = record.opened
    if isinstance(record, SettledRecord):
        outcome: RecentOutcome = record.outcome
        settled_at_ms = record.settled_at_ms
        answer_source = record.answer_source
    else:
        outcome = ENDED
        settled_at_ms = host_outcome.at_ms if host_outcome else opened.deadline_at_ms
        answer_source = None
    return RecentApproval(
        correlation_id=opened.correlation_id,
        subject=opened.subject,
        opened_at_ms=opened.opened_at_ms,
        settled_at_ms=settled_at_ms,
        outcome=outcome,
        answer_source=answer_source,
        host_outcome=host_outcome.choice if host_outcome else None,
    )


def approvals_view(
    records: Iterable[ApprovalRecord],
    host_outcomes: Mapping[str, HostOutcome],
    now_ms: int,
    process_is_running: ProcessIsRunning,
) -> ApprovalsView:
    waiting = []
    recent = []
    for record in records:
        request_id = record.opened.request_id
        host_outcome = host_outcomes.get(request_id) if request_id else None
        if isinstance(record, WaitingRecord) and _is_live(record, host_outcome, now_ms, process_is_running):
            waiting.append(_waiting_elsewhere(record))
        else:
            recent.append(_recent(record, host_outcome))
    waiting.sort(key=lambda item: item.opened_at_ms)
    recent.sort(key=lambda item: item.settled_at_ms, reverse=True)
    return ApprovalsView(waiting=tuple(waiting), recent=tuple(recent[:RECENT_LIMIT]))


def _ledger_files(home: Path) -> List[Path]:
    try:
        return [path for path in ledger_directory(home).iterdir() if path.is_file()]
    except OSError:
        return []


def _correlation_id_of(name: str) -> Optional[str]:
    candidate = name[: -len(RECORD_SUFFIX)] if name.endswith(RECORD_SUFFIX) else None
    return candidate if is_correlation_id(candidate) else None


def _ledger_entries(home: Path) -> List[Tuple[Path, object]]:
    return [(path, _read_json(path)) for path in _ledger_files(home) if not path.name.startswith(TEMPORARY_PREFIX)]


def read_approvals(home: Path, now_ms: int, process_is_running: ProcessIsRunning) -> ApprovalsView:
    records: List[ApprovalRecord] = []
    host_outcomes: Dict[str, HostOutcome] = {}
    for path, value in _ledger_entries(home):
        name = path.name
        if name.startswith(HOST_OUTCOME_PREFIX):
            host_outcome = parse_host_outcome(value)
            if host_outcome is not None:
                host_outcomes[host_outcome.request_id] = host_outcome
        else:
            record = parse_record(value)
            if record is not None and name == f"{record.opened.correlation_id}{RECORD_SUFFIX}":
                records.append(record)
    return approvals_view(records, host_outcomes, now_ms, process_is_running)


def _unexpired_waiting(home: Path, now_ms: int) -> Set[str]:
    kept: Set[str] = set()
    for path, value in _ledger_entries(home):
        if path.name.startswith(HOST_OUTCOME_PREFIX) or not path.name.endswith(RECORD_SUFFIX):
            continue
        record = parse_record(value)
        if isinstance(record, WaitingRecord) and record.opened.deadline_at_ms > now_ms:
            kept.add(record.opened.correlation_id)
    return kept


def prune(home: Path, now_seconds: float) -> None:
    kept = _unexpired_waiting(home, int(now_seconds * 1000))
    for path in _ledger_files(home):
        if _correlation_id_of(path.name) in kept:
            continue
        try:
            if now_seconds - path.stat().st_mtime > RETENTION_SECONDS:
                path.unlink(missing_ok=True)
        except OSError:
            continue
