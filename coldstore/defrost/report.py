"""Defrost projections."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DefrostState:
    """Active session, pending queue and ledger summary for the console."""

    active: dict[str, object] | None
    defrosting: list[str]
    queue: list[str]
    records: int
    sealed: int
    chain_ok: bool
    frost: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "active": self.active,
            "defrosting": self.defrosting,
            "queue": self.queue,
            "records": self.records,
            "sealed": self.sealed,
            "chain_ok": self.chain_ok,
            "frost": self.frost,
        }
