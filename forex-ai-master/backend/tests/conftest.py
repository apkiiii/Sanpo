"""Shared fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.config.settings import RiskLimits, TradingMode
from backend.core.types import AccountSnapshot, Direction, Quote, TradeProposal, TradeSetup, new_id
from backend.risk.risk_engine import RiskEngine, RiskState
from backend.risk.safety_manager import SafetyManager
from backend.trading.simulated_broker import SimulatedBroker, SimulatedBrokerConfig

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
MIDS = {"EURUSD": 1.10, "GBPUSD": 1.27, "USDJPY": 150.0, "USDCHF": 0.88, "AUDUSD": 0.66, "USDCAD": 1.36,
        "NZDUSD": 0.60}


@pytest.fixture
def limits() -> RiskLimits:
    return RiskLimits()


@pytest.fixture
def engine(limits: RiskLimits) -> RiskEngine:
    return RiskEngine(limits)


def make_setup(symbol: str = "EURUSD", direction: Direction = Direction.LONG, entry: float = 1.10000,
               stop_pips: float = 20.0, rr: float = 2.0, strategy: str = "test") -> TradeSetup:
    pip = 0.01 if symbol.endswith("JPY") else 0.0001
    d = direction.sign
    stop = entry - d * stop_pips * pip
    tp = entry + d * stop_pips * rr * pip
    return TradeSetup(symbol, direction, entry, stop, tp, stop, strategy, "M15", 0.6, "unit test")


def make_proposal(setup: TradeSetup | None = None, exceptional: bool = False) -> TradeProposal:
    return TradeProposal(new_id("PROP"), setup or make_setup(), 0.8, ("a", "b", "c"), exceptional, NOW)


def make_state(balance: float = 100_000.0, equity: float | None = None, symbol: str = "EURUSD",
               spread_pips: float = 0.8, **kw: object) -> RiskState:
    equity = balance if equity is None else equity
    mid = MIDS[symbol]
    pip = 0.01 if symbol.endswith("JPY") else 0.0001
    q = Quote(symbol, mid - spread_pips * pip / 2, mid + spread_pips * pip / 2, NOW)
    base = dict(mode=TradingMode.SIMULATION, account=AccountSnapshot(balance, equity, 0.0, equity),
                peak_equity=max(balance, equity), day_start_equity=balance, week_start_equity=balance,
                daily_pnl=equity - balance, weekly_pnl=equity - balance, open_positions=[], quote=q,
                mid_prices=dict(MIDS), atr_spike_ratio=1.0, now=NOW)
    base.update(kw)
    return RiskState(**base)  # type: ignore[arg-type]


@pytest.fixture
def safety() -> SafetyManager:
    return SafetyManager(max_consecutive_errors=3)


@pytest.fixture
def broker() -> SimulatedBroker:
    b = SimulatedBroker(SimulatedBrokerConfig(slippage_pips=0.0))
    b.clock = NOW
    b.connect()
    for sym, mid in MIDS.items():
        b.set_mid(sym, mid, 0.8 if not sym.endswith("JPY") else 1.0, NOW)
    return b


@pytest.fixture(autouse=True)
def _default_trade_sizes():  # type: ignore[no-untyped-def]
    """Trade-size rules are process-wide configuration: reset them around every test."""
    from backend.core.instruments import configure_trade_sizes
    configure_trade_sizes(1_000, 1_000)
    yield
    configure_trade_sizes(1_000, 1_000)
