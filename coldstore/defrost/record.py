"""Persistence of the append only defrost ledger."""

from __future__ import annotations

import json

from coldstore.store.commands import append_command, load_commands
from coldstore.store.store import Store

from .ledger import Event, Ledger, event_from_dict

LEDGER_KEY = "defrost:ledger"


def load_ledger(store: Store) -> Ledger:
    """Fold every well formed ledger entry into a read model."""

    events: list[Event] = []
    for index, item in enumerate(load_commands(store, LEDGER_KEY), start=1):
        try:
            payload = json.loads(item)
        except ValueError:
            continue
        event = event_from_dict(payload, index)
        if event is not None:
            events.append(event)
    return Ledger(events)


def append_event(store: Store, event: Event) -> None:
    """Append one event. Callers validate the transition beforehand."""

    append_command(store, LEDGER_KEY, json.dumps(event.as_dict(), ensure_ascii=False))
