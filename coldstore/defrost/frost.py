"""Frost accumulation model.

The amount of frost on an evaporator is driven by how much moisture-laden warm
air it has to cool, not by the clock alone. The model therefore integrates the
temperature difference between room air and the evaporator baseline over the
time the cold fan is actually running::

    frost += (room_temp - frost_baseline) * elapsed_seconds

A plain time estimate cannot tell a door-open humid room from a sealed cold one
after the same number of hours; the difference integral can. Time still plays a
role as a safety net: when no defrost has run for ``max_interval`` seconds the
room is due regardless of the integrated value, so a stuck sensor or a flat
integral can never suppress defrost forever.

State is persisted per room inside the key/value document; accumulating more
samples only rewrites that room's own key.
"""

from __future__ import annotations

import json

from coldstore.fan import FanSet
from coldstore.room import RoomRegistry
from coldstore.store.epoch import load_float, save_float
from coldstore.store.store import Store

from .frost_report import FrostState, FrostView, TriggerVerdict
from .schedule import now_unix

BASELINE_KEY = "defrost:frost-baseline"
THRESHOLD_KEY = "defrost:frost-threshold"
INTERVAL_KEY = "defrost:max-interval"
ROOM_KEY_PREFIX = "defrost:frost:"

DEFAULT_BASELINE = -25.0
DEFAULT_THRESHOLD = 300.0
DEFAULT_MAX_INTERVAL = 6 * 60 * 60
MIN_BASELINE = -60.0
MAX_BASELINE = 10.0


