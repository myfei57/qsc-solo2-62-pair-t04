"""Defrost controller.

A defrost is one staged flow driven from an append only ledger:

    started -> fan_stopped -> heat_began -> heat_sample* -> drain_began
            -> completed
        any non terminal state -> aborted (rolled back)

Rules enforced here:

* Triggering is judged first (frost integral, with a time based safety net);
  a forced start is allowed but records ``trigger=manual``.
* Heating begins only after every cold fan has been confirmed stopped.
* A single defrost runs end to end (single stage), ending on coil end
  temperature plus a drain dwell; staged heating is intentionally not
  supported because re-freezing between stages wastes energy and leaves
  ambiguous state.
* An abort restores the fan snapshot captured at start — the previous
  stable state — so a cancelled defrost leaves no half switched hardware.
* Terminal ledger records are immutable and the queue is idempotent, so
  replay against the same store never duplicates a warm up.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Callable

from coldstore.fan import FanSet
from coldstore.frost import FrostEstimator
from coldstore.room import RoomRegistry
from coldstore.store.commands import append_command, load_commands
from coldstore.store.epoch import load_float, save_float
from coldstore.store.store import Store

from .ledger import (
    ABORTED_EVENT,
    COMPLETED_EVENT,
    DRAIN_BEGAN,
    Event,
    FAN_RESTORE_SKIPPED,
    FAN_STOP_FAILED,
    FAN_STOPPED,
    HEAT_BEGAN,
    HEAT_SAMPLE,
    Ledger,
    LedgerError,
    RollbackPoint,
    STARTED,
    STOPPING,
)
from .record import append_event, load_ledger
from .report import DefrostState, validate_end_temp, validate_positive_seconds

COMMAND_KEY = "defrost:commands"
END_TEMP_KEY = "defrost:end-temp"
MAX_HEAT_KEY = "defrost:max-heat-seconds"
DWELL_KEY = "defrost:drain-seconds"

DEFAULT_END_TEMP = 8.0
DEFAULT_MAX_HEAT_SECONDS = 1_800.0
DEFAULT_DRAIN_SECONDS = 120.0


class Controller:
    """Sequences defrost steps against the cold fans and rooms."""

    def __init__(
        self,
        fans: FanSet,
        rooms: RoomRegistry,
        store: Store,
        frost: FrostEstimator | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._fans = fans
        self._rooms = rooms
        self._store = store
        self._clock = clock
        self._frost = frost or FrostEstimator(store, clock)
        self._lock = threading.RLock()
        self._sync_room_flags()

    # -- clock and config -------------------------------------------------

    def _now(self) -> int:
        if self._clock is not None:
            return int(self._clock())
        return int(time.time())

    def end_temp(self) -> float:
        value, present = load_float(self._store, END_TEMP_KEY)
        return value if present else DEFAULT_END_TEMP

    def set_end_temp(self, value: float) -> float:
        validate_end_temp(value)
        save_float(self._store, END_TEMP_KEY, value)
        return value

    def max_heat_seconds(self) -> float:
        value, present = load_float(self._store, MAX_HEAT_KEY)
        return value if present else DEFAULT_MAX_HEAT_SECONDS

    def set_max_heat_seconds(self, value: float) -> float:
        validate_positive_seconds(value)
        save_float(self._store, MAX_HEAT_KEY, value)
        return value

    def drain_seconds(self) -> float:
        value, present = load_float(self._store, DWELL_KEY)
        return value if present else DEFAULT_DRAIN_SECONDS

    def set_drain_seconds(self, value: float) -> float:
        validate_positive_seconds(value)
        save_float(self._store, DWELL_KEY, value)
        return value

    # -- trigger -----------------------------------------------------------

    def trigger(self, room_id: str, force: bool = False):
        """Frost verdict for a room: integral trigger first, interval second."""

        self._require_room(room_id)
        return self._frost.verdict(room_id, at=self._now(), force=force)

    def due(self, room_id: str) -> bool:
        return self.trigger(room_id).due

    def observe_frost(self, room_id: str, room_temp: float, coil_temp: float,
                      at: int | None = None) -> float:
        """Feed one probe pair; the integral only grows while the fan runs."""

        self._require_room(room_id)
        moment = self._now() if at is None else at
        running = bool(self._fans.running_ids())
        return self._frost.observe(room_id, room_temp, coil_temp, running, at=moment)

    # -- flow --------------------------------------------------------------

    def start(self, room_id: str, force: bool = False) -> dict[str, object]:
        """Open a session: judge the trigger, snapshot fans, then stop them.

        The session lands in ``stopping_fans``; call :meth:`confirm_fans_stopped`
        before any heating is allowed.
        """

        with self._lock:
            self._require_room(room_id)
            ledger = load_ledger(self._store)
            if ledger.active_for_room(room_id) is not None:
                raise LedgerError(f"cold room {room_id} already has an active defrost")

            moment = self._now()
            verdict = self._frost.verdict(room_id, at=moment, force=force)
            if not verdict.due and not force:
                raise LedgerError(
                    f"cold room {room_id} is not due for defrost ({verdict.reason})"
                )

            point = RollbackPoint(
                running_fans=self._fans.running_ids(),
                primary=self._fans.primary(),
                captured_at=moment,
            )
            session_id = self._new_session_id()
            event = Event(
                seq=ledger.next_seq(),
                session_id=session_id,
                room_id=room_id,
                kind=STARTED,
                at=moment,
                detail=f"trigger={verdict.reason} load={verdict.load:.1f}",
                payload={
                    "trigger": verdict.reason,
                    "frost_load": verdict.load,
                    "rollback_point": point.as_dict(),
                },
            )
            ledger.assert_append_allowed(session_id, STARTED)
            append_event(self._store, event)

            self._fans.stop_all()
            self._rooms.set_defrosting(room_id, True)
            return {
                "session_id": session_id,
                "room_id": room_id,
                "state": STOPPING,
                "trigger": verdict.reason,
                "rollback_point": point.as_dict(),
            }

    def confirm_fans_stopped(self, room_id: str) -> dict[str, object]:
        """Record the fan stopped confirmation; refuses to lie about hardware."""

        with self._lock:
            ledger = load_ledger(self._store)
            session = self._require_active(ledger, room_id)
            running = self._fans.running_ids()
            if running:
                self._append_fan_failure(ledger, session, running)
                raise LedgerError(
                    f"fans still running before heating: {', '.join(running)}"
                )
            self._append(ledger, session, FAN_STOPPED, detail="all cold fans stopped")
            return self._session_view(room_id)

    def begin_heat(self, room_id: str) -> dict[str, object]:
        """Begin heating; only legal after the fan stop confirmation."""

        with self._lock:
            ledger = load_ledger(self._store)
            session = self._require_active(ledger, room_id)
            if self._fans.running_ids():
                self.abort(room_id, reason="fan detected running at heat start")
                raise LedgerError("heating refused: a cold fan is still running")
            self._append(
                ledger,
                session,
                HEAT_BEGAN,
                detail="heating began",
                payload={"temp": self._coil_temp(room_id)},
            )
            return self._session_view(room_id)

    def heat_sample(self, room_id: str, coil_temp: float,
                    at: int | None = None) -> dict[str, object]:
        """Record one coil temperature sample during heating.

        Reaching the configured end temperature moves the session to draining;
        exceeding the maximum heating window aborts and rolls it back.
        """

        with self._lock:
            ledger = load_ledger(self._store)
            session = self._require_active(ledger, room_id)
            moment = self._now() if at is None else at
            elapsed = max(0, moment - session.heat_started_at)
            if elapsed > self.max_heat_seconds():
                self._abort_locked(
                    ledger,
                    session,
                    reason=f"max heating time {self.max_heat_seconds():.0f}s exceeded",
                )
                raise LedgerError("defrost aborted: heating took too long")
            self._append(
                ledger,
                session,
                HEAT_SAMPLE,
                detail=f"coil={coil_temp:.2f}",
                payload={"temp": coil_temp, "elapsed": elapsed},
                at=moment,
            )
            if coil_temp >= self.end_temp():
                self._append(ledger, session, DRAIN_BEGAN,
                             detail=f"end temp {self.end_temp():.2f} reached", at=moment)
            return self._session_view(room_id)

    def complete(self, room_id: str, at: int | None = None) -> dict[str, object]:
        """Finish after the drain dwell elapses and restore stable duty."""

        with self._lock:
            ledger = load_ledger(self._store)
            session = self._require_active(ledger, room_id)
            moment = self._now() if at is None else at
            dwell = max(0, moment - session.drain_started_at)
            if dwell < self.drain_seconds():
                raise LedgerError(
                    f"drain dwell incomplete: {dwell:.0f}/{self.drain_seconds():.0f}s"
                )
            restored = self._restore_fans(session)
            self._frost.commit_defrost(room_id, at=moment)
            self._append(
                ledger,
                session,
                COMPLETED_EVENT,
                detail=f"restored fans={','.join(restored) or 'none'}",
                payload={"restored_fans": restored},
                at=moment,
            )
            self._rooms.set_defrosting(room_id, False)
            self._discard_command(room_id)
            return self._session_view(room_id)

    def finish(self, room_id: str, at: int | None = None) -> dict[str, object]:
        """Complete a drained defrost (historical name, see :meth:`complete`)."""

        return self.complete(room_id, at=at)

    def abort(self, room_id: str, reason: str = "operator cancelled") -> dict[str, object]:
        """Abort an active defrost and roll back to the captured stable state."""

        with self._lock:
            ledger = load_ledger(self._store)
            session = self._require_active(ledger, room_id)
            return self._abort_locked(ledger, session, reason)

    # -- queue, replay, recovery ------------------------------------------

    def enqueue(self, room_id: str) -> None:
        """Queue one defrost; duplicates for a queued room are ignored."""

        self._require_room(room_id)
        with self._lock:
            if room_id in self.command_list():
                return
            append_command(self._store, COMMAND_KEY, room_id)

    def replay(self) -> list[dict[str, object]]:
        """Re-run queued defrosts deterministically.

        Each command opens a session only when none exists for the room, so
        replaying the same store resumes in-flight work instead of producing
        duplicate warm ups. Commands are reported with the action taken.
        """

        with self._lock:
            results: list[dict[str, object]] = []
            for room_id in self.command_list():
                ledger = load_ledger(self._store)
                active = ledger.active_for_room(room_id)
                if active is not None:
                    results.append(
                        {"room_id": room_id, "action": "resumed",
                         "session_id": active.session_id, "state": active.state}
                    )
                    continue
                try:
                    opened = self.start(room_id, force=True)
                    results.append(
                        {"room_id": room_id, "action": "started",
                         "session_id": opened["session_id"], "state": opened["state"]}
                    )
                except LedgerError as exc:
                    results.append(
                        {"room_id": room_id, "action": "rejected", "reason": str(exc)}
                    )
            return results

    def recover(self) -> dict[str, object]:
        """Roll back active sessions left by an interrupted console.

        Aborts every in-flight session (restoring its fan snapshot), clears
        the pending queue and rebuilds the in memory defrosting flags from the
        ledger. Terminal records are left untouched.
        """

        with self._lock:
            rolled_back: list[str] = []
            while True:
                ledger = load_ledger(self._store)
                active = ledger.active_sessions()
                if not active:
                    break
                session = active[0]
                self._abort_locked(
                    ledger, session, reason="recovered after interrupted console"
                )
                rolled_back.append(session.room_id)
            self._store.delete(COMMAND_KEY)
            self._sync_room_flags()
            return {"recovered": True, "rolled_back": rolled_back}

    # -- queries -----------------------------------------------------------

    def command_list(self) -> list[str]:
        return [str(item) for item in load_commands(self._store, COMMAND_KEY)]

    def pending_count(self) -> int:
        return len(self.command_list())

    def select(self, zone: int) -> str:
        """Resolve the room that currently occupies a zone."""

        mapping = self._rooms.mapping()
        if zone not in mapping:
            raise ValueError(f"no cold room for zone {zone}")
        return mapping[zone]

    def defrosting(self) -> list[str]:
        return self._rooms.defrosting_ids()

    def warmups(self) -> list[str]:
        """Distinct rooms whose defrost sessions ever reached heating."""

        ledger = load_ledger(self._store)
        warmed: list[str] = []
        for session in ledger.sessions():
            if session.heat_started_at and session.room_id not in warmed:
                warmed.append(session.room_id)
        return warmed

    def warmup_count(self) -> int:
        return len(self.warmups())

    def last_warmup(self) -> str | None:
        warmups = self.warmups()
        if not warmups:
            return None
        return warmups[-1]

    def spilled(self) -> bool:
        """True when any session recorded a fan still running at heat start."""

        ledger = load_ledger(self._store)
        for session in ledger.sessions():
            for event in session.events:
                if event.kind == FAN_STOP_FAILED or (
                    event.kind == ABORTED_EVENT and "fan detected running" in event.detail
                ):
                    return True
        return False

    def trajectory(self, room_id: str) -> list[dict[str, object]]:
        """Full trajectory of every defrost ever recorded for one room.

        The ledger is durable even when the in memory room registry has not
        been repopulated yet, so historical rooms remain queryable.
        """

        ledger = load_ledger(self._store)
        return [session.trajectory() for session in ledger.for_room(room_id)]

    def sessions(self) -> list[dict[str, object]]:
        ledger = load_ledger(self._store)
        return [session.trajectory() for session in ledger.sessions()]

    def state(self) -> DefrostState:
        ledger = load_ledger(self._store)
        sessions = ledger.sessions()
        completed = sum(session.state == "completed" for session in sessions)
        aborted = sum(session.state == "aborted" for session in sessions)
        active = [session.trajectory() for session in ledger.active_sessions()]
        return DefrostState(
            commands=self.command_list(),
            defrosting=ledger.active_rooms(),
            active=active,
            completed=completed,
            aborted=aborted,
            sessions=len(sessions),
            warmups=list(
                dict.fromkeys(
                    session.room_id for session in sessions if session.heat_started_at
                )
            ),
        )

    def describe(self) -> str:
        state = self.state()
        return (
            f"defrost commands={len(state.commands)} sessions={state.sessions} "
            f"active={len(state.defrosting)} completed={state.completed} "
            f"aborted={state.aborted}"
        )

    # -- internals ---------------------------------------------------------

    def _require_room(self, room_id: str) -> None:
        if self._rooms.room(room_id) is None:
            raise ValueError(f"cold room {room_id} not found")

    def _require_active(self, ledger: Ledger, room_id: str):
        session = ledger.active_for_room(room_id)
        if session is None:
            raise LedgerError(f"no active defrost for cold room {room_id}")
        return session

    def _new_session_id(self) -> str:
        return f"df-{self._now()}-{uuid.uuid4().hex[:8]}"

    def _coil_temp(self, room_id: str) -> float:
        """Best available coil proxy: the room's stored air temperature."""

        value, present = self._rooms.temp(room_id)
        return value if present else 0.0

    def _append(self, ledger: Ledger, session, kind: str, detail: str,
                payload: dict[str, object] | None = None,
                at: int | None = None) -> Event:
        ledger.assert_append_allowed(session.session_id, kind)
        event = Event(
            seq=ledger.next_seq(),
            session_id=session.session_id,
            room_id=session.room_id,
            kind=kind,
            at=self._now() if at is None else at,
            detail=detail,
            payload=payload or {},
        )
        append_event(self._store, event)
        # Keep the passed read model usable for a follow-up append in the same call.
        ledger.apply(event)
        return event

    def _append_fan_failure(self, ledger: Ledger, session, running: list[str]) -> None:
        """Record a failed stop confirmation as an ordinary session event.

        It does not advance the state machine, so the session stays in
        ``stopping_fans`` and can still be confirmed or aborted.
        """

        event = Event(
            seq=ledger.next_seq(),
            session_id=session.session_id,
            room_id=session.room_id,
            kind=FAN_STOP_FAILED,
            at=self._now(),
            detail=f"fans still running: {', '.join(running)}",
            payload={"running_fans": running},
        )
        append_event(self._store, event)
        ledger.apply(event)

    def _abort_locked(self, ledger: Ledger, session, reason: str) -> dict[str, object]:
        restored = self._restore_fans(session)
        self._append(
            ledger,
            session,
            ABORTED_EVENT,
            detail=reason,
            payload={"restored_fans": restored},
        )
        self._rooms.set_defrosting(session.room_id, False)
        self._discard_command(session.room_id)
        return self._session_view(session.room_id)

    def _restore_fans(self, session) -> list[str]:
        """Roll hardware back to the stable state captured before the defrost.

        Only fans still present and healthy are restarted. A fan that was
        removed or faulted mid defrost cannot be forced back on; it is skipped
        but still listed in the ledger so the skipped hardware is visible.
        """

        restored: list[str] = []
        skipped: list[str] = []
        point = session.rollback_point
        if point is None:
            return restored
        for fan_id in point.running_fans:
            unit = self._fans.fan(fan_id)
            if unit is None or unit.faulted:
                skipped.append(fan_id)
                continue
            self._fans.start(fan_id)
            restored.append(fan_id)
        if skipped:
            event = Event(
                seq=load_ledger(self._store).next_seq(),
                session_id=session.session_id,
                room_id=session.room_id,
                kind=FAN_RESTORE_SKIPPED,
                at=self._now(),
                detail=f"fans not restored: {', '.join(skipped)}",
                payload={"skipped_fans": skipped},
            )
            append_event(self._store, event)
        return restored

    def _discard_command(self, room_id: str) -> None:
        queued = [item for item in self.command_list() if item != room_id]
        if queued:
            self._store.put(
                COMMAND_KEY,
                json.dumps(queued, ensure_ascii=False),
            )
        else:
            self._store.delete(COMMAND_KEY)

    def _session_view(self, room_id: str) -> dict[str, object]:
        ledger = load_ledger(self._store)
        session = ledger.active_for_room(room_id)
        if session is None:
            finished = ledger.for_room(room_id)
            if not finished:
                raise LedgerError(f"no defrost session for cold room {room_id}")
            return finished[-1].trajectory()
        return session.trajectory()

    def sync_room_flags(self) -> None:
        """Rebuild the in memory defrosting flags from the durable ledger."""

        with self._lock:
            self._sync_room_flags()

    def _sync_room_flags(self) -> None:
        """Rebuild the in memory defrosting flags from durable ledger state."""

        ledger = load_ledger(self._store)
        active_rooms = set(ledger.active_rooms())
        for room in self._rooms.rooms():
            self._rooms.set_defrosting(room.id, room.id in active_rooms)
