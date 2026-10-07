"""Defrost projections and validation rules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DefrostState:
    """Queue, active sessions and the counters operators watch."""

    commands: list[str]
    defrosting: list[str]
    active: list[dict[str, object]]
    completed: int
    aborted: int
    sessions: int
    warmups: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "commands": self.commands,
            "defrosting": self.defrosting,
            "active": self.active,
            "completed": self.completed,
            "aborted": self.aborted,
            "sessions": self.sessions,
            "warmups": self.warmups,
        }


def validate_end_temp(value: float) -> None:
    """Defrost completion temperature must stay inside the probe range."""

    if value < -50:
        raise ValueError("defrost end temperature is below the supported range")
    if value > 50:
        raise ValueError("defrost end temperature exceeds the supported range")


def validate_positive_seconds(value: float, limit: float = 1_000_000) -> None:
    if value <= 0:
        raise ValueError("duration must be positive")
    if value > limit:
        raise ValueError("duration exceeds the supported range")
