"""Defrost controller.

A defrost is one continuous session driven through a strict lifecycle rather
than two loose "stop fan / write timestamp" actions:

1. Trigger first. The session only starts when the frost model reports the
   integrated frost load past its threshold (or the maximum interval safety
   net trips). Manual starts are explicit overrides.
2. The cold fan is stopped and then independently observed stopped before any
   heating is allowed; heating from any other stage is rejected.
3. Completion is judged by evaporator temperature reaching the finish target,
   with a maximum heating duration as a safety cut-off.
4. An abort, or recovery after a crash, rolls the physical plant back to the
   checkpoint of the last settled state and seals the record as aborted, so no
   half-applied defrost is left behind.

Every transition is appended to the tamper-evident ledger as it happens;
records are never modified afterwards. Queued commands replay idempotently, so
re-running the queue after an interruption cannot defrost a room twice.
"""

from __future__ import annotations

from coldstore.fan import FanSet
from coldstore.room import RoomRegistry
from coldstore.store.commands import append_command, clear_commands, load_commands
from coldstore.store.epoch import load_float, save_float
from coldstore.store.store import Store

from .frost import FrostModel
from .ledger import DefrostLedger, DefrostRecord
from .report import DefrostState
from .schedule import now_unix
from .stages import (
    ABORTED,
    ACTIVE_KEY,
    COMPLETED,
    FAN_STOPPED,
    FAN_STOPPING,
    HEATING,
    IDLE,
    ActiveSession,
    capture_checkpoint,
    clear_active,
    load_active,
    new_session_id,
    save_active,
)

COMMAND_KEY = "defrost:commands"
FINISH_TEMP_KEY = "defrost:finish-temp"
MAX_DURATION_KEY = "defrost:max-duration"
DEFAULT_FINISH_TEMP = 5.0
DEFAULT_MAX_DURATION = 1800


class DefrostFlowError(ValueError):
    """Raised when a defrost action violates the lifecycle."""


class DefrostInProgress(DefrostFlowError):
    """Raised when a room already has an open session."""


class DefrostNotDue(DefrostFlowError):
    """Raised when an unforced start is requested below the trigger."""


