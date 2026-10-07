"""Projections for the frost accumulation model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TriggerVerdict:
    """Whether a room has crossed the defrost trigger, and why."""

    room_id: str
    frost_load: float
    threshold: float
    demand: bool
    interval_seconds: int
    max_interval: int
    due_by_interval: bool
    trigger: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "room_id": self.room_id,
            "frost_load": self.frost_load,
            "threshold": self.threshold,
            "demand": self.demand,
            "interval_seconds": self.interval_seconds,
            "max_interval": self.max_interval,
            "due_by_interval": self.due_by_interval,
            "trigger": self.trigger,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FrostView:
    """Persisted frost state of one room."""

    room_id: str
    frost_load: float
    samples: int
    last_sample_at: int
    last_defrost_at: int
    tracked_since: int

    def as_dict(self) -> dict[str, object]:
        return {
            "room_id": self.room_id,
            "frost_load": self.frost_load,
            "samples": self.samples,
            "last_sample_at": self.last_sample_at,
            "last_defrost_at": self.last_defrost_at,
            "tracked_since": self.tracked_since,
        }


@dataclass(frozen=True)
class FrostState:
    """Every tracked room plus the active model parameters."""

    rooms: list[FrostView]
    baseline: float
    threshold: float
    max_interval: int

    def as_dict(self) -> dict[str, object]:
        return {
            "rooms": [room.as_dict() for room in self.rooms],
            "baseline": self.baseline,
            "threshold": self.threshold,
            "max_interval": self.max_interval,
        }
