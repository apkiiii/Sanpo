"""Common agent infrastructure.

Agents are *pure analysers*: they receive a :class:`MarketContext` and return an
:class:`AgentReport`. They have no access to the broker or to order placement.
Any unexpected exception is converted into an ``ERROR`` report so that a failing
agent can never be mistaken for a favourable one.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from backend.core.logging import get_logger
from backend.core.types import AgentReport, AgentStatus, Quote, Timeframe, utcnow
from backend.data.market_data import validate_ohlc

log = get_logger(__name__)


@dataclass(slots=True)
class MarketContext:
    """Snapshot of everything an agent may look at for one symbol."""

    symbol: str
    now: datetime
    candles: dict[Timeframe, pd.DataFrame]
    quote: Quote | None = None
    extras: dict[str, object] = field(default_factory=dict)

    def frame(self, tf: Timeframe) -> pd.DataFrame | None:
        return self.candles.get(tf)


@dataclass(slots=True)
class AgentHealth:
    last_run: datetime | None = None
    last_duration_ms: float = 0.0
    runs: int = 0
    errors: int = 0
    last_error: str = ""


class Agent(ABC):
    """Base class for all analysis agents."""

    name: str = "agent"
    min_bars: int = 210  # EMA200 + margin

    def __init__(self) -> None:
        self.health = AgentHealth()
        self.last_reports: dict[str, AgentReport] = {}

    def analyze(self, ctx: MarketContext) -> AgentReport:
        """Run the analysis safely and record health metrics."""
        start = time.perf_counter()
        try:
            report = self._analyze(ctx)
        except Exception as exc:  # noqa: BLE001 - convert to a safe ERROR report
            self.health.errors += 1
            self.health.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("agent failure", extra={"ctx": {"agent": self.name, "symbol": ctx.symbol}})
            report = AgentReport(self.name, ctx.symbol, AgentStatus.ERROR, summary=self.health.last_error)
        self.health.runs += 1
        self.health.last_run = utcnow()
        self.health.last_duration_ms = (time.perf_counter() - start) * 1000
        self.last_reports[ctx.symbol] = report
        return report

    @abstractmethod
    def _analyze(self, ctx: MarketContext) -> AgentReport:
        """Agent-specific logic."""

    def _insufficient(self, ctx: MarketContext, reason: str) -> AgentReport:
        return AgentReport(self.name, ctx.symbol, AgentStatus.INSUFFICIENT_DATA, summary=reason, reasons=(reason,))

    def _check_frame(self, ctx: MarketContext, tf: Timeframe, min_bars: int | None = None) -> str | None:
        """Return an error message if the frame for ``tf`` is unusable, else ``None``."""
        df = ctx.frame(tf)
        if df is None:
            return f"no {tf.value} data"
        q = validate_ohlc(df, min_bars=min_bars or self.min_bars)
        if not q.ok:
            return f"{tf.value}: " + "; ".join(q.issues)
        return None
