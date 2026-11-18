"""Economic calendar access.

No economic data is ever invented. Providers return a :class:`CalendarSnapshot`
whose ``available`` flag is False when the data is missing, unreadable or stale;
the Macro agent then reports ``DATA_UNAVAILABLE`` and, by default, entries are
blocked.

Supported source: a verified export file (CSV or JSON) produced from a
calendar provider of your choice, with fields
``time (ISO-8601 UTC), currency, impact (low|medium|high), title`` and
optionally ``actual, forecast, previous``. JSON may be a list or
``{"generated_at": "...", "events": [...]}``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import pandas as pd

from backend.core.logging import get_logger

log = get_logger(__name__)

IMPACT_LEVELS = ("low", "medium", "high")


@dataclass(frozen=True, slots=True)
class EconomicEvent:
    time: datetime
    currency: str
    impact: str
    title: str
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None


@dataclass(slots=True)
class CalendarSnapshot:
    available: bool
    source: str
    fetched_at: datetime | None
    events: list[EconomicEvent] = field(default_factory=list)
    reason: str = ""

    def covers(self, start: datetime, end: datetime) -> bool:
        return self.available


class CalendarProvider(Protocol):
    def snapshot(self, now: datetime) -> CalendarSnapshot: ...


class NullCalendarProvider:
    """Used when no calendar source is configured: always unavailable."""

    def snapshot(self, now: datetime) -> CalendarSnapshot:
        return CalendarSnapshot(False, "none", None, [], "no economic calendar source configured")


class StaticCalendarProvider:
    """In-memory provider (tests, or events pushed by another verified component)."""

    def __init__(self, events: list[EconomicEvent], fetched_at: datetime, source: str = "static") -> None:
        self.events = events
        self.fetched_at = fetched_at
        self.source = source

    def snapshot(self, now: datetime) -> CalendarSnapshot:
        return CalendarSnapshot(True, self.source, self.fetched_at, list(self.events))


class FileCalendarProvider:
    """Load a verified calendar export. Stale files (older than ``max_age``) are rejected."""

    def __init__(self, path: str | Path, max_age: timedelta = timedelta(days=7)) -> None:
        self.path = Path(path)
        self.max_age = max_age

    def snapshot(self, now: datetime) -> CalendarSnapshot:
        if not self.path.exists():
            return CalendarSnapshot(False, str(self.path), None, [], "calendar file not found")
        try:
            events, generated = self._load()
        except Exception as exc:  # noqa: BLE001 - any parsing error means "unavailable"
            log.warning("calendar file unreadable", extra={"ctx": {"error": str(exc)}})
            return CalendarSnapshot(False, str(self.path), None, [], f"unreadable calendar: {exc}")
        fetched = generated or datetime.fromtimestamp(self.path.stat().st_mtime, UTC)
        if now - fetched > self.max_age:
            return CalendarSnapshot(False, str(self.path), fetched, events, "calendar data is stale")
        return CalendarSnapshot(True, str(self.path), fetched, events)

    def _load(self) -> tuple[list[EconomicEvent], datetime | None]:
        generated: datetime | None = None
        if self.path.suffix.lower() == ".json":
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                if payload.get("generated_at"):
                    generated = _to_utc(payload["generated_at"])
                rows = payload.get("events", [])
            else:
                rows = payload
        else:
            rows = pd.read_csv(self.path).fillna("").to_dict("records")
        events = []
        for r in rows:
            impact = str(r["impact"]).strip().lower()
            if impact not in IMPACT_LEVELS:
                raise ValueError(f"invalid impact '{impact}'")
            events.append(EconomicEvent(_to_utc(r["time"]), str(r["currency"]).upper(), impact, str(r["title"]),
                                        str(r.get("actual") or "") or None, str(r.get("forecast") or "") or None,
                                        str(r.get("previous") or "") or None))
        return events, generated


def _to_utc(value: object) -> datetime:
    ts = pd.Timestamp(value)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.to_pydatetime()
