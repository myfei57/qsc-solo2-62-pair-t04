"""Defrost lifecycle stages and rollback checkpoints.

A defrost moves forward through a single straight line of stages:

    IDLE -> FAN_STOPPING -> FAN_STOPPED -> HEATING -> COMPLETED

Only IDLE and COMPLETED are fully stable. FAN_STOPPING and FAN_STOPPED are the
hard interlock that keeps heating off until every cold fan has actually been
observed stopped. An abnormal exit leaves the session ABORTED and the physical
plant is rolled back to the most recent checkpoint instead of being abandoned
half way.

The chosen policy is one continuous defrost to completion (no segmented
reheating): segments would multiply the fan stop/start cycles and leave more
half-applied state to reconcile, while accurate triggering already prevents
the long heat-ups that segmentation is meant to avoid.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from coldstore.store.store import Store

IDLE = "idle"
FAN_STOPPING = "fan_stopping"
FAN_STOPPED = "fan_stopped"
HEATING = "heating"
COMPLETED = "completed"
ABORTED = "aborted"

# Stages from which the plant is in a settled, consistent state.
STABLE_STAGES = (IDLE, COMPLETED, ABORTED)
ACTIVE_KEY = "defrost:active"

# Legal forward moves of the lifecycle.
NEXT_STAGE = {
    IDLE: FAN_STOPPING,
    FAN_STOPPING: FAN_STOPPED,
    FAN_STOPPED: HEATING,
    HEATING: COMPLETED,
}


@dataclass(frozen=True)
class Checkpoint:
    """A settled state the plant can be returned to after an abort."""

    stage: str
    running_fans: tuple[str, ...]
    primary_fan: str
    defrosting_rooms: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "running_fans": list(self.running_fans),
            "primary_fan": self.primary_fan,
            "defrosting_rooms": list(self.defrosting_rooms),
        }


@dataclass(frozen=True)
class ActiveSession:
    """The in-flight defrost, persisted so a restart can resume or roll back."""

    session_id: str
    room_id: str
    stage: str
    trigger: str
    started_at: int
    updated_at: int
    rollback: Checkpoint

    def as_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "room_id": self.room_id,
            "stage": self.stage,
            "trigger": self.trigger,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "rollback": self.rollback.as_dict(),
        }


def new_session_id() -> str:
    return uuid.uuid4().hex


def capture_checkpoint(stage: str, fans, rooms) -> Checkpoint:
    """Snapshot the plant state that defines one rollback point."""

    return Checkpoint(
        stage=stage,
        running_fans=tuple(fans.running_ids()),
        primary_fan=fans.primary(),
        defrosting_rooms=tuple(rooms.defrosting_ids()),
    )


def save_active(store: Store, session: ActiveSession) -> None:
    store.put(ACTIVE_KEY, json.dumps(session.as_dict(), ensure_ascii=False))


def clear_active(store: Store) -> None:
    store.delete(ACTIVE_KEY)


def load_active(store: Store) -> ActiveSession | None:
    raw, present = store.get(ACTIVE_KEY)
    if not present:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    rollback_raw = payload.get("rollback", {})
    if not isinstance(rollback_raw, dict):
        rollback_raw = {}
    return ActiveSession(
        session_id=str(payload.get("session_id", "")),
        room_id=str(payload.get("room_id", "")),
        stage=str(payload.get("stage", IDLE)),
        trigger=str(payload.get("trigger", "")),
        started_at=int(payload.get("started_at", 0)),
        updated_at=int(payload.get("updated_at", 0)),
        rollback=Checkpoint(
            stage=str(rollback_raw.get("stage", IDLE)),
            running_fans=tuple(str(item) for item in rollback_raw.get("running_fans", [])),
            primary_fan=str(rollback_raw.get("primary_fan", "")),
            defrosting_rooms=tuple(
                str(item) for item in rollback_raw.get("defrosting_rooms", [])
            ),
        ),
    )
