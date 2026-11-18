"""Master bot: consensus rules, decisions and full cycles against the simulated broker."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from backend.agents.base import MarketContext
from backend.config.settings import Settings, TargetReachedPolicy, TradingMode
from backend.core.errors import ConfigurationError, LiveModeNotAllowedError
from backend.core.types import AgentReport, AgentStatus, ClosedTrade, Direction, Timeframe
from backend.data.economic_calendar import StaticCalendarProvider
from backend.data.market_data import CSVMarketDataProvider
from backend.data.synthetic import generate_ohlc
from backend.master.consensus_engine import ConsensusEngine
from backend.master.decision_engine import DecisionEngine
from backend.master.master_controller import MasterController
from backend.master.state_store import InMemoryStateStore
from backend.trading.simulated_broker import SimulatedBroker, SimulatedBrokerConfig
from backend.tests.conftest import NOW, make_setup

pytestmark = pytest.mark.critical


def rep(agent: str, signal: Direction = Direction.NEUTRAL, conf: float = 0.8,
        status: AgentStatus = AgentStatus.OK, **details: Any) -> AgentReport:
    return AgentReport(agent, "EURUSD", status, signal, conf, f"{agent} test", details, ("test",), NOW)


def favourable(direction: Direction = Direction.LONG, setup: dict | None = None) -> dict[str, AgentReport]:
    pa_details = {"setup": setup} if setup else {}
    return {
        "market_analyst": rep("market_analyst", direction, 0.8,
                              timeframes={"M15": {"regime": "trending"}}),
        "technical_analyst": rep("technical_analyst", direction, 0.7),
        "price_action_analyst": rep("price_action_analyst", direction, 0.6, **pa_details),
        "macro_analyst": rep("macro_analyst", Direction.NEUTRAL, 1.0),
        "risk_manager": rep("risk_manager", Direction.NEUTRAL, 1.0),
        "strategy_optimizer": rep("strategy_optimizer", direction, 0.6),
        "execution_monitor": rep("execution_monitor", Direction.NEUTRAL, 1.0),
    }


# ======================================================================== consensus
def test_consensus_accepts_aligned_reports() -> None:
    r = ConsensusEngine().evaluate(favourable(), Direction.LONG, "pullback", "trending")
    assert r.accepted and set(r.supporting) == {"market_analyst", "technical_analyst", "price_action_analyst",
                                                "strategy_optimizer"}


def test_master_refuses_with_six_of_seven_favourable() -> None:
    reports = favourable()
    reports["macro_analyst"] = rep("macro_analyst", status=AgentStatus.BLOCK)
    r = ConsensusEngine().evaluate(reports, Direction.LONG, "pullback", "trending")
    assert not r.accepted and any("macro_analyst blocks" in v for v in r.vetoes)


def test_consensus_detects_contradictions() -> None:
    reports = favourable()
    reports["technical_analyst"] = rep("technical_analyst", Direction.SHORT)
    r = ConsensusEngine().evaluate(reports, Direction.LONG, "pullback", "trending")
    assert not r.accepted and r.contradictions and "technical_analyst" in r.opposing


def test_consensus_rejects_unverified_data_and_errors() -> None:
    reports = favourable()
    reports["market_analyst"] = rep("market_analyst", status=AgentStatus.INSUFFICIENT_DATA)
    assert not ConsensusEngine().evaluate(reports, Direction.LONG, "pullback").accepted
    reports = favourable()
    reports["execution_monitor"] = rep("execution_monitor", status=AgentStatus.ERROR)
    assert not ConsensusEngine().evaluate(reports, Direction.LONG, "pullback").accepted
    reports = favourable()
    reports["macro_analyst"] = rep("macro_analyst", status=AgentStatus.DATA_UNAVAILABLE)
    assert not ConsensusEngine().evaluate(reports, Direction.LONG, "pullback").accepted
    assert ConsensusEngine(allow_missing_macro=True).evaluate(reports, Direction.LONG, "pullback").accepted


def test_consensus_requires_support_and_regime_compatibility() -> None:
    reports = {k: (rep(k) if k in ("market_analyst", "technical_analyst", "strategy_optimizer") else v)
               for k, v in favourable().items()}
    assert not ConsensusEngine().evaluate(reports, Direction.LONG, "pullback").accepted  # 1 supporter only
    assert not ConsensusEngine().evaluate(favourable(), Direction.LONG, "mean_reversion", "trending").accepted


def test_confidence_alone_is_not_enough() -> None:
    reports = favourable()
    reports["risk_manager"] = rep("risk_manager", status=AgentStatus.BLOCK)
    for name in ("market_analyst", "technical_analyst", "price_action_analyst", "strategy_optimizer"):
        reports[name] = rep(name, Direction.LONG, 1.0)
    assert not ConsensusEngine().evaluate(reports, Direction.LONG, "pullback").accepted


# ======================================================================== decision engine
def _pa_setup(direction: str = "long", strategy: str = "pullback") -> dict:
    s = make_setup(direction=Direction(direction), strategy=strategy)
    return {"direction": s.direction.value, "entry": s.entry, "stop_loss": s.stop_loss, "take_profit": s.take_profit,
            "invalidation": s.invalidation, "strategy": s.strategy, "reward_risk": s.reward_risk}


def test_demo_and_live_require_validated_and_whitelisted_strategies() -> None:
    reports = favourable(setup=_pa_setup())
    sim = DecisionEngine(ConsensusEngine(), TradingMode.SIMULATION)
    assert any(p is not None for _, p in sim.evaluate_symbol("EURUSD", reports, [], set(), "trending", NOW))
    demo = DecisionEngine(ConsensusEngine(), TradingMode.DEMO)
    assert all(p is None for _, p in demo.evaluate_symbol("EURUSD", reports, [], set(), "trending", NOW))
    live = DecisionEngine(ConsensusEngine(), TradingMode.LIVE, live_whitelist=[])
    res = live.evaluate_symbol("EURUSD", reports, [], {"pullback"}, "trending", NOW)
    assert all(p is None for _, p in res) and any("whitelisted" in r for d, _ in res for r in d.reasons)


# ======================================================================== controller
def build(tmp_settings: dict | None = None, policy_target: TargetReachedPolicy = TargetReachedPolicy.BREAKEVEN,
          store: InMemoryStateStore | None = None) -> tuple[MasterController, SimulatedBroker]:
    df = generate_ohlc("EURUSD", NOW - timedelta(days=60), 20000, Timeframe.M5, seed=3)
    df = df.loc[: NOW - timedelta(minutes=5)]
    provider = CSVMarketDataProvider("/nonexistent")
    provider.add_frame("EURUSD", df)
    provider.cursor = NOW
    broker = SimulatedBroker(SimulatedBrokerConfig(slippage_pips=0.0), provider)
    broker.clock = NOW
    broker.connect()
    last = float(df["close"].iloc[-1])
    broker.set_mid("EURUSD", last, 0.6, NOW)
    kw = {"target_session_profit": 1000, "target_reached_policy": policy_target, **(tmp_settings or {})}
    settings = Settings(_env_file=None, symbols=["EURUSD"], timeframes=["M15", "H1", "H4"], entry_timeframe="M15",
                        **kw)
    cal = StaticCalendarProvider([], NOW)
    m = MasterController(settings, broker, provider, cal, store=store or InMemoryStateStore())
    return m, broker


def force_reports(m: MasterController, reports: dict[str, AgentReport]) -> None:
    for name, agent in m.agents.items():
        r = reports[name]
        agent.analyze = (lambda r: lambda ctx: r)(r)  # type: ignore[method-assign]


def setup_at_market(broker: SimulatedBroker, stop_pips: float = 20.0) -> dict:
    q = broker.get_quote("EURUSD")
    s = make_setup(entry=q.ask, stop_pips=stop_pips)
    return {"direction": "long", "entry": s.entry, "stop_loss": s.stop_loss, "take_profit": s.take_profit,
            "invalidation": s.invalidation, "strategy": "pullback", "reward_risk": s.reward_risk}


def test_full_cycle_executes_only_after_risk_approval() -> None:
    m, broker = build()
    force_reports(m, favourable(setup=setup_at_market(broker)))
    report = m.run_cycle(NOW)
    assert report.entries_allowed, report.blocking_reasons
    executed = [d for d in report.decisions if d.status == "executed"]
    assert len(executed) == 1, [(d.status, d.reasons) for d in report.decisions]
    assert executed[0].risk_checks and all(c["ok"] for c in executed[0].risk_checks.values())
    positions = broker.get_positions()
    assert len(positions) == 1 and positions[0].stop_loss is not None
    managed = m.execution.positions.open_positions()[0]
    assert managed.strategy == "pullback" and managed.validated_by and managed.initial_risk > 0
    assert managed.initial_risk <= 100_000 * 0.0025 * 1.05
    assert m.audit.verify()[0]
    # a second cycle with the same setup must not open a duplicate position on the same symbol
    m.run_cycle(NOW)
    assert len(broker.get_positions()) == 1


def test_risk_manager_veto_blocks_execution() -> None:
    m, broker = build()
    broker.set_mid("EURUSD", broker.get_quote("EURUSD").mid, 9.0, NOW)  # abnormal spread
    force_reports(m, favourable(setup=setup_at_market(broker)))
    report = m.run_cycle(NOW)
    assert not broker.get_positions()
    assert any(d.status == "rejected_risk" and any("spread" in r for r in d.reasons) for d in report.decisions)


def test_master_cannot_modify_risk_limits() -> None:
    m, _ = build()
    with pytest.raises(Exception):
        m.risk_manager.limits.max_risk_per_trade_pct = 10  # type: ignore[misc]
    with pytest.raises(AttributeError):
        m.risk_manager.limits = None  # type: ignore[misc,assignment]
    with pytest.raises(AttributeError):
        m.mode = TradingMode.LIVE  # type: ignore[misc]


def test_kill_switch_blocks_entries_and_keeps_monitoring() -> None:
    m, broker = build()
    force_reports(m, favourable(setup=setup_at_market(broker)))
    m.run_cycle(NOW)
    assert len(broker.get_positions()) == 1
    m.safety.activate_kill_switch("operator test", "admin")
    assert any(a["type"] == "kill_switch" for a in m.alerts.recent())
    assert any(e.action == "kill_switch_activated" for e in m.audit.entries)
    # positions kept (policy keep_with_stops) and still reconciled/managed
    assert len(broker.get_positions()) == 1
    report = m.run_cycle(NOW)
    assert not report.entries_allowed and any("kill switch" in r for r in report.blocking_reasons)
    pid = broker.get_positions()[0].position_id
    broker.set_mid("EURUSD", 1.0, 0.6, NOW)  # crash through the stop
    m.run_cycle(NOW)
    assert pid not in m.execution.positions.positions
    assert m.portfolio.session and m.portfolio.session.trades == 1


def test_kill_switch_close_all_policy() -> None:
    m, broker = build({"emergency_close_policy": "close_all"})
    force_reports(m, favourable(setup=setup_at_market(broker)))
    m.run_cycle(NOW)
    m.safety.activate_kill_switch("emergency", "admin")
    assert broker.get_positions() == []


def test_session_target_stops_new_entries() -> None:
    m, broker = build({"target_session_profit": 100})
    force_reports(m, favourable(setup=setup_at_market(broker)))
    m.run_cycle(NOW)
    pos = broker.get_positions()[0]
    broker.set_mid("EURUSD", pos.take_profit + 0.0005, 0.6, NOW)  # take-profit hit: +2R ~ +500 USD
    report = m.run_cycle(NOW)
    assert m.portfolio.target.reached
    assert any(a["type"] == "session_target_reached" for a in m.alerts.recent())
    assert "session target reached" in report.blocking_reasons
    force_reports(m, favourable(setup=setup_at_market(broker)))
    m.run_cycle(NOW)
    assert broker.get_positions() == []  # no new entry after target


def test_target_disabled_keeps_trading() -> None:
    m, broker = build({"target_session_profit": 100, "target_enabled": False})
    force_reports(m, favourable(setup=setup_at_market(broker)))
    m.run_cycle(NOW)
    pos = broker.get_positions()[0]
    broker.set_mid("EURUSD", pos.take_profit + 0.0005, 0.6, NOW)
    m.run_cycle(NOW)
    assert not m.portfolio.target.reached


def test_broker_disconnection_enters_safety_mode_and_recovers() -> None:
    m, broker = build()
    broker.faults.disconnected = True
    report = m.run_cycle(NOW)
    assert m.safety.state.safety_mode and "broker disconnected" in report.blocking_reasons
    assert any(a["type"] == "broker_disconnected" for a in m.alerts.recent())
    broker.faults.disconnected = False
    broker.disconnect()
    m.run_cycle(NOW)
    assert not m.safety.state.safety_mode


def test_unknown_position_triggers_anomaly() -> None:
    m, broker = build()
    broker.inject_unknown_position("EURUSD", Direction.LONG, 10_000, 1.08, None)
    report = m.run_cycle(NOW)
    assert m.safety.state.safety_mode and not report.entries_allowed
    titles = [a["title"] for a in m.alerts.recent()]
    assert "Unknown position at broker" in titles and "Position without stop-loss" in titles


def test_restart_restores_state_and_reconciles() -> None:
    store = InMemoryStateStore()
    m, broker = build(store=store)
    force_reports(m, favourable(setup=setup_at_market(broker)))
    m.run_cycle(NOW)
    m.safety.activate_kill_switch("before restart", "admin")
    # "restart": new controller, same broker and store
    settings = m.settings
    m2 = MasterController(settings, broker, m.data, StaticCalendarProvider([], NOW), store=store)
    m2.restore()
    assert m2.safety.state.kill_switch
    assert len(m2.execution.positions.open_positions()) == 1
    m2.run_cycle(NOW)
    assert not m2.safety.state.safety_mode  # position known: no anomaly
    assert m2.execution.positions.open_positions()[0].strategy == "pullback"


def test_mode_must_match_broker_environment() -> None:
    m, broker = build()
    with pytest.raises(ConfigurationError):
        MasterController(Settings(_env_file=None, trading_mode="demo"), broker, m.data, StaticCalendarProvider([], NOW))
    broker.environment = "live"  # pretend: requirements must still block
    with pytest.raises(LiveModeNotAllowedError):
        MasterController(Settings(_env_file=None, trading_mode="live", live_trading_enabled=True), broker, m.data,
                         StaticCalendarProvider([], NOW))


def test_agents_run_on_real_pipeline_without_forcing() -> None:
    """No mocks: the agents analyse synthetic data; macro has no events; the cycle must not crash."""
    m, _ = build()
    report = m.run_cycle(NOW)
    assert not report.errors, report.errors
    assert set(m.latest_reports["EURUSD"]) == set(m.agents)
    assert m.agents_view() and m.status()["mode"] == "simulation"


def test_closed_trade_accounting_flows_to_portfolio() -> None:
    m, _ = build()
    t = ClosedTrade("T1", "EURUSD", Direction.LONG, 10_000, 1.1, 1.101, NOW, NOW, 10.0, -0.7, -0.1, 0.05, "take_profit")
    m._on_closed_trade(t, NOW)
    s = m.portfolio.snapshot()["session"]
    assert s["trades"] == 1 and s["net_realized"] == pytest.approx(9.2) and s["commissions"] == pytest.approx(-0.7)
    m._on_closed_trade(t, NOW)  # duplicates ignored
    assert m.portfolio.snapshot()["session"]["trades"] == 1
    _ = MarketContext  # imported for type completeness
