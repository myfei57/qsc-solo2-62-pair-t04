"""Append only, hash chained defrost ledger.

Every state transition of every defrost session is appended as one event. The
ledger is the system of record for "what actually happened": it is never
rewritten, entries are never deleted, and a completed record cannot be edited.

Tamper evidence comes from a hash chain: each event stores the SHA-256 of the
previous event's canonical form, and the first event anchors on a fixed
genesis hash. Replaying the chain recomputes every link, so changing or
reordering a historical event is detectable at read time.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from coldstore.store.commands import append_command, load_commands
from coldstore.store.store import Store

LEDGER_KEY = "defrost:ledger"
GENESIS_HASH = "0" * 64

# Event kinds written along the lifecycle. ``sealed`` closes a record; its
# result is one of the terminal outcomes.
SEAL_RESULTS = ("completed", "aborted")


@dataclass(frozen=True)
class LedgerEvent:
    """One immutable transition in a defrost session."""

    seq: int
    event_id: str
    session_id: str
    room_id: str
    at: int
    kind: str
    stage: str
    detail: str
    prev_hash: str
    entry_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "room_id": self.room_id,
            "at": self.at,
            "kind": self.kind,
            "stage": self.stage,
            "detail": self.detail,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


@dataclass(frozen=True)
class DefrostRecord:
    """The full trajectory of one defrost session, assembled from events."""

    session_id: str
    room_id: str
    started_at: int
    ended_at: int
    stage: str
    sealed: bool
    result: str
    reason: str
    trigger: str
    frost_load: float
    events: list[LedgerEvent]

    def as_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "room_id": self.room_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "stage": self.stage,
            "sealed": self.sealed,
            "result": self.result,
            "reason": self.reason,
            "trigger": self.trigger,
            "frost_load": self.frost_load,
            "events": [event.as_dict() for event in self.events],
        }


def canonical_payload(event: LedgerEvent) -> str:
    """Stable serialization of everything the hash commits to."""

    return json.dumps(
        {
            "seq": event.seq,
            "event_id": event.event_id,
            "session_id": event.session_id,
            "room_id": event.room_id,
            "at": event.at,
            "kind": event.kind,
            "stage": event.stage,
            "detail": event.detail,
            "prev_hash": event.prev_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def hash_payload(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DefrostLedger:
    """Appends lifecycle events and reconstructs room trajectories."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def append(
        self,
        session_id: str,
        room_id: str,
        kind: str,
        stage: str,
        detail: str = "",
        at: int | None = None,
    ) -> LedgerEvent:
        events = self.events()
        seq = len(events)
        prev_hash = events[-1].entry_hash if events else GENESIS_HASH
        event = LedgerEvent(
            seq=seq,
            event_id=uuid.uuid4().hex,
            session_id=session_id,
            room_id=room_id,
            at=int(at) if at is not None else _now(),
            kind=kind,
            stage=stage,
            detail=detail,
            prev_hash=prev_hash,
            entry_hash="",
        )
        entry_hash = hash_payload(canonical_payload(event))
        event = LedgerEvent(
            seq=event.seq,
            event_id=event.event_id,
            session_id=event.session_id,
            room_id=event.room_id,
            at=event.at,
            kind=event.kind,
            stage=event.stage,
            detail=event.detail,
            prev_hash=event.prev_hash,
            entry_hash=entry_hash,
        )
        append_command(self._store, LEDGER_KEY, json.dumps(event.as_dict(), ensure_ascii=False))
        return event

    def events(self) -> list[LedgerEvent]:
        events: list[LedgerEvent] = []
        for item in load_commands(self._store, LEDGER_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            events.append(
                LedgerEvent(
                    seq=int(payload.get("seq", 0)),
                    event_id=str(payload.get("event_id", "")),
                    session_id=str(payload.get("session_id", "")),
                    room_id=str(payload.get("room_id", "")),
                    at=int(payload.get("at", 0)),
                    kind=str(payload.get("kind", "")),
                    stage=str(payload.get("stage", "")),
                    detail=str(payload.get("detail", "")),
                    prev_hash=str(payload.get("prev_hash", "")),
                    entry_hash=str(payload.get("entry_hash", "")),
                )
            )
        return events

    def records(self) -> list[DefrostRecord]:
        """Reconstruct every session record in session start order."""

        grouped: dict[str, list[LedgerEvent]] = {}
        order: list[str] = []
        for event in self.events():
            if event.session_id not in grouped:
                grouped[event.session_id] = []
                order.append(event.session_id)
            grouped[event.session_id].append(event)

        records: list[DefrostRecord] = []
        for session_id in order:
            events = grouped[session_id]
            first = events[0]
            last = events[-1]
            sealed = last.kind == "sealed" and last.stage in SEAL_RESULTS
            result = last.stage if sealed else ""
            reason = last.detail if sealed else ""
            trigger = first.stage if first.kind == "triggered" else ""
            frost_load = 0.0
            if first.kind == "triggered":
                try:
                    frost_load = float(first.detail)
                except ValueError:
                    frost_load = 0.0
            records.append(
                DefrostRecord(
                    session_id=session_id,
                    room_id=first.room_id,
                    started_at=first.at,
                    ended_at=last.at if sealed else 0,
                    stage=last.stage,
                    sealed=sealed,
                    result=result,
                    reason=reason,
                    trigger=trigger,
                    frost_load=frost_load,
                    events=events,
                )
            )
        return records

    def records_for_room(self, room_id: str) -> list[DefrostRecord]:
        return [record for record in self.records() if record.room_id == room_id]

    def record(self, session_id: str) -> DefrostRecord | None:
        for record in self.records():
            if record.session_id == session_id:
                return record
        return None

    def verify_chain(self) -> tuple[bool, int]:
        """Recompute every link. Returns (intact, first_broken_seq)."""

        expected_prev = GENESIS_HASH
        for event in self.events():
            if event.prev_hash != expected_prev:
                return False, event.seq
            if hash_payload(canonical_payload(event)) != event.entry_hash:
                return False, event.seq
            expected_prev = event.entry_hash
        return True, -1

    def count(self) -> int:
        return len(self.records())

    def event_count(self) -> int:
        return len(self.events())


def _now() -> int:
    import time

    return int(time.time())
