"""Backtesting engine: hand-built bars with known expected outcomes."""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

import numpy as np
import pandas as pd
import pytest

from backend.backtesting.engine import BacktestConfig, BacktestEngine, _crossed_rollover
from backend.backtesting.metrics import compute_metrics, max_drawdown, trade_stats
from backend.backtesting.reports import walk_forward_markdown
from backend.backtesting.validation import assess_eligibility, monte_carlo, walk_forward
from backend.core.types import Timeframe
from backend.data.synthetic import generate_ohlc
from backend.strategies import STRATEGIES
from backend.strategies.base import Strategy

PIP = 0.0001


class Scripted(Strategy):
    """Emits pre-defined signals: {bar_index: (signal, stop_pips, target_pips)}."""

    name = "scripted"
    default_params = {"script": {}}

    def _signals(self, ind: pd.DataFrame):  # type: ignore[no-untyped-def]
        n = len(ind)
        sig, stop, tgt = np.zeros(n), np.full(n, np.nan), np.full(n, np.nan)
        for i, (s, sp, tp) in self.params["script"].items():
            sig[i], stop[i], tgt[i] = s, sp * PIP, tp * PIP
        return pd.Series(sig, ind.index), pd.Series(stop, ind.index), pd.Series(tgt, ind.index)


def bars(prices: list[tuple[float, float, float, float]], start: datetime = datetime(2026, 1, 6, 8, tzinfo=UTC),
         freq: str = "1h") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(prices), freq=freq, tz="UTC")
    return pd.DataFrame(prices, columns=["open", "high", "low", "close"], index=idx)


def flat(n: int, p: float = 1.1) -> list[tuple[float, float, float, float]]:
    return [(p, p + 2 * PIP, p - 2 * PIP, p)] * n


def cfg(**kw: object) -> BacktestConfig:
    base = dict(warmup_bars=0, slippage_pips=0.0, stop_slippage_pips=0.0, latency_ms=0.0, commission_per_lot_side=0.0,
                spread_pips={"EURUSD": 0.0}, swap_long_pips=0.0, swap_short_pips=0.0)
    base.update(kw)
    return BacktestConfig(**base)  # type: ignore[arg-type]


def run(prices, script, **kw):  # type: ignore[no-untyped-def]
    df = bars(prices)
    return BacktestEngine(cfg(**kw)).run("EURUSD", df, Scripted(script=script))


def test_entry_on_next_open_and_take_profit() -> None:
    p = flat(3) + [(1.1000, 1.1045, 1.0995, 1.1040)] + flat(3, 1.104)
    res = run(p, {2: (1, 20, 40)})  # signal on bar 2 close -> entry at bar 3 open (1.1000)
    t = res.trades[0]
    assert t.entry_time == bars(p).index[3] and t.entry_price == pytest.approx(1.1000)
    assert t.exit_reason == "take_profit" and t.exit_price == pytest.approx(1.1040)
    assert t.r_multiple == pytest.approx(2.0, rel=1e-3)


def test_stop_loss_and_same_bar_conservative() -> None:
    # Bar touches both TP and SL: the stop must be assumed first.
    p = flat(3) + [(1.1000, 1.1050, 1.0970, 1.1000)] + flat(2)
    t = run(p, {2: (1, 20, 40)}).trades[0]
    assert t.exit_reason == "stop_loss" and t.r_multiple == pytest.approx(-1.0, rel=1e-3)


def test_gap_through_stop_exits_at_open() -> None:
    p = flat(3) + [(1.1000, 1.1005, 1.0995, 1.1000), (1.0950, 1.0955, 1.0940, 1.0950)] + flat(2, 1.095)
    t = run(p, {2: (1, 20, 40)}).trades[0]
    assert t.exit_reason == "stop_loss_gap" and t.exit_price == pytest.approx(1.0950)
    assert t.r_multiple < -2.0  # worse than the planned 1R


def test_short_trade_and_spread_cost() -> None:
    p = flat(3) + [(1.1000, 1.1005, 1.0955, 1.0960)] + flat(2, 1.096)
    res = run(p, {2: (-1, 20, 40)}, spread_pips={"EURUSD": 1.0})
    t = res.trades[0]
    assert t.entry_price == pytest.approx(1.1000 - 0.5 * PIP)  # short fills on the bid
    assert t.exit_reason == "take_profit"


def test_commission_and_slippage_reduce_pnl() -> None:
    p = flat(3) + [(1.1000, 1.1045, 1.0995, 1.1040)] + flat(2, 1.104)
    clean = run(p, {2: (1, 20, 40)}).trades[0]
    costly = run(p, {2: (1, 20, 40)}, commission_per_lot_side=3.5, slippage_pips=0.5, latency_ms=200).trades[0]
    assert costly.commission < 0 and costly.slippage_cost > 0 and costly.net_pnl < clean.net_pnl


def test_swap_rollover_counting() -> None:
    tue = datetime(2026, 1, 6, 21, tzinfo=UTC)  # Tuesday
    assert _crossed_rollover(tue, tue + timedelta(hours=2)) == 1
    wed = datetime(2026, 1, 7, 21, tzinfo=UTC)
    assert _crossed_rollover(wed, wed + timedelta(hours=2)) == 3  # Wednesday triple
    assert _crossed_rollover(tue, tue + timedelta(days=7)) == 7  # a full week = 7 nights
    p = flat(3) + flat(40)
    res = run(p, {2: (1, 20, 40)}, swap_long_pips=-1.0)
    assert res.trades[0].swap < 0


