"""Frost accumulation projections and validation rules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FrostVerdict:
    """Whether one room has crossed a defrost trigger and why."""

    room_id: str
    load: float
    threshold: float
    elapsed: float
    max_interval: float
    due: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "room_id": self.room_id,
            "load": self.load,
            "threshold": self.threshold,
            "elapsed": self.elapsed,
            "max_interval": self.max_interval,
            "due": self.due,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FrostState:
    """Configured limits and one verdict per tracked room."""

    threshold: float
    max_interval: float
    verdicts: list[FrostVerdict]
    due: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "threshold": self.threshold,
            "max_interval": self.max_interval,
            "due": self.due,
            "verdicts": [verdict.as_dict() for verdict in self.verdicts],
        }


def validate_threshold(value: float) -> None:
    """Frost load thresholds are positive degree seconds."""

    if value <= 0:
        raise ValueError("frost threshold must be positive")
    if value > 10_000_000:
        raise ValueError("frost threshold exceeds the supported range")


def validate_interval(value: float) -> None:
    """The time based fallback interval is a positive number of seconds."""

    if value <= 0:
        raise ValueError("frost interval must be positive")
    if value > 10_000_000:
        raise ValueError("frost interval exceeds the supported range")