class FrostModel:
    """Integrates frost load per room and judges the defrost trigger."""

    def __init__(self, store: Store, rooms: RoomRegistry, fans: FanSet) -> None:
        self._store = store
        self._rooms = rooms
        self._fans = fans

    def baseline(self) -> float:
        value, present = load_float(self._store, BASELINE_KEY)
        return value if present else DEFAULT_BASELINE

    def set_baseline(self, value: float) -> float:
        if value < MIN_BASELINE or value > MAX_BASELINE:
            raise ValueError("frost baseline out of supported range")
        save_float(self._store, BASELINE_KEY, value)
        return value

    def threshold(self) -> float:
        value, present = load_float(self._store, THRESHOLD_KEY)
        return value if present else DEFAULT_THRESHOLD

    def set_threshold(self, value: float) -> float:
        if value <= 0:
            raise ValueError("frost threshold must be positive")
        save_float(self._store, THRESHOLD_KEY, value)
        return value

    def max_interval(self) -> int:
        raw, present = self._store.get(INTERVAL_KEY)
        if not present:
            return DEFAULT_MAX_INTERVAL
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_MAX_INTERVAL
        return value if value > 0 else DEFAULT_MAX_INTERVAL

    def set_max_interval(self, seconds: int) -> int:
        if seconds <= 0:
            raise ValueError("max interval must be positive")
        self._store.put(INTERVAL_KEY, str(int(seconds)))
        return int(seconds)

    def sample(self, room_id: str, at: int | None = None) -> FrostView:
        """Accumulate one frost sample for a room while its fan is running."""

        room = self._rooms.room(room_id)
        if room is None:
            raise ValueError(f"cold room {room_id} not found")
        sampled_at = int(at) if at is not None else now_unix()
        view = self._load(room_id)
        # Integrate from the previous reading only; the first reading anchors
        # the clock without any elapsed time.
        elapsed = sampled_at - view.last_sample_at if view.samples > 0 else 0
        running = bool(self._fans.running_ids())
        increment = 0.0
        if running and elapsed > 0:
            increment = max(0.0, (room.temp - self.baseline()) * elapsed)
        # The interval safety clock starts when tracking first begins.
        tracked_since = view.tracked_since or sampled_at
        view = FrostView(
            room_id=room_id,
            frost_load=view.frost_load + increment,
            samples=view.samples + 1,
            last_sample_at=sampled_at,
            last_defrost_at=view.last_defrost_at,
            tracked_since=tracked_since,
        )
        self._save(view)
        return view

    def reset(self, room_id: str, at: int | None = None) -> None:
        """Zero the integral after a completed defrost."""

        reset_at = int(at) if at is not None else now_unix()
        # One anchor sample at the reset instant: elapsed time afterwards is
        # measured from here, but the fan-off defrost gap is not integrated.
        self._save(
            FrostView(
                room_id=room_id,
                frost_load=0.0,
                samples=1,
                last_sample_at=reset_at,
                last_defrost_at=reset_at,
                tracked_since=reset_at,
            )
        )

    def frost_load(self, room_id: str) -> float:
        return self._load(room_id).frost_load

    def verdict(self, room_id: str, at: int | None = None) -> TriggerVerdict:
        """Judge the trigger: integrated demand first, interval as backstop."""

        view = self._load(room_id)
        sampled_at = int(at) if at is not None else now_unix()
        threshold = self.threshold()
        max_interval = self.max_interval()
        demand = view.frost_load >= threshold
        # The interval clock only exists once tracking has started.
        interval = sampled_at - view.tracked_since if view.tracked_since else 0
        due_by_interval = view.tracked_since > 0 and interval >= max_interval
        if demand:
            trigger, reason = True, "frost_demand"
        elif due_by_interval:
            trigger, reason = True, "max_interval"
        else:
            trigger, reason = False, "below_threshold"
        return TriggerVerdict(
            room_id=room_id,
            frost_load=view.frost_load,
            threshold=threshold,
            demand=demand,
            interval_seconds=interval,
            max_interval=max_interval,
            due_by_interval=due_by_interval,
            trigger=trigger,
            reason=reason,
        )

    def due_rooms(self, at: int | None = None) -> list[str]:
        """Every room past its trigger, highest frost load first."""

        verdicts = [self.verdict(room.id, at) for room in self._rooms.rooms()]
        verdicts = [verdict for verdict in verdicts if verdict.trigger]
        verdicts.sort(key=lambda verdict: (-verdict.frost_load, verdict.room_id))
        return [verdict.room_id for verdict in verdicts]

    def views(self) -> list[FrostView]:
        return [self._load(room.id) for room in self._rooms.rooms()]

    def state(self) -> FrostState:
        return FrostState(
            rooms=self.views(),
            baseline=self.baseline(),
            threshold=self.threshold(),
            max_interval=self.max_interval(),
        )

    def describe(self) -> str:
        return (
            f"frost baseline={self.baseline():.4f} threshold={self.threshold():.4f} "
            f"rooms={len(self.views())}"
        )

    def _key(self, room_id: str) -> str:
        return f"{ROOM_KEY_PREFIX}{room_id}"

    def _load(self, room_id: str) -> FrostView:
        raw, present = self._store.get(self._key(room_id))
        if not present:
            return FrostView(room_id=room_id, frost_load=0.0, samples=0,
                             last_sample_at=0, last_defrost_at=0, tracked_since=0)
        try:
            payload = json.loads(raw)
        except ValueError:
            return FrostView(room_id=room_id, frost_load=0.0, samples=0,
                             last_sample_at=0, last_defrost_at=0, tracked_since=0)
        if not isinstance(payload, dict):
            return FrostView(room_id=room_id, frost_load=0.0, samples=0,
                             last_sample_at=0, last_defrost_at=0, tracked_since=0)
        return FrostView(
            room_id=room_id,
            frost_load=float(payload.get("frost_load", 0.0)),
            samples=int(payload.get("samples", 0)),
            last_sample_at=int(payload.get("last_sample_at", 0)),
            last_defrost_at=int(payload.get("last_defrost_at", 0)),
            tracked_since=int(payload.get("tracked_since", 0)),
        )

    def _save(self, view: FrostView) -> None:
        self._store.put(self._key(view.room_id), json.dumps(view.as_dict(), ensure_ascii=False))
