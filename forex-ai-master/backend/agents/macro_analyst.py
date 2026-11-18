"""BOT 4 — News & Macro Analyst.

Uses only a verified calendar source (see :mod:`backend.data.economic_calendar`).
Blocks new entries around high-impact events for either currency of the pair,
and when the calendar is unavailable or stale (configurable). Medium-impact
events reduce confidence but do not block.
"""

from __future__ import annotations

from datetime import timedelta

from backend.agents.base import Agent, MarketContext
from backend.core.instruments import get_instrument
from backend.core.types import AgentReport, AgentStatus, Direction
from backend.data.economic_calendar import CalendarProvider, NullCalendarProvider

# Keywords of events that are always treated as high impact, whatever the source says.
CRITICAL_KEYWORDS = ("interest rate", "rate decision", "non-farm", "nonfarm", "cpi", "fomc", "ecb press",
                     "monetary policy", "gdp", "employment change", "unemployment rate")


class MacroAnalyst(Agent):
    name = "macro_analyst"

    def __init__(self, calendar: CalendarProvider | None = None, minutes_before: int = 30, minutes_after: int = 30,
                 block_when_unavailable: bool = True, lookahead_hours: int = 24) -> None:
        super().__init__()
        self.calendar = calendar or NullCalendarProvider()
        self.before = timedelta(minutes=minutes_before)
        self.after = timedelta(minutes=minutes_after)
        self.block_when_unavailable = block_when_unavailable
        self.lookahead = timedelta(hours=lookahead_hours)
        self.manual_risk_off: str | None = None  # set by an operator for geopolitical shocks

    def set_manual_risk_off(self, reason: str | None) -> None:
        """Operator switch for events a calendar cannot capture (geopolitics). ``None`` clears it."""
        self.manual_risk_off = reason

    def _analyze(self, ctx: MarketContext) -> AgentReport:
        if self.manual_risk_off:
            return AgentReport(self.name, ctx.symbol, AgentStatus.BLOCK, summary=f"manual risk-off: {self.manual_risk_off}",
                               reasons=(self.manual_risk_off,), timestamp=ctx.now)
        snap = self.calendar.snapshot(ctx.now)
        if not snap.available:
            status = AgentStatus.BLOCK if self.block_when_unavailable else AgentStatus.DATA_UNAVAILABLE
            return AgentReport(self.name, ctx.symbol, status, summary=f"calendar unavailable: {snap.reason}",
                               details={"source": snap.source}, reasons=(snap.reason,), timestamp=ctx.now)
        spec = get_instrument(ctx.symbol)
        ccys = {spec.base, spec.quote}
        relevant = [e for e in snap.events if e.currency in ccys and
                    ctx.now - self.after <= e.time <= ctx.now + self.lookahead]
        def is_high(e: object) -> bool:
            return e.impact == "high" or any(k in e.title.lower() for k in CRITICAL_KEYWORDS)  # type: ignore[attr-defined]
        blocking = [e for e in relevant if is_high(e) and e.time - self.before <= ctx.now <= e.time + self.after]
        upcoming = [{"time": e.time.isoformat(), "currency": e.currency, "impact": e.impact, "title": e.title}
                    for e in sorted(relevant, key=lambda x: x.time)[:10]]
        details = {"source": snap.source, "fetched_at": snap.fetched_at.isoformat() if snap.fetched_at else None,
                   "upcoming": upcoming}
        if blocking:
            names = ", ".join(f"{e.currency} {e.title} @ {e.time:%H:%M}" for e in blocking)
            return AgentReport(self.name, ctx.symbol, AgentStatus.BLOCK, summary=f"news blackout: {names}",
                               details=details, reasons=(f"high-impact event window: {names}",), timestamp=ctx.now)
        medium_soon = [e for e in relevant if e.impact == "medium" and abs(e.time - ctx.now) <= self.before]
        # Neutral signal: the macro agent filters risk, it does not pick a direction.
        confidence = 0.5 if medium_soon else 1.0
        return AgentReport(self.name, ctx.symbol, AgentStatus.OK, Direction.NEUTRAL, confidence,
                           "no blocking event" + (" (medium-impact event nearby)" if medium_soon else ""),
                           details, ("calendar checked",), ctx.now)
