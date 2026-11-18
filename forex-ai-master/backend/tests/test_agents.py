"""Each agent is tested independently with controlled data."""

import json
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from backend.agents.base import Agent, MarketContext
from backend.agents.macro_analyst import MacroAnalyst
from backend.agents.market_analyst import MarketAnalyst
from backend.agents.price_action_analyst import PriceActionAnalyst
from backend.agents.risk_manager import RiskManager
from backend.agents.strategy_optimizer import StrategyOptimizer
from backend.agents.technical_analyst import TechnicalAnalyst
from backend.backtesting.engine import BacktestConfig
from backend.core.types import AgentReport, AgentStatus, Direction, Timeframe
from backend.data.economic_calendar import EconomicEvent, FileCalendarProvider, StaticCalendarProvider
from backend.data.market_data import resample_ohlc
from backend.data.synthetic import generate_ohlc, ranging_series, trending_series
from backend.tests.conftest import NOW


def _mtf(direction: int = 1, n: int = 4000) -> dict[Timeframe, pd.DataFrame]:
    m15 = trending_series(n, step=0.00012 * direction / abs(direction), noise=0.00005, timeframe=Timeframe.M15,
                          direction=1)
    frames = {Timeframe.M15: m15}
    for tf in (Timeframe.M30, Timeframe.H1, Timeframe.H4):
        frames[tf] = resample_ohlc(m15, tf)
    return frames


def ctx(frames: dict[Timeframe, pd.DataFrame], symbol: str = "EURUSD") -> MarketContext:
    return MarketContext(symbol, NOW, frames)


class _Boom(Agent):
    name = "boom"

    def _analyze(self, ctx: MarketContext) -> AgentReport:
        raise RuntimeError("unexpected")


def test_agent_exception_becomes_error_report() -> None:
    a = _Boom()
    r = a.analyze(ctx({}))
    assert r.status is AgentStatus.ERROR and r.signal is Direction.NEUTRAL and a.health.errors == 1


# ---------------------------------------------------------------- BOT 1
def test_market_analyst_uptrend_and_downtrend() -> None:
    up = MarketAnalyst().analyze(ctx(_mtf(1)))
    assert up.status is AgentStatus.OK and up.signal is Direction.LONG
    assert "H4" in up.details["timeframes"] and up.details["timeframes"]["H1"]["supports"] is not None
    down = MarketAnalyst().analyze(ctx(_mtf(-1)))
    assert down.signal is Direction.SHORT


def test_market_analyst_insufficient_data() -> None:
    r = MarketAnalyst().analyze(ctx({Timeframe.H1: trending_series(50)}))
    assert r.status is AgentStatus.INSUFFICIENT_DATA and r.signal is Direction.NEUTRAL


def test_market_analyst_detects_range() -> None:
    rng = ranging_series(600, amplitude=0.0015, timeframe=Timeframe.H1)
    tf = MarketAnalyst().analyze_timeframe(rng)
    assert tf["regime"] in ("ranging", "transition")


# ---------------------------------------------------------------- BOT 2
def test_technical_analyst_requires_multiple_confirmations() -> None:
    assert TechnicalAnalyst(min_confirmations=1).min_confirmations == 2  # one indicator is never enough
    r = TechnicalAnalyst(Timeframe.H1).analyze(ctx({Timeframe.H1: trending_series(600, direction=1)}))
    assert r.status is AgentStatus.OK
    assert r.signal in (Direction.LONG, Direction.NEUTRAL)
    assert set(r.details["votes"]) >= {"ema_stack", "macd", "rsi", "adx", "bollinger"}


def test_technical_analyst_downtrend_never_long() -> None:
    r = TechnicalAnalyst(Timeframe.H1).analyze(ctx({Timeframe.H1: trending_series(600, direction=-1)}))
    assert r.signal in (Direction.SHORT, Direction.NEUTRAL)


def test_technical_analyst_missing_data() -> None:
    r = TechnicalAnalyst(Timeframe.H1).analyze(ctx({}))
    assert r.status is AgentStatus.INSUFFICIENT_DATA


def test_technical_analyst_rejects_nan_data() -> None:
    df = trending_series(600)
    df.iloc[-5, df.columns.get_loc("close")] = np.nan
    r = TechnicalAnalyst(Timeframe.H1).analyze(ctx({Timeframe.H1: df}))
    assert r.status is AgentStatus.INSUFFICIENT_DATA


# ---------------------------------------------------------------- BOT 3
def test_price_action_setups_are_consistent() -> None:
    agent = PriceActionAnalyst(Timeframe.H1)
    found = 0
    df = generate_ohlc("EURUSD", NOW - timedelta(days=200), 3000, Timeframe.H1, seed=5)
    for end in range(300, 3000, 25):
        r = agent.analyze(ctx({Timeframe.H1: df.iloc[:end]}))
        assert r.status is AgentStatus.OK
        if r.signal is Direction.NEUTRAL:
            assert "setup" not in r.details
            continue
        found += 1
        s = r.details["setup"]
        d = 1 if s["direction"] == "long" else -1
        assert (s["entry"] - s["stop_loss"]) * d > 0 and (s["take_profit"] - s["entry"]) * d > 0
        assert s["reward_risk"] >= 1.5
    assert found > 0


