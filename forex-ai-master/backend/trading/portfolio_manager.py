"""Portfolio and performance tracking: session / day / week P&L and the session target.

The session target (default 1 000 USD) is a *performance indicator*: when the
net **realised** profit of the session reaches it, new entries are suspended for
the rest of the session. It never influences position size or trade selection.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from backend.core.market_hours import trading_day_start, trading_session_bounds, week_start
from backend.core.types import AccountSnapshot, ClosedTrade, utcnow


@dataclass(slots=True)
class PeriodStats:
    start: datetime
    start_equity: float
    gross_pnl: float = 0.0
    commissions: float = 0.0
    swaps: float = 0.0
    slippage_cost: float = 0.0
    realized_profit: float = 0.0  # sum of winning net results
    realized_loss: float = 0.0  # sum of losing net results (negative)
    trades: int = 0
    wins: int = 0
    losses: int = 0

    @property
    def net_realized(self) -> float:
        return self.realized_profit + self.realized_loss

    def add(self, t: ClosedTrade) -> None:
        self.gross_pnl += t.gross_pnl
        self.commissions += t.commission
        self.swaps += t.swap
        self.slippage_cost += t.slippage_cost
        self.trades += 1
        if t.net_pnl > 0:
            self.realized_profit += t.net_pnl
            self.wins += 1
        else:
            self.realized_loss += t.net_pnl
            self.losses += 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["net_realized"] = self.net_realized
        return d


@dataclass(slots=True)
class SessionTarget:
    target: float = 1000.0
    enabled: bool = True
    reached: bool = False
    reached_at: datetime | None = None
    reached_net: float | None = None


@dataclass(slots=True)
class PortfolioManager:
    initial_balance: float
    session_start_utc: str = "07:00"
    session_end_utc: str = "21:00"
    target: SessionTarget = field(default_factory=SessionTarget)
    balance: float = 0.0
    equity: float = 0.0
    peak_equity: float = 0.0
    unrealized_profit: float = 0.0
    unrealized_loss: float = 0.0
    consecutive_losses: int = 0
    session: PeriodStats | None = None
    day: PeriodStats | None = None
    week: PeriodStats | None = None
    all_time: PeriodStats | None = None
    trade_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.balance = self.balance or self.initial_balance
        self.equity = self.equity or self.initial_balance
        self.peak_equity = max(self.peak_equity, self.equity)
        now = utcnow()
        self._roll(now)
        if self.all_time is None:
            self.all_time = PeriodStats(now, self.initial_balance)

    # ------------------------------------------------------------------ periods
    def _roll(self, now: datetime) -> list[str]:
        """Start new session/day/week periods when boundaries are crossed."""
        events = []
        s_start, _ = trading_session_bounds(now, self.session_start_utc, self.session_end_utc)
        if s_start > now:  # outside session: the "current" session is the one that just ended
            s_start -= timedelta(days=1)
        if self.session is None or self.session.start != s_start:
            self.session = PeriodStats(s_start, self.equity)
            self.target.reached, self.target.reached_at, self.target.reached_net = False, None, None
            events.append("new_session")
        d_start = trading_day_start(now)
        if self.day is None or self.day.start != d_start:
            self.day = PeriodStats(d_start, self.equity)
            events.append("new_day")
        w_start = week_start(now)
        if self.week is None or self.week.start != w_start:
            self.week = PeriodStats(w_start, self.equity)
            events.append("new_week")
        return events

    def update_account(self, account: AccountSnapshot, unrealized: list[float], now: datetime | None = None) -> list[str]:
        events = self._roll(now or utcnow())
        self.balance, self.equity = account.balance, account.equity
        self.peak_equity = max(self.peak_equity, self.equity)
        self.unrealized_profit = sum(u for u in unrealized if u > 0)
        self.unrealized_loss = sum(u for u in unrealized if u < 0)
        return events

    def record_trade(self, trade: ClosedTrade, now: datetime | None = None) -> bool:
        """Record a broker-confirmed closed trade. Returns True if the session target just got reached."""
        if trade.trade_id in self.trade_ids:
            return False
        self.trade_ids.add(trade.trade_id)
        self._roll(now or utcnow())
        for period in (self.session, self.day, self.week, self.all_time):
            assert period is not None
            period.add(trade)
        self.consecutive_losses = 0 if trade.net_pnl > 0 else self.consecutive_losses + 1
        return self.check_target(now)

    def check_target(self, now: datetime | None = None) -> bool:
        assert self.session is not None
        if not self.target.enabled or self.target.reached or self.target.target <= 0:
            return False
        if self.session.net_realized >= self.target.target:
            self.target.reached = True
            self.target.reached_at = now or utcnow()
            self.target.reached_net = self.session.net_realized
            return True
        return False

    # ------------------------------------------------------------------ views
    @property
    def drawdown(self) -> float:
        return max(0.0, self.peak_equity - self.equity)

    @property
    def drawdown_pct(self) -> float:
        return self.drawdown / self.peak_equity * 100 if self.peak_equity > 0 else 0.0

    def daily_pnl(self) -> float:
        """Net P&L since day start, including open positions (equity based)."""
        assert self.day is not None
        return self.equity - self.day.start_equity

    def weekly_pnl(self) -> float:
        assert self.week is not None
        return self.equity - self.week.start_equity

    def snapshot(self) -> dict[str, Any]:
        assert self.session and self.day and self.week and self.all_time
        return {
            "balance": self.balance, "equity": self.equity, "peak_equity": self.peak_equity,
            "drawdown": self.drawdown, "drawdown_pct": self.drawdown_pct,
            "unrealized_profit": self.unrealized_profit, "unrealized_loss": self.unrealized_loss,
            "unrealized_pnl": self.unrealized_profit + self.unrealized_loss,
            "consecutive_losses": self.consecutive_losses,
            "session": self.session.to_dict(), "day": self.day.to_dict(), "week": self.week.to_dict(),
            "all_time": self.all_time.to_dict(),
            "target": {"target": self.target.target, "enabled": self.target.enabled, "reached": self.target.reached,
                       "reached_at": self.target.reached_at.isoformat() if self.target.reached_at else None,
                       "progress": (self.session.net_realized / self.target.target) if self.target.target else 0.0},
        }

    def to_state(self) -> dict[str, Any]:
        return {"snapshot": self.snapshot(), "trade_ids": sorted(self.trade_ids)}

    def restore(self, state: dict[str, Any]) -> None:
        """Restore peak equity, consecutive losses and target status after a restart."""
        snap = state.get("snapshot", {})
        self.peak_equity = max(self.peak_equity, float(snap.get("peak_equity", 0.0)))
        self.consecutive_losses = int(snap.get("consecutive_losses", 0))
        self.trade_ids |= set(state.get("trade_ids", []))
        sess = snap.get("session") or {}
        tgt = snap.get("target") or {}
        if self.session is not None and sess.get("start") == self.session.start.isoformat():
            for k in ("gross_pnl", "commissions", "swaps", "slippage_cost", "realized_profit", "realized_loss",
                      "trades", "wins", "losses", "start_equity"):
                if k in sess:
                    setattr(self.session, k, type(getattr(self.session, k))(sess[k]))
            self.target.reached = bool(tgt.get("reached", False))
        day = snap.get("day") or {}
        if self.day is not None and day.get("start") == self.day.start.isoformat():
            self.day.start_equity = float(day.get("start_equity", self.day.start_equity))
        week = snap.get("week") or {}
        if self.week is not None and week.get("start") == self.week.start.isoformat():
            self.week.start_equity = float(week.get("start_equity", self.week.start_equity))
