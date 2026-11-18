"""Event-driven bar backtester with realistic costs.

Modelled:
* bid/ask spread (fixed per symbol or from a ``spread`` column in pips);
* commissions per lot and per side;
* random adverse slippage on market fills and on stop exits, plus latency slippage;
* swaps at the 22:00 UTC rollover (triple on Wednesday);
* gaps: a bar opening beyond the stop is exited at the open (worse than the stop);
* conservative intrabar ordering: when stop and target are both touched in the
  same bar, the stop is assumed first;
* partial fills above a liquidity threshold (remaining size filled at a worse price);
* random order rejections;
* market hours (no entries when closed or just before the weekly close);
* the same risk rules as live trading: fixed fractional sizing, daily loss halt,
  max drawdown halt.

Signals are produced on bar ``t`` close and executed at bar ``t+1`` open.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any

import numpy as np
import pandas as pd

from backend.analysis.indicators import compute_all
from backend.core.instruments import get_instrument, price_move_value
from backend.core.market_hours import is_market_open, market_session_name, trading_day_start
from backend.core.types import Direction
from backend.risk.position_sizing import size_position
from backend.strategies.base import Strategy

DEFAULT_SPREADS = {"EURUSD": 0.8, "GBPUSD": 1.2, "USDJPY": 1.0, "USDCHF": 1.5, "AUDUSD": 1.0,
                   "USDCAD": 1.5, "NZDUSD": 1.6}


@dataclass(slots=True)
class BacktestConfig:
    initial_balance: float = 100_000.0
    account_ccy: str = "USD"
    risk_pct: float = 0.25
    spread_pips: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SPREADS))
    commission_per_lot_side: float = 3.5  # USD per 100k units per side
    slippage_pips: float = 0.2  # mean |N| adverse slippage on market fills
    stop_slippage_pips: float = 0.5  # extra slippage when a stop is hit
    latency_ms: float = 150.0
    latency_slippage_pips_per_100ms: float = 0.05
    swap_long_pips: float = -0.6  # per night, negative = cost (conservative default)
    swap_short_pips: float = -0.6
    partial_fill_threshold_units: float = 5_000_000.0
    partial_fill_extra_slippage_pips: float = 0.5
    reject_probability: float = 0.0
    daily_loss_limit_pct: float = 1.0
    max_drawdown_pct: float = 5.0
    max_bars_in_trade: int | None = None
    warmup_bars: int = 210
    seed: int = 7


@dataclass(slots=True)
class BacktestTrade:
    trade_id: int
    symbol: str
    strategy: str
    direction: Direction
    units: float
    entry_time: datetime
    entry_price: float
    stop_loss: float
    take_profit: float
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    gross_pnl: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    slippage_cost: float = 0.0
    initial_risk: float = 0.0
    bars_held: int = 0
    session: str = ""
    partial_fill: bool = False

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl + self.commission + self.swap

    @property
    def r_multiple(self) -> float:
        return self.net_pnl / self.initial_risk if self.initial_risk > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.trade_id, "symbol": self.symbol, "strategy": self.strategy,
                "direction": self.direction.value, "units": self.units, "entry_time": self.entry_time.isoformat(),
                "entry_price": self.entry_price, "stop_loss": self.stop_loss, "take_profit": self.take_profit,
                "exit_time": self.exit_time.isoformat() if self.exit_time else None, "exit_price": self.exit_price,
                "exit_reason": self.exit_reason, "gross_pnl": round(self.gross_pnl, 2),
                "commission": round(self.commission, 2), "swap": round(self.swap, 2),
                "slippage_cost": round(self.slippage_cost, 2), "net_pnl": round(self.net_pnl, 2),
                "r_multiple": round(self.r_multiple, 3), "bars_held": self.bars_held, "session": self.session}


@dataclass(slots=True)
class BacktestResult:
    symbol: str
    strategy: str
    params: dict[str, Any]
    config: BacktestConfig
    trades: list[BacktestTrade]
    equity_curve: pd.Series
    rejected_orders: int = 0
    halts: list[str] = field(default_factory=list)
    data_info: dict[str, Any] = field(default_factory=dict)

    @property
    def synthetic(self) -> bool:
        return bool(self.data_info.get("synthetic"))


def _crossed_rollover(prev: datetime, cur: datetime) -> int:
    """Number of swap nights charged between two bar times (Wednesday counts triple)."""
    nights = 0
    t = datetime.combine(prev.date(), time(22, 0), UTC)
    if t <= prev:
        t += timedelta(days=1)
    while t <= cur:
        if t.weekday() <= 4:  # Mon-Fri rollovers; Wednesday is tripled to cover the weekend
            nights += 3 if t.weekday() == 2 else 1
        t += timedelta(days=1)
    return nights


class BacktestEngine:
    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()

    def run(self, symbol: str, df: pd.DataFrame, strategy: Strategy,
            indicators: pd.DataFrame | None = None) -> BacktestResult:
        cfg = self.config
        rng = np.random.default_rng(cfg.seed)
        spec = get_instrument(symbol)
        ind = indicators if indicators is not None else compute_all(df)
        signals = strategy.generate(df, ind)
        n = len(df)
        times = [t.to_pydatetime() for t in df.index]
        o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
        spread_col = df["spread"].to_numpy(dtype=float) if "spread" in df.columns else None
        sig = signals["signal"].to_numpy()
        sdist = signals["stop_dist"].to_numpy(dtype=float)
        tdist = signals["target_dist"].to_numpy(dtype=float)

        balance = cfg.initial_balance
        peak = balance
        equity = np.full(n, np.nan)
        trades: list[BacktestTrade] = []
        pos: BacktestTrade | None = None
        rejected = 0
        halts: list[str] = []
        halted = False
        day_key: datetime | None = None
        day_start_balance = balance

        def half_spread(i: int) -> float:
            pips = spread_col[i] if spread_col is not None else cfg.spread_pips.get(symbol, 1.5)
            return pips * spec.pip_size / 2

        def slip(extra_pips: float = 0.0) -> float:
            base = abs(rng.normal(0.0, cfg.slippage_pips)) + cfg.latency_ms / 100 * cfg.latency_slippage_pips_per_100ms
            return (base + extra_pips) * spec.pip_size

        def money(distance: float, units: float, price: float) -> float:
            return price_move_value(symbol, distance, units, cfg.account_ccy, {symbol: price})

        def close_position(i: int, raw_exit: float, reason: str, slip_price: float) -> None:
            nonlocal balance, pos
            assert pos is not None
            d = pos.direction.sign
            exit_price = raw_exit - d * slip_price
            pos.exit_time, pos.exit_price, pos.exit_reason = times[i], exit_price, reason
            pos.gross_pnl = money((exit_price - pos.entry_price) * d, pos.units, exit_price)
            pos.commission -= pos.units / 100_000 * cfg.commission_per_lot_side
            pos.slippage_cost += money(slip_price, pos.units, exit_price)
            balance += pos.net_pnl
            trades.append(pos)
            pos = None

        for i in range(1, n):
            # --- new trading day bookkeeping
            dk = trading_day_start(times[i])
            if dk != day_key:
                day_key, day_start_balance = dk, balance

            # --- 1. execute pending entry from previous bar's signal at this bar's open
            if pos is None and not halted and i - 1 >= cfg.warmup_bars and sig[i - 1] != 0:
                allowed = is_market_open(times[i]) and not (times[i].weekday() == 4 and times[i].hour >= 20) \
                    and not (times[i].weekday() == 6 and times[i].hour == 22)
                day_loss_pct = (day_start_balance - balance) / day_start_balance * 100
                # same rule as the live Risk Manager: current loss + this trade's risk must fit in the limit
                if day_loss_pct + cfg.risk_pct > cfg.daily_loss_limit_pct + 1e-9:
                    allowed = False
                if (peak - balance) / peak * 100 + cfg.risk_pct > cfg.max_drawdown_pct + 1e-9:
                    allowed = False
                if allowed and rng.random() < cfg.reject_probability:
                    rejected += 1
                    allowed = False
                if allowed:
                    d = Direction.LONG if sig[i - 1] > 0 else Direction.SHORT
                    hs = half_spread(i)
                    s = slip()
                    fill = o[i] + d.sign * (hs + s)
                    stop = fill - d.sign * sdist[i - 1]
                    target = fill + d.sign * tdist[i - 1]
                    sizing = size_position(balance, balance, cfg.risk_pct, fill, stop, symbol, cfg.account_ccy,
                                           {symbol: fill})
                    if sizing.units > 0:
                        partial = sizing.units > cfg.partial_fill_threshold_units
                        if partial:  # remainder filled at a worse price -> volume-weighted average
                            rest = sizing.units - cfg.partial_fill_threshold_units
                            worse = fill + d.sign * cfg.partial_fill_extra_slippage_pips * spec.pip_size
                            fill = (fill * cfg.partial_fill_threshold_units + worse * rest) / sizing.units
                        pos = BacktestTrade(len(trades) + 1, symbol, strategy.name, d, sizing.units, times[i],
                                            fill, stop, target, session=market_session_name(times[i]),
                                            partial_fill=partial)
                        pos.initial_risk = money(abs(fill - stop), sizing.units, fill)
                        pos.commission = -sizing.units / 100_000 * cfg.commission_per_lot_side
                        pos.slippage_cost = money(s, sizing.units, fill)

            # --- 2. manage the open position on this bar
            if pos is not None:
                d = pos.direction.sign
                hs = half_spread(i)
                if i > 0 and pos.entry_time < times[i]:
                    nights = _crossed_rollover(times[i - 1], times[i])
                    if nights:
                        swap_pips = cfg.swap_long_pips if d > 0 else cfg.swap_short_pips
                        pos.swap += nights * money(swap_pips * spec.pip_size, pos.units, c[i - 1])
                # executable exit prices: long exits on bid, short exits on ask
                ex_open, ex_high, ex_low = o[i] - d * hs, h[i] - d * hs, l[i] - d * hs
                adverse_extreme = ex_low if d > 0 else ex_high
                favourable_extreme = ex_high if d > 0 else ex_low
                stop_hit_at_open = (ex_open - pos.stop_loss) * d <= 0
                if pos.entry_time < times[i] and stop_hit_at_open:
                    close_position(i, ex_open, "stop_loss_gap", slip(cfg.stop_slippage_pips))
                elif (adverse_extreme - pos.stop_loss) * d <= 0:
                    close_position(i, pos.stop_loss, "stop_loss", slip(cfg.stop_slippage_pips))
                elif (favourable_extreme - pos.take_profit) * d >= 0:
                    close_position(i, pos.take_profit, "take_profit", 0.0)
                else:
                    pos.bars_held += 1
                    if cfg.max_bars_in_trade and pos.bars_held >= cfg.max_bars_in_trade:
                        close_position(i, c[i] - d * hs, "time_exit", slip())

            # --- 3. mark to market and risk halts
            mtm = balance
            if pos is not None:
                d = pos.direction.sign
                mtm += money((c[i] - d * half_spread(i) - pos.entry_price) * d, pos.units, c[i]) + pos.commission + pos.swap
            equity[i] = mtm
            peak = max(peak, mtm)
            if not halted and (peak - mtm) / peak * 100 >= cfg.max_drawdown_pct:
                halted = True
                halts.append(f"{times[i].isoformat()}: max drawdown {cfg.max_drawdown_pct}% reached, trading halted")
                if pos is not None:
                    close_position(i, c[i] - pos.direction.sign * half_spread(i), "risk_halt", slip())
                    equity[i] = balance

        if pos is not None:  # close at the end of data
            close_position(n - 1, c[-1] - pos.direction.sign * half_spread(n - 1), "end_of_data", 0.0)
            equity[n - 1] = balance
        eq = pd.Series(equity, index=df.index).ffill().fillna(cfg.initial_balance)
        info = {"synthetic": bool(df.attrs.get("synthetic", False)), "source": df.attrs.get("source", "unknown"),
                "start": times[0].isoformat() if n else None, "end": times[-1].isoformat() if n else None,
                "bars": n}
        return BacktestResult(symbol, strategy.name, dict(strategy.params), cfg, trades, eq, rejected, halts, info)