def test_price_action_no_fabricated_signal_on_flat_data() -> None:
    idx = pd.date_range("2025-01-06", periods=300, freq="1h", tz="UTC")
    flat = pd.DataFrame({"open": 1.1, "high": 1.1001, "low": 1.0999, "close": 1.1}, index=idx)
    r = PriceActionAnalyst(Timeframe.H1).analyze(ctx({Timeframe.H1: flat}))
    assert r.signal is Direction.NEUTRAL


def test_price_action_insufficient() -> None:
    r = PriceActionAnalyst(Timeframe.H1).analyze(ctx({Timeframe.H1: trending_series(30)}))
    assert r.status is AgentStatus.INSUFFICIENT_DATA


# ---------------------------------------------------------------- BOT 4
def test_macro_blocks_when_calendar_unavailable() -> None:
    r = MacroAnalyst().analyze(ctx({}))
    assert r.status is AgentStatus.BLOCK
    r2 = MacroAnalyst(block_when_unavailable=False).analyze(ctx({}))
    assert r2.status is AgentStatus.DATA_UNAVAILABLE


def test_macro_blackout_window() -> None:
    ev = EconomicEvent(NOW + timedelta(minutes=20), "USD", "high", "Non-Farm Payrolls")
    agent = MacroAnalyst(StaticCalendarProvider([ev], NOW))
    assert agent.analyze(ctx({})).status is AgentStatus.BLOCK  # EURUSD contains USD
    assert agent.analyze(ctx({}, "AUDNZD" if False else "EURUSD")).status is AgentStatus.BLOCK
    far = MacroAnalyst(StaticCalendarProvider([EconomicEvent(NOW + timedelta(hours=3), "USD", "high", "CPI")], NOW))
    r = far.analyze(ctx({}))
    assert r.status is AgentStatus.OK and r.details["upcoming"]
    other_ccy = MacroAnalyst(StaticCalendarProvider([EconomicEvent(NOW, "JPY", "high", "BoJ")], NOW))
    assert other_ccy.analyze(ctx({})).status is AgentStatus.OK


def test_macro_keyword_forces_high_impact() -> None:
    ev = EconomicEvent(NOW + timedelta(minutes=5), "EUR", "medium", "ECB Interest Rate Decision")
    assert MacroAnalyst(StaticCalendarProvider([ev], NOW)).analyze(ctx({})).status is AgentStatus.BLOCK


def test_macro_manual_risk_off() -> None:
    agent = MacroAnalyst(StaticCalendarProvider([], NOW))
    agent.set_manual_risk_off("geopolitical shock")
    assert agent.analyze(ctx({})).status is AgentStatus.BLOCK


def test_calendar_file_provider(tmp_path: pytest.TempPathFactory) -> None:
    p = tmp_path / "cal.json"  # type: ignore[operator]
    p.write_text(json.dumps({"generated_at": NOW.isoformat(), "events": [
        {"time": (NOW + timedelta(minutes=10)).isoformat(), "currency": "EUR", "impact": "high", "title": "CPI"}]}))
    snap = FileCalendarProvider(p).snapshot(NOW)
    assert snap.available and snap.events[0].currency == "EUR"
    stale = FileCalendarProvider(p).snapshot(NOW + timedelta(days=30))
    assert not stale.available and "stale" in stale.reason
    bad = tmp_path / "bad.csv"  # type: ignore[operator]
    bad.write_text("time,currency,impact,title\n2026-01-01T00:00:00Z,USD,extreme,X\n")
    assert not FileCalendarProvider(bad).snapshot(NOW).available


# ---------------------------------------------------------------- BOT 5
def test_risk_manager_report_blocks_on_kill_switch(limits, safety) -> None:  # type: ignore[no-untyped-def]
    rm = RiskManager(limits, safety, timeframe=Timeframe.H1)
    frames = {Timeframe.H1: trending_series(300)}
    assert rm.analyze(ctx(frames)).status is AgentStatus.OK
    safety.activate_kill_switch("test", "operator")
    assert rm.analyze(ctx(frames)).status is AgentStatus.BLOCK


def test_risk_manager_limits_are_read_only(limits, safety) -> None:  # type: ignore[no-untyped-def]
    rm = RiskManager(limits, safety)
    with pytest.raises(AttributeError):
        rm.limits = limits  # type: ignore[misc]
    with pytest.raises(Exception):
        rm.limits.max_open_positions = 50  # type: ignore[misc]


# ---------------------------------------------------------------- BOT 6
def test_strategy_optimizer_never_validates_synthetic_data() -> None:
    df = generate_ohlc("EURUSD", NOW - timedelta(days=120), 3000, Timeframe.H1, seed=9)
    opt = StrategyOptimizer(Timeframe.H1, BacktestConfig(), n_folds=2)
    recs = opt.validate("EURUSD", df, ["trend_following"])
    assert recs[0].synthetic and not recs[0].eligible
    assert any("SYNTHETIC" in r for r in recs[0].reasons)
    r = opt.analyze(ctx({Timeframe.H1: df}))
    assert r.signal is Direction.NEUTRAL and "no validated strategy" in r.summary
