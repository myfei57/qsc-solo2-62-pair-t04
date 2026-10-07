"""Append only defrost ledger and its projections.

The ledger is the single source of truth for every defrost. Events are
immutable: once a session reaches a terminal state (``completed`` or
``aborted``) the store rejects any further event for it, so a finished record
can never be edited, truncated or overwritten. State is materialised by
folding the event stream, which makes replay deterministic — replaying the
same command against the same ledger resumes or refuses the session instead
of manufacturing a second warm up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Stable flow states derived from the event stream.
IDLE = "idle"
STOPPING = "stopping_fans"
FANS_OFF = "fans_stopped"
HEATING = "heating"
DRAINING = "draining"
COMPLETED = "completed"
ABORTED = "aborted"

TERMINAL_STATES = frozenset({COMPLETED, ABORTED})

#: Event kinds, in the order a healthy session emits them.
STARTED = "started"
FAN_STOPPED = "fan_stopped"
FAN_STOP_FAILED = "fan_stop_failed"
FAN_RESTORE_SKIPPED = "fan_restore_skipped"
HEAT_BEGAN = "heat_began"
HEAT_SAMPLE = "heat_sample"
DRAIN_BEGAN = "drain_began"
COMPLETED_EVENT = "completed"
ABORTED_EVENT = "aborted"

#: Events that end a session.
TERMINAL_EVENTS = frozenset({COMPLETED_EVENT, ABORTED_EVENT})

#: State a session must be in before each event may be appended.
_ALLOWED_PREVIOUS = {
    STARTED: frozenset({None}),
    FAN_STOPPED: frozenset({STOPPING}),
    HEAT_BEGAN: frozenset({FANS_OFF}),
    HEAT_SAMPLE: frozenset({HEATING}),
    DRAIN_BEGAN: frozenset({HEATING}),
    COMPLETED_EVENT: frozenset({DRAINING}),
    ABORTED_EVENT: frozenset({STOPPING, FANS_OFF, HEATING, DRAINING}),
}

_STATE_AFTER = {
    STARTED: STOPPING,
    FAN_STOPPED: FANS_OFF,
    HEAT_BEGAN: HEATING,
    HEAT_SAMPLE: HEATING,
    DRAIN_BEGAN: DRAINING,
    COMPLETED_EVENT: COMPLETED,
    ABORTED_EVENT: ABORTED,
}


@dataclass(frozen=True)
class RollbackPoint:
    """Stable fan state captured before a defrost touches hardware."""

    running_fans: list[str]
    primary: str
    captured_at: int

    def as_dict(self) -> dict[str, object]:
        return {
            "running_fans": list(self.running_fans),
            "primary": self.primary,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "RollbackPoint":
        data = payload if isinstance(payload, dict) else {}
        fans = data.get("running_fans", [])
        return cls(
            running_fans=[str(item) for item in fans] if isinstance(fans, list) else [],
            primary=str(data.get("primary", "")),
            captured_at=int(data.get("captured_at", 0) or 0),
        )


@dataclass
class Event:
    """One immutable ledger entry."""

    seq: int
    session_id: str
    room_id: str
    kind: str
    at: int
    detail: str
    payload: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "session_id": self.session_id,
            "room_id": self.room_id,
            "kind": self.kind,
            "at": self.at,
            "detail": self.detail,
            "payload": self.payload,
        }


@dataclass
class Session:
    """Materialised trajectory of one defrost."""

    session_id: str
    room_id: str
    state: str
    trigger: str
    started_at: int
    updated_at: int
    events: list[Event] = field(default_factory=list)
    rollback_point: RollbackPoint | None = None
    stop_confirmed_at: int = 0
    heat_started_at: int = 0
    last_temp: float = 0.0
    samples: int = 0
    drain_started_at: int = 0
    finished_at: int = 0
    outcome: str = ""
    reason: str = ""

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def trajectory(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "room_id": self.room_id,
            "state": self.state,
            "trigger": self.trigger,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "stop_confirmed_at": self.stop_confirmed_at,
            "heat_started_at": self.heat_started_at,
            "last_temp": self.last_temp,
            "samples": self.samples,
            "drain_started_at": self.drain_started_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome,
            "reason": self.reason,
            "rollback_point": self.rollback_point.as_dict() if self.rollback_point else None,
            "events": [event.as_dict() for event in self.events],
        }


class LedgerError(ValueError):
    """Raised when an event would violate the ledger state machine."""


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def event_from_dict(payload: object, seq: int) -> Event | None:
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("kind", ""))
    session_id = str(payload.get("session_id", ""))
    if not kind or not session_id:
        return None
    nested = payload.get("payload", {})
    return Event(
        seq=seq,
        session_id=session_id,
        room_id=str(payload.get("room_id", "")),
        kind=kind,
        at=_coerce_int(payload.get("at")),
        detail=str(payload.get("detail", "")),
        payload=dict(nested) if isinstance(nested, dict) else {},
    )


class Ledger:
    """Folds the persisted event stream into sessions and guards append rules."""

    def __init__(self, events: list[Event]) -> None:
        self._events = events
        self._sessions: dict[str, Session] = {}
        self._fold()

    def _fold(self) -> None:
        for event in self._events:
            session = self._sessions.get(event.session_id)
            if session is None:
                if event.kind != STARTED:
                    continue
                session = Session(
                    session_id=event.session_id,
                    room_id=event.room_id,
                    state=STOPPING,
                    trigger=str(event.payload.get("trigger", "manual")),
                    started_at=event.at,
                    updated_at=event.at,
                    events=[event],
                    rollback_point=RollbackPoint.from_dict(event.payload.get("rollback_point")),
                )
                self._sessions[event.session_id] = session
                continue
            session.events.append(event)
            session.updated_at = event.at
            self._apply(session, event)

    @staticmethod
    def _apply(session: Session, event: Event) -> None:
        session.state = _STATE_AFTER.get(event.kind, session.state)
        if event.kind == FAN_STOPPED:
            session.stop_confirmed_at = event.at
        elif event.kind == HEAT_BEGAN:
            session.heat_started_at = event.at
            session.last_temp = _coerce_float(event.payload.get("temp"))
            session.samples += 1
        elif event.kind == HEAT_SAMPLE:
            session.last_temp = _coerce_float(event.payload.get("temp"), session.last_temp)
            session.samples += 1
        elif event.kind == DRAIN_BEGAN:
            session.drain_started_at = event.at
        elif event.kind in TERMINAL_EVENTS:
            session.finished_at = event.at
            session.outcome = event.kind
            session.reason = event.detail

    def events(self) -> list[Event]:
        return list(self._events)

    def sessions(self) -> list[Session]:
        return list(self._sessions.values())

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def active_for_room(self, room_id: str) -> Session | None:
        for session in self._sessions.values():
            if session.room_id == room_id and not session.is_terminal():
                return session
        return None

    def active_sessions(self) -> list[Session]:
        return [session for session in self._sessions.values() if not session.is_terminal()]

    def active_rooms(self) -> list[str]:
        return [session.room_id for session in self.active_sessions()]

    def for_room(self, room_id: str) -> list[Session]:
        return [
            session
            for session in self._sessions.values()
            if session.room_id == room_id
        ]

    def next_seq(self) -> int:
        if not self._events:
            return 1
        return self._events[-1].seq + 1

    def apply(self, event: Event) -> None:
        """Fold one newly persisted event into this read model in place."""

        self._events.append(event)
        session = self._sessions.get(event.session_id)
        if session is None:
            if event.kind != STARTED:
                return
            session = Session(
                session_id=event.session_id,
                room_id=event.room_id,
                state=STOPPING,
                trigger=str(event.payload.get("trigger", "manual")),
                started_at=event.at,
                updated_at=event.at,
                events=[event],
                rollback_point=RollbackPoint.from_dict(event.payload.get("rollback_point")),
            )
            self._sessions[event.session_id] = session
            return
        session.events.append(event)
        session.updated_at = event.at
        self._apply(session, event)

    def assert_append_allowed(self, session_id: str, kind: str) -> Session | None:
        """Validate the transition without persisting anything.

        Returns the current session, or ``None`` for a valid ``started``
        event that opens a brand new session.
        """

        session = self._sessions.get(session_id)
        if session is not None and session.is_terminal():
            raise LedgerError(
                f"defrost session {session_id} is {session.state} and cannot be modified"
            )
        allowed = _ALLOWED_PREVIOUS.get(kind)
        if allowed is None:
            raise LedgerError(f"unknown defrost event kind {kind}")
        previous = session.state if session is not None else None
        if previous not in allowed:
            raise LedgerError(
                f"defrost event {kind} not allowed in state {previous or 'idle'}"
            )
        return session