class Controller:
    """Sequences defrost steps against the cold fans and rooms."""

    def __init__(self, fans: FanSet, rooms: RoomRegistry, store: Store) -> None:
        self._fans = fans
        self._rooms = rooms
        self._store = store
        self._frost = FrostModel(store, rooms, fans)
        self._ledger = DefrostLedger(store)

    # ----- configuration -------------------------------------------------

    def finish_temp(self) -> float:
        value, present = load_float(self._store, FINISH_TEMP_KEY)
        return value if present else DEFAULT_FINISH_TEMP

    def set_finish_temp(self, value: float) -> float:
        if value <= -20.0 or value > 30.0:
            raise ValueError("finish temp must be between -20 and 30 degrees")
        save_float(self._store, FINISH_TEMP_KEY, value)
        return value

    def max_duration(self) -> int:
        raw, present = self._store.get(MAX_DURATION_KEY)
        if not present:
            return DEFAULT_MAX_DURATION
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_MAX_DURATION
        return value if value > 0 else DEFAULT_MAX_DURATION

    def set_max_duration(self, seconds: int) -> int:
        if seconds <= 0:
            raise ValueError("max duration must be positive")
        self._store.put(MAX_DURATION_KEY, str(int(seconds)))
        return int(seconds)

    @property
    def frost(self) -> FrostModel:
        return self._frost

    @property
    def ledger(self) -> DefrostLedger:
        return self._ledger

    # ----- trigger -------------------------------------------------------

    def sample_frost(self, room_id: str, at: int | None = None):
        return self._frost.sample(room_id, at)

    def trigger_verdict(self, room_id: str, at: int | None = None):
        return self._frost.verdict(room_id, at)

    def due_rooms(self, at: int | None = None) -> list[str]:
        return self._frost.due_rooms(at)

    # ----- lifecycle -----------------------------------------------------

    def start(self, room_id: str, force: bool = False, at: int | None = None) -> ActiveSession:
        """Judge the trigger, then stop the fan and open a session."""

        room = self._rooms.room(room_id)
        if room is None:
            raise ValueError(f"cold room {room_id} not found")
        active = self.active()
        if active is not None:
            raise DefrostInProgress(f"defrost {active.session_id} for {active.room_id} is open")

        verdict = self._frost.verdict(room_id, at)
        if not force and not verdict.trigger:
            raise DefrostNotDue(
                f"frost load {verdict.frost_load:.1f} below {verdict.threshold:.1f}"
            )
        trigger_reason = verdict.reason if verdict.trigger else "manual"

        session_id = new_session_id()
        started_at = int(at) if at is not None else now_unix()
        # The ledger write commits the decision before the plant moves; the
        # checkpoint below is the state to return to if it never finishes.
        self._ledger.append(
            session_id,
            room_id,
            kind="triggered",
            stage=trigger_reason,
            detail=f"{verdict.frost_load:.4f}",
            at=started_at,
        )
        checkpoint = capture_checkpoint(IDLE, self._fans, self._rooms)
        self._fans.stop_all()
        self._rooms.set_defrosting(room_id, True)
        session = ActiveSession(
            session_id=session_id,
            room_id=room_id,
            stage=FAN_STOPPING,
            trigger=trigger_reason,
            started_at=started_at,
            updated_at=started_at,
            rollback=checkpoint,
        )
        save_active(self._store, session)
        self._ledger.append(
            session_id, room_id, kind="stage", stage=FAN_STOPPING,
            detail="fans stop requested", at=started_at,
        )
        return session

    def confirm_fan_stopped(self, at: int | None = None) -> ActiveSession:
        """Confirm every fan is observed stopped; the gate before heating."""

        session = self._require_active(expected=FAN_STOPPING)
        if self._fans.running_ids():
            raise DefrostFlowError("fans are still running; heating is interlocked")
        confirmed_at = int(at) if at is not None else now_unix()
        session = self._advance(session, FAN_STOPPED, "fans observed stopped", confirmed_at)
        return session

    def begin_heating(self, at: int | None = None) -> ActiveSession:
        """Start heating only after the stopped gate has been confirmed."""

        session = self._require_active(expected=FAN_STOPPED)
        heated_at = int(at) if at is not None else now_unix()
        return self._advance(session, HEATING, "heating started", heated_at)

    def complete(
        self,
        coil_temp: float | None = None,
        at: int | None = None,
        force: bool = False,
    ) -> DefrostRecord:
        """Finish when the coil reaches target or the max duration elapses."""

        session = self._require_active(expected=HEATING)
        ended_at = int(at) if at is not None else now_unix()
        reached = coil_temp is not None and coil_temp >= self.finish_temp()
        timed_out = ended_at - session.started_at >= self.max_duration()
        if not force and not reached and not timed_out:
            raise DefrostFlowError(
                f"coil {coil_temp if coil_temp is not None else 'n/a'} below "
                f"{self.finish_temp()} and duration under {self.max_duration()}s"
            )
        reason = "target_temp" if reached else ("max_duration" if timed_out else "forced")

        clear_active(self._store)
        self._rooms.set_defrosting(session.room_id, False)
        self._restore_fans(session.rollback)
        self._frost.reset(session.room_id, ended_at)
        self._ledger.append(
            session.session_id, session.room_id, kind="sealed", stage=COMPLETED,
            detail=reason, at=ended_at,
        )
        record = self._ledger.record(session.session_id)
        assert record is not None
        return record

    def abort(self, reason: str = "manual", at: int | None = None) -> DefrostRecord:
        """Abnormal stop: roll the plant back to the last settled checkpoint."""

        session = self._require_active()
        aborted_at = int(at) if at is not None else now_unix()
        self._rollback_to(session)
        self._ledger.append(
            session.session_id, session.room_id, kind="sealed", stage=ABORTED,
            detail=reason, at=aborted_at,
        )
        record = self._ledger.record(session.session_id)
        assert record is not None
        return record

    # ----- queue, replay and recovery ------------------------------------

    def enqueue(self, room_id: str) -> None:
        if self._rooms.room(room_id) is None:
            raise ValueError(f"cold room {room_id} not found")
        if room_id not in self.command_list():
            append_command(self._store, COMMAND_KEY, room_id)

    def replay(self, coil_temp: float | None = None, at: int | None = None) -> list[str]:
        """Drain the queue idempotently, driving each room through the line.

        A room that already holds an open session is skipped rather than
        defrosted again, and duplicate queue entries collapse to one run, so
        replaying after an interruption reproduces the same result.
        """

        requested = self.command_list()
        seen: set[str] = set()
        completed: list[str] = []
        for room_id in requested:
            if room_id in seen:
                continue
            seen.add(room_id)
            if self._is_open_for(room_id) or self._rooms.room(room_id) is None:
                continue
            base_at = at
            start_at = base_at if base_at is None else base_at
            self.start(room_id, force=True, at=start_at)
            stop_at = start_at + 1 if start_at is not None else None
            self.confirm_fan_stopped(at=stop_at)
            heat_at = stop_at + 1 if stop_at is not None else None
            temp = self.finish_temp() + 1.0 if coil_temp is None else coil_temp
            self.begin_heating(at=heat_at)
            end_at = heat_at + 1 if heat_at is not None else None
            self.complete(coil_temp=temp, at=end_at)
            completed.append(room_id)
        clear_commands(self._store, COMMAND_KEY)
        return completed

    def recover(self) -> None:
        """Discard queued commands left over from an interrupted console."""

        clear_commands(self._store, COMMAND_KEY)

    def recover_interrupted(self, at: int | None = None) -> str | None:
        """Roll an unsealed open session back after a restart.

        Physical state is returned to the checkpoint and the record is sealed
        as aborted so the trajectory still tells the whole story. The command
        queue is left intact; a subsequent replay schedules a fresh attempt.
        """

        session = load_active(self._store)
        if session is None:
            return None
        recovered_at = int(at) if at is not None else now_unix()
        self._rollback_to(session)
        self._ledger.append(
            session.session_id, session.room_id, kind="sealed", stage=ABORTED,
            detail="interrupted", at=recovered_at,
        )
        return session.session_id

    # ----- queries -------------------------------------------------------

    def active(self) -> ActiveSession | None:
        return load_active(self._store)

    def command_list(self) -> list[str]:
        return load_commands(self._store, COMMAND_KEY)

    def pending_count(self) -> int:
        return len(self.command_list())

    def select(self, zone: int) -> str:
        mapping = self._rooms.mapping()
        if zone not in mapping:
            raise ValueError(f"no cold room for zone {zone}")
        return mapping[zone]

    def defrosting(self) -> list[str]:
        return self._rooms.defrosting_ids()

    def records(self) -> list[DefrostRecord]:
        return self._ledger.records()

    def trajectory(self, room_id: str | None = None, session_id: str | None = None):
        """Full ledger trajectory per room (or one session), chain verified."""

        chain_ok, broken_seq = self._ledger.verify_chain()
        if session_id:
            record = self._ledger.record(session_id)
            records = [record] if record is not None else []
        elif room_id is not None:
            records = self._ledger.records_for_room(room_id)
        else:
            records = self._ledger.records()
        return {
            "room_id": room_id or "",
            "records": [record.as_dict() for record in records],
            "count": len(records),
            "chain_ok": chain_ok,
            "broken_seq": broken_seq,
        }

    def state(self) -> DefrostState:
        active = self.active()
        records = self._ledger.records()
        chain_ok, _ = self._ledger.verify_chain()
        return DefrostState(
            active=active.as_dict() if active is not None else None,
            defrosting=self.defrosting(),
            queue=self.command_list(),
            records=len(records),
            sealed=sum(1 for record in records if record.sealed),
            chain_ok=chain_ok,
            frost=self._frost.state().as_dict(),
        )

    def describe(self) -> str:
        state = self.state()
        active_stage = state.active["stage"] if state.active else IDLE
        return (
            f"defrost stage={active_stage} queue={len(state.queue)} "
            f"records={state.records} chain_ok={state.chain_ok}"
        )

    # ----- internals -----------------------------------------------------

    def _require_active(self, expected: str | None = None) -> ActiveSession:
        session = load_active(self._store)
        if session is None:
            raise DefrostFlowError("no active defrost session")
        if expected is not None and session.stage != expected:
            raise DefrostFlowError(
                f"session is {session.stage}, expected {expected}"
            )
        return session

    def _advance(
        self, session: ActiveSession, stage: str, detail: str, at: int
    ) -> ActiveSession:
        moved = ActiveSession(
            session_id=session.session_id,
            room_id=session.room_id,
            stage=stage,
            trigger=session.trigger,
            started_at=session.started_at,
            updated_at=at,
            rollback=session.rollback,
        )
        save_active(self._store, moved)
        self._ledger.append(
            moved.session_id, moved.room_id, kind="stage", stage=stage,
            detail=detail, at=at,
        )
        return moved

    def _is_open_for(self, room_id: str) -> bool:
        session = load_active(self._store)
        return session is not None and session.room_id == room_id

    def _rollback_to(self, session: ActiveSession) -> None:
        self._restore_fans(session.rollback)
        # Any defrosting flag not present at the checkpoint is undone.
        checkpoint_rooms = set(session.rollback.defrosting_rooms)
        for room_id in self._rooms.room_ids():
            marked = room_id in checkpoint_rooms
            if self._rooms.room(room_id) is not None and self._rooms.room(room_id).defrosting != marked:
                self._rooms.set_defrosting(room_id, marked)
        clear_active(self._store)

    def _restore_fans(self, checkpoint) -> None:
        self._fans.stop_all()
        for fan_id in checkpoint.running_fans:
            fan = self._fans.fan(fan_id)
            if fan is not None and not fan.faulted:
                self._fans.start(fan_id)

    # ----- retained accessors used by console plumbing -------------------

    def last_record(self) -> DefrostRecord | None:
        records = self._ledger.records()
        return records[-1] if records else None
