"""Performance metrics for backtests and live/demo trade history."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any, Protocol

import numpy as np
import pandas as pd


class TradeLike(Protocol):
    symbol: str
    strategy: str
    session: str
    net_pnl: float
    r_multiple: float
    commission: float
    swap: float
    slippage_cost: float
    entry_time: Any
    exit_time: Any


def max_drawdown(equity: pd.Series) -> tuple[float, float]:
    """Return (max drawdown in account currency, in percent of the running peak)."""
    if equity.empty:
        return 0.0, 0.0
    peak = equity.cummax()
    dd = peak - equity
    pct = (dd / peak).max() * 100
    return float(dd.max()), float(pct)


def _daily_returns(equity: pd.Series) -> pd.Series:
    daily = equity.resample("1D").last().dropna()
    return daily.pct_change().dropna()


def sharpe_ratio(equity: pd.Series, periods: int = 252) -> float:
    r = _daily_returns(equity)
    if len(r) < 2 or r.std(ddof=1) == 0:
        return 0.0
    return float(r.mean() / r.std(ddof=1) * math.sqrt(periods))


def sortino_ratio(equity: pd.Series, periods: int = 252) -> float:
    r = _daily_returns(equity)
    downside = r[r < 0]
    if len(r) < 2 or len(downside) < 2 or downside.std(ddof=1) == 0:
        return 0.0
    return float(r.mean() / downside.std(ddof=1) * math.sqrt(periods))


def trade_stats(trades: Sequence[TradeLike]) -> dict[str, float]:
    pnl = np.array([t.net_pnl for t in trades], dtype=float)
    if len(pnl) == 0:
        return {"trades": 0, "net_profit": 0.0, "gross_profit": 0.0, "gross_loss": 0.0, "profit_factor": 0.0,
                "win_rate": 0.0, "expectancy": 0.0, "expectancy_r": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "payoff_ratio": 0.0}
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gp, gl = float(wins.sum()), float(-losses.sum())
    return {
        "trades": int(len(pnl)),
        "net_profit": float(pnl.sum()),
        "gross_profit": gp,
        "gross_loss": gl,
        "profit_factor": gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0),
        "win_rate": float(len(wins) / len(pnl)),
        "expectancy": float(pnl.mean()),
        "expectancy_r": float(np.mean([t.r_multiple for t in trades])),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "payoff_ratio": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.mean() < 0 else 0.0,
    }


def _group(trades: Iterable[TradeLike], key: str) -> dict[str, dict[str, float]]:
    groups: dict[str, list[TradeLike]] = defaultdict(list)
    for t in trades:
        groups[str(getattr(t, key))].append(t)
    return {k: trade_stats(v) for k, v in sorted(groups.items())}


def compute_metrics(trades: Sequence[TradeLike], equity: pd.Series, initial_balance: float) -> dict[str, Any]:
    """Full metric set used in reports and by the Strategy Optimizer."""
    stats = trade_stats(trades)
    dd_abs, dd_pct = max_drawdown(equity)
    durations = [(t.exit_time - t.entry_time).total_seconds() / 3600 for t in trades if t.exit_time]
    final = float(equity.iloc[-1]) if len(equity) else initial_balance
    return {
        **stats,
        "initial_balance": initial_balance,
        "final_equity": final,
        "return_pct": (final - initial_balance) / initial_balance * 100,
        "max_drawdown": dd_abs,
        "max_drawdown_pct": dd_pct,
        "sharpe": sharpe_ratio(equity),
        "sortino": sortino_ratio(equity),
        "avg_duration_hours": float(np.mean(durations)) if durations else 0.0,
        "total_commission": float(sum(t.commission for t in trades)),
        "total_swap": float(sum(t.swap for t in trades)),
        "total_slippage_cost": float(sum(t.slippage_cost for t in trades)),
        "by_symbol": _group(trades, "symbol"),
        "by_strategy": _group(trades, "strategy"),
        "by_session": _group(trades, "session"),
    }