def test_daily_loss_halt_blocks_new_entries() -> None:
    losing = [(1.1000, 1.1002, 1.0970, 1.0975)]
    p = flat(3) + losing + flat(2) + losing + flat(2) + losing + flat(5)
    script = {2: (1, 20, 40), 5: (1, 20, 40), 8: (1, 20, 40)}
    res = run(p, script, risk_pct=0.5, daily_loss_limit_pct=1.0)
    assert len(res.trades) == 2  # third entry refused: daily loss limit reached


def test_drawdown_halt() -> None:
    losing = [(1.1000, 1.1002, 1.0970, 1.0975)]
    p, script = flat(1), {}
    for k in range(10):
        script[len(p)] = (1, 20, 40)
        p += flat(1) + losing + flat(30)  # several days apart
    res = run(p, script, risk_pct=1.0, daily_loss_limit_pct=1.5, max_drawdown_pct=3.0)
    # entries stop *before* the limit can be breached (projected rule), so the equity drawdown stays below it
    assert len(res.trades) == 3  # 1.99 % + 1 % fits in 3 %; a 4th would not
    assert max_drawdown(res.equity_curve)[1] < 3.0


def test_no_entry_when_market_closed() -> None:
    sat = datetime(2026, 1, 10, 8, tzinfo=UTC)
    df = bars(flat(10), start=sat)
    res = BacktestEngine(cfg()).run("EURUSD", df, Scripted(script={2: (1, 20, 40)}))
    assert res.trades == []


def test_rejections_and_partial_fills() -> None:
    p = flat(3) + [(1.1000, 1.1045, 1.0995, 1.1040)] + flat(3, 1.104)
    assert run(p, {2: (1, 20, 40)}, reject_probability=1.0).rejected_orders == 1
    res = run(p, {2: (1, 20, 40)}, partial_fill_threshold_units=10_000)
    assert res.trades[0].partial_fill and res.trades[0].entry_price > 1.1000


def test_metrics_known_values() -> None:
    class T:
        def __init__(self, pnl: float, r: float) -> None:
            self.net_pnl, self.r_multiple, self.symbol, self.strategy, self.session = pnl, r, "EURUSD", "s", "london"
            self.commission = self.swap = self.slippage_cost = 0.0
            self.entry_time = datetime(2026, 1, 1, tzinfo=UTC)
            self.exit_time = self.entry_time + timedelta(hours=2)

    trades = [T(200, 2), T(-100, -1), T(200, 2), T(-100, -1)]
    st = trade_stats(trades)  # type: ignore[arg-type]
    assert st["profit_factor"] == pytest.approx(2.0) and st["win_rate"] == 0.5
    assert st["expectancy"] == pytest.approx(50.0) and st["expectancy_r"] == pytest.approx(0.5)
    eq = pd.Series([100.0, 110.0, 99.0, 120.0], index=pd.date_range("2026-01-01", periods=4, freq="1D", tz="UTC"))
    dd, pct = max_drawdown(eq)
    assert dd == pytest.approx(11.0) and pct == pytest.approx(10.0)
    m = compute_metrics(trades, eq, 100.0)  # type: ignore[arg-type]
    assert m["avg_duration_hours"] == pytest.approx(2.0) and "london" in m["by_session"]


def test_monte_carlo_shapes() -> None:
    mc = monte_carlo([100, -50, 80, -40, 60, -30] * 5, 10_000, 5.0, runs=500)
    assert mc["runs"] == 500 and mc["return_p5"] <= mc["return_p50"] <= mc["return_p95"]
    assert monte_carlo([1, 2], 1000, 5)["runs"] == 0


def test_all_strategies_run_without_lookahead() -> None:
    """Truncating future data must not change past signals (causality check)."""
    df = generate_ohlc("EURUSD", datetime(2025, 1, 6, tzinfo=UTC), 1500, Timeframe.H1, seed=4)
    for name, cls in STRATEGIES.items():
        full = cls().generate(df)
        part = cls().generate(df.iloc[:1000])
        pd.testing.assert_series_equal(full["signal"].iloc[:1000], part["signal"], check_names=False,
                                       obj=f"{name} signals")


def test_walk_forward_and_report_on_synthetic_flags_it() -> None:
    df = generate_ohlc("EURUSD", datetime(2025, 1, 6, tzinfo=UTC), 4000, Timeframe.H1, seed=8)
    wf = walk_forward("EURUSD", df, STRATEGIES["trend_following"], BacktestConfig(), n_folds=2, grid_limit=4,
                      mc_runs=100)
    assert len(wf.folds) == 2 and wf.synthetic
    eligible, reasons = assess_eligibility(wf)
    assert not eligible
    md = walk_forward_markdown([(wf, eligible, reasons)], "Test", "synthetic")
    assert "SYNTHETIC DATA" in md and "no predictive value" in md


@pytest.mark.critical
def test_no_edge_on_pure_random_walk() -> None:
    """Anti-lookahead sanity check: on a driftless random walk no strategy may show a significant edge."""
    from backend.backtesting.engine import BacktestEngine as BE
    for name, cls in STRATEGIES.items():
        rs: list[float] = []
        for seed in range(3):
            df = generate_ohlc("EURUSD", datetime(2023, 1, 2, tzinfo=UTC), 6000, Timeframe.H1, seed=700 + seed,
                               drift_regimes=False)
            res = BE(BacktestConfig(max_drawdown_pct=100, daily_loss_limit_pct=100)).run("EURUSD", df, cls())
            rs += [t.r_multiple for t in res.trades]
        if len(rs) < 30:
            continue
        arr = np.array(rs)
        se = arr.std(ddof=1) / np.sqrt(len(arr))
        assert arr.mean() < 3 * se, f"{name}: suspicious edge {arr.mean():+.3f}R on random data (se {se:.3f})"
