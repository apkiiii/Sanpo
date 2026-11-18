"""Tamper-evident audit log.

Each entry stores the SHA-256 hash of the previous entry, so any modification or
deletion of a past entry breaks :meth:`AuditLog.verify`. Entries are redacted
before being stored.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.core.logging import redact
from backend.core.types import utcnow

GENESIS = "0" * 64


@dataclass(frozen=True, slots=True)
class AuditEntry:
    seq: int
    time: datetime
    actor: str
    action: str
    details: dict[str, Any]
    prev_hash: str
    hash: str

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "time": self.time.isoformat(), "actor": self.actor, "action": self.action,
                "details": self.details, "prev_hash": self.prev_hash, "hash": self.hash}


def _digest(seq: int, time: datetime, actor: str, action: str, details: dict[str, Any], prev: str) -> str:
    payload = json.dumps([seq, time.isoformat(), actor, action, details, prev], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


class AuditLog:
    def __init__(self, sink: Callable[[AuditEntry], None] | None = None) -> None:
        self.entries: list[AuditEntry] = []
        self._sink = sink

    @property
    def last_hash(self) -> str:
        return self.entries[-1].hash if self.entries else GENESIS

    def load(self, entries: list[AuditEntry]) -> None:
        self.entries = list(entries)

    def record(self, actor: str, action: str, details: dict[str, Any] | None = None) -> AuditEntry:
        clean = json.loads(redact(json.dumps(details or {}, default=str)))
        seq = len(self.entries) + 1
        t = utcnow()
        prev = self.last_hash
        entry = AuditEntry(seq, t, actor, action, clean, prev, _digest(seq, t, actor, action, clean, prev))
        self.entries.append(entry)
        if self._sink is not None:
            self._sink(entry)
        return entry

    def verify(self) -> tuple[bool, int | None]:
        """Return (valid, first_invalid_seq)."""
        prev = GENESIS
        for e in self.entries:
            if e.prev_hash != prev or _digest(e.seq, e.time, e.actor, e.action, e.details, e.prev_hash) != e.hash:
                return False, e.seq
            prev = e.hash
        return True, None

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.entries[-limit:]][::-1]
