"""Frost accumulation estimator.

Decision: frost is judged by the integral of the room-to-coil temperature gap
while the cold fan is running, not by elapsed time alone. Evaporator frost
forms from moist air crossing the cold coil, so frost mass tracks fan runtime
weighted by that gap: a large gap with the fan running loads the coil quickly,
a stopped fan adds nothing, and a warm idle period must not count as frost.

Elapsed time since the last completed defrost is kept only as a safety net:
when no probe data ever arrives (or the probe reads a zero gap for long
enough), a maximum interval still forces a defrost. The verdict therefore
reports ``load`` (the integral trigger) and ``interval`` (the fallback
trigger) as distinct reasons.
"""

from __future__ import annotations

import time
from typing import Callable

from coldstore.room import RoomRegistry
from coldstore.store.epoch import load_float, save_float
from coldstore.store.store import Store

from .report import FrostState, FrostVerdict, validate_interval, validate_threshold

THRESHOLD_KEY = "frost:load-threshold"
MAX_INTERVAL_KEY = "frost:max-interval"
DEFAULT_THRESHOLD = 600.0
DEFAULT_MAX_INTERVAL = 21_600.0

LOAD_PREFIX = "frost:load:"
LAST_PREFIX = "frost:last:"
SEEN_PREFIX = "frost:seen:"


class FrostEstimator:
    """Per room integral accumulation, persisted to the key/value store."""

    def __init__(self, store: Store, clock: Callable[[], int] | None = None) -> None:
        self._store = store
        self._clock = clock

    def _now(self) -> int:
        if self._clock is not None:
            return int(self._clock())
        return int(time.time())

    def threshold(self) -> float:
        value, present = load_float(self._store, THRESHOLD_KEY)
        return value if present else DEFAULT_THRESHOLD

    def set_threshold(self, value: float) -> float:
        validate_threshold(value)
        save_float(self._store, THRESHOLD_KEY, value)
        return value

    def max_interval(self) -> float:
        value, present = load_float(self._store, MAX_INTERVAL_KEY)
        return value if present else DEFAULT_MAX_INTERVAL

    def set_max_interval(self, value: float) -> float:
        validate_interval(value)
        save_float(self._store, MAX_INTERVAL_KEY, value)
        return value

    def load(self, room_id: str) -> float:
        value, _ = load_float(self._store, f"{LOAD_PREFIX}{room_id}")
        return value

    def last_defrost_at(self, room_id: str) -> int:
        value, present = load_float(self._store, f"{LAST_PREFIX}{room_id}")
        return int(value) if present else 0

    def observed_at(self, room_id: str) -> int:
        value, present = load_float(self._store, f"{SEEN_PREFIX}{room_id}")
        return int(value) if present else 0

    def observe(self, room_id: str, room_temp: float, coil_temp: float,
                fan_running: bool, at: int | None = None) -> float:
        """Advance the integral for one observation interval.

        The increment is ``max(room_temp - coil_temp, 0) * elapsed`` and is
        only applied while the fan runs. Recording the observation also
        advances ``last-seen`` so callers can spot a dead probe.
        """

        now = int(at) if at is not None else self._now()
        _, seen_present = load_float(self._store, f"{SEEN_PREFIX}{room_id}")
        seen = self.observed_at(room_id) if seen_present else None
        elapsed = now - seen if seen is not None else 0
        if elapsed < 0:
            elapsed = 0
        increment = 0.0
        if fan_running and elapsed > 0:
            gap = room_temp - coil_temp
            increment = gap * elapsed if gap > 0 else 0.0
        current = self.load(room_id)
        next_load = current + increment
        save_float(self._store, f"{LOAD_PREFIX}{room_id}", next_load)
        save_float(self._store, f"{SEEN_PREFIX}{room_id}", float(now))
        return next_load

    def commit_defrost(self, room_id: str, at: int | None = None) -> float:
        """Zero the accumulated load once a defrost completes."""

        now = int(at) if at is not None else self._now()
        save_float(self._store, f"{LOAD_PREFIX}{room_id}", 0.0)
        save_float(self._store, f"{LAST_PREFIX}{room_id}", float(now))
        save_float(self._store, f"{SEEN_PREFIX}{room_id}", float(now))
        return 0.0

    def elapsed(self, room_id: str, at: int | None = None) -> int:
        """Seconds since the last completed defrost.

        A room that has never been defrosted is measured from its first probe
        observation, not from the unix epoch, so a brand new room is not
        instantly overdue.
        """

        now = int(at) if at is not None else self._now()
        last_value, last_present = load_float(self._store, f"{LAST_PREFIX}{room_id}")
        if last_present:
            baseline = int(last_value)
        else:
            seen_value, seen_present = load_float(self._store, f"{SEEN_PREFIX}{room_id}")
            baseline = int(seen_value) if seen_present else 0
        if not last_present and not seen_present:
            return 0
        return max(0, now - baseline)

    def verdict(self, room_id: str, at: int | None = None,
                force: bool = False) -> FrostVerdict:
        load = self.load(room_id)
        interval = float(self.elapsed(room_id, at))
        threshold = self.threshold()
        max_interval = self.max_interval()
        has_probe_history = self.observed_at(room_id) > 0 or self.last_defrost_at(room_id) > 0
        if force:
            due, reason = True, "manual"
        elif load >= threshold:
            due, reason = True, "load"
        elif has_probe_history and interval >= max_interval:
            due, reason = True, "interval"
        else:
            due, reason = False, "none"
        return FrostVerdict(
            room_id=room_id,
            load=load,
            threshold=threshold,
            elapsed=interval,
            max_interval=max_interval,
            due=due,
            reason=reason,
        )

    def state(self, rooms: RoomRegistry, at: int | None = None) -> FrostState:
        verdicts = [self.verdict(room.id, at) for room in rooms.rooms()]
        return FrostState(
            threshold=self.threshold(),
            max_interval=self.max_interval(),
            verdicts=verdicts,
            due=[v.room_id for v in verdicts if v.due],
        )

    def describe(self, rooms: RoomRegistry) -> str:
        state = self.state(rooms)
        return f"frost threshold={state.threshold:.1f} due={len(state.due)}"
