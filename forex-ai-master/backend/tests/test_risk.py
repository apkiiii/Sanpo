"""Risk engine, position sizing and safety manager — CRITICAL tests."""

from datetime import timedelta

import pytest

from backend.core.types import Direction, Quote
from backend.risk.position_sizing import size_position
from backend.risk.risk_engine import OpenRisk, RiskApproval, RiskEngine
from backend.risk.safety_manager import SafetyManager
from backend.tests.conftest import MIDS, NOW, make_proposal, make_setup, make_state

pytestmark = pytest.mark.critical


# ---------------------------------------------------------------- sizing
def test_position_size_eurusd() -> None:
    # 0.25 % of 100k = 250 USD ; 20 pips stop ; pip value per unit 0.0001 -> 125,000 units
    r = size_position(100_000, 100_000, 0.25, 1.1000, 1.0980, "EURUSD", "USD", MIDS)
    assert r.units == 125_000 and r.risk_amount == pytest.approx(250.0)  # raw sizing (no slippage buffer)


def test_position_size_usdjpy_converts_quote() -> None:
    r = size_position(100_000, 100_000, 0.25, 150.00, 149.80, "USDJPY", "USD", MIDS)
    # loss per unit = 0.20 JPY = 0.2/150 USD -> 250 / 0.001333 = 187,500
    assert r.units == 187_000  # rounded DOWN to 1,000-unit steps
    assert r.risk_amount <= 250.0


def test_position_size_rounds_down_and_uses_min_capital() -> None:
    r = size_position(100_000, 90_000, 0.25, 1.1000, 1.0973, "EURUSD", "USD", MIDS)
    assert r.risk_amount <= 90_000 * 0.0025 + 1e-9
    tiny = size_position(1_000, 1_000, 0.25, 1.1000, 1.0900, "EURUSD", "USD", MIDS)
    assert tiny.units == 0 and "below minimum" in tiny.reason
    assert size_position(1000, 1000, 0.25, 1.1, 1.1, "EURUSD", "USD", MIDS).units == 0


# ---------------------------------------------------------------- approval
def test_valid_trade_is_approved_and_signed(engine: RiskEngine) -> None:
    d = engine.evaluate(make_proposal(), make_state())
    assert d.approved, d.reasons
    assert d.approval is not None and engine.verify_approval(d.approval, NOW)
    assert d.risk_pct <= 0.25 + 1e-9


def test_forged_or_expired_approval_rejected(engine: RiskEngine) -> None:
    d = engine.evaluate(make_proposal(), make_state())
    assert d.approval is not None
    forged = RiskApproval(d.approval.proposal_id, "EURUSD", "long", d.approval.units * 10, d.approval.stop_loss,
                          d.approval.expires_at, d.approval.signature)
    assert not engine.verify_approval(forged, NOW)
    assert not engine.verify_approval(d.approval, NOW + timedelta(minutes=5))
    other = RiskEngine(engine.limits)
    assert not other.verify_approval(d.approval, NOW)


# ---------------------------------------------------------------- rules
@pytest.mark.parametrize("setup_kw,check", [
    ({"stop_pips": 0.0}, "stop_loss"),
    ({"stop_pips": 3.0}, "min_stop"),
    ({"rr": 1.0}, "reward_risk"),
])
def test_invalid_setups_rejected(engine: RiskEngine, setup_kw: dict, check: str) -> None:
    d = engine.evaluate(make_proposal(make_setup(**setup_kw)), make_state())
    assert not d.approved and not d.checks[check][0]


def test_wrong_side_stop_rejected(engine: RiskEngine) -> None:
    s = make_setup()
    from dataclasses import replace
    bad = replace(s, stop_loss=s.entry + 0.0020)
    d = engine.evaluate(make_proposal(bad), make_state())
    assert not d.approved and not d.checks["stop_loss"][0]


def test_abnormal_spread_rejected(engine: RiskEngine) -> None:
    d = engine.evaluate(make_proposal(), make_state(spread_pips=8.0))
    assert not d.approved and not d.checks["spread"][0]


def test_stale_quote_rejected(engine: RiskEngine) -> None:
    st = make_state()
    st.quote = Quote("EURUSD", 1.0999, 1.1001, NOW - timedelta(minutes=5))
    assert not engine.evaluate(make_proposal(), st).approved


def test_extreme_volatility_rejected(engine: RiskEngine) -> None:
    assert not engine.evaluate(make_proposal(), make_state(atr_spike_ratio=5.0)).checks["volatility"][0]
    assert not engine.evaluate(make_proposal(), make_state(atr_spike_ratio=None)).approved


def test_daily_loss_limit(engine: RiskEngine) -> None:
    st = make_state(daily_pnl=-1_000.0)  # -1 % of 100k
    d = engine.evaluate(make_proposal(), st)
    assert not d.approved and not d.checks["daily_loss"][0]
    # projected: -0.9 % already + 0.25 % new risk > 1 %
    d2 = engine.evaluate(make_proposal(), make_state(daily_pnl=-900.0))
    assert not d2.approved and not d2.checks["daily_loss_projected"][0]


def test_weekly_loss_limit(engine: RiskEngine) -> None:
    assert not engine.evaluate(make_proposal(), make_state(weekly_pnl=-2_600.0)).checks["weekly_loss"][0]


def test_drawdown_limit_triggers_safety(engine: RiskEngine) -> None:
    d = engine.evaluate(make_proposal(), make_state(balance=95_000, peak_equity=100_000.0, day_start_equity=95_000,
                                                    week_start_equity=95_000, daily_pnl=0.0, weekly_pnl=0.0))
    assert not d.approved and d.trigger_safety_mode


def test_max_open_positions(engine: RiskEngine) -> None:
    opens = [OpenRisk(s, Direction.LONG, 1000, 1.0, 0.99, 10.0) for s in ("GBPUSD", "USDJPY", "AUDUSD")]
    d = engine.evaluate(make_proposal(), make_state(open_positions=opens))
    assert not d.checks["max_positions"][0]


def test_total_open_risk_and_currency_exposure(engine: RiskEngine) -> None:
    # Two existing long-USD-ish positions each risking 0.25 %
    opens = [OpenRisk("USDJPY", Direction.LONG, 1000, 150, 149.8, 250.0),
             OpenRisk("USDCHF", Direction.LONG, 1000, 0.88, 0.878, 250.0)]
    # Short EURUSD = long USD: USD exposure would reach 0.75 % > 0.5 %
    d = engine.evaluate(make_proposal(make_setup(direction=Direction.SHORT)), make_state(open_positions=opens))
    assert not d.checks["currency_exposure"][0]
    # Long EURUSD hedges USD: exposure fine, total risk = 0.75 % OK
    d2 = engine.evaluate(make_proposal(make_setup(direction=Direction.LONG)), make_state(open_positions=opens))
    assert d2.checks["currency_exposure"][0] and d2.checks["total_open_risk"][0]


def test_correlation_check(engine: RiskEngine) -> None:
    opens = [OpenRisk("GBPUSD", Direction.LONG, 1000, 1.27, 1.268, 50.0)]
    st = make_state(open_positions=opens, correlations={("EURUSD", "GBPUSD"): 0.9})
    assert not engine.evaluate(make_proposal(), st).checks["correlation"][0]
    st2 = make_state(open_positions=opens, correlations={("EURUSD", "GBPUSD"): 0.5})
    assert engine.evaluate(make_proposal(), st2).checks["correlation"][0]


def test_duplicate_symbol_rejected(engine: RiskEngine) -> None:
    opens = [OpenRisk("EURUSD", Direction.LONG, 1000, 1.1, 1.098, 10.0)]
    assert not engine.evaluate(make_proposal(), make_state(open_positions=opens)).checks["duplicate_symbol"][0]


def test_session_target_blocks_entries(engine: RiskEngine) -> None:
    assert not engine.evaluate(make_proposal(), make_state(session_target_reached=True)).approved


def test_kill_switch_and_safety_block(engine: RiskEngine) -> None:
    assert not engine.evaluate(make_proposal(), make_state(kill_switch=True)).approved
    assert not engine.evaluate(make_proposal(), make_state(safety_mode=True)).approved
    assert not engine.evaluate(make_proposal(), make_state(market_open=False)).approved


def test_margin_check(engine: RiskEngine) -> None:
    from backend.core.types import AccountSnapshot
    st = make_state(account=AccountSnapshot(100_000, 100_000, 29_000, 71_000))
    assert not engine.evaluate(make_proposal(), st).checks["margin"][0]


def test_no_martingale_exceptional_risk(engine: RiskEngine) -> None:
    ok = engine.evaluate(make_proposal(exceptional=True), make_state())
    assert ok.approved and ok.risk_pct <= 0.50 + 1e-9 and ok.risk_pct > 0.25
    after_loss = engine.evaluate(make_proposal(exceptional=True), make_state(consecutive_losses=1))
    assert after_loss.risk_pct <= 0.25 + 1e-9  # never increased after a loss
    losing_day = engine.evaluate(make_proposal(exceptional=True), make_state(daily_pnl=-100.0))
    assert losing_day.risk_pct <= 0.25 + 1e-9


def test_risk_never_depends_on_target_gap(engine: RiskEngine) -> None:
    """Sizing is identical whatever the session P&L (no 'catch-up' behaviour)."""
    a = engine.evaluate(make_proposal(), make_state(daily_pnl=0.0))
    b = engine.evaluate(make_proposal(), make_state(daily_pnl=-500.0))
    assert a.units >= b.units  # same or smaller, never larger after losses


def test_entry_drift_rejected(engine: RiskEngine) -> None:
    s = make_setup(entry=1.1050)  # quote is at 1.1000: 50 pips away
    assert not engine.evaluate(make_proposal(s), make_state()).checks["entry_drift"][0]


# ---------------------------------------------------------------- safety manager
def test_kill_switch_lifecycle() -> None:
    calls: list[str] = []
    sm = SafetyManager()
    sm.on_kill_switch(lambda reason, actor: calls.append(reason))
    sm.activate_kill_switch("manual stop", "operator")
    sm.activate_kill_switch("again", "operator")  # idempotent callbacks
    assert sm.entries_blocked and calls == ["manual stop"]
    with pytest.raises(PermissionError):
        sm.deactivate_kill_switch("system", True)
    with pytest.raises(PermissionError):
        sm.deactivate_kill_switch("operator", False)
    sm.deactivate_kill_switch("operator", True)
    assert not sm.entries_blocked


def test_consecutive_errors_trigger_safety_mode() -> None:
    sm = SafetyManager(max_consecutive_errors=3)
    sm.record_error("e1")
    sm.record_success()
    sm.record_error("e1")
    sm.record_error("e2")
    assert not sm.state.safety_mode
    sm.record_error("e3")
    assert sm.state.safety_mode


def test_safety_restore_never_clears() -> None:
    sm = SafetyManager()
    sm.restore({"kill_switch": True, "kill_switch_reason": "x", "safety_reasons": ["broker disconnected"]})
    assert sm.state.kill_switch and sm.state.safety_mode
    sm.clear_safety_reason("broker disconnected", "system")
    assert not sm.state.safety_mode and sm.state.kill_switch


def test_sizing_uses_executable_price_after_drift(engine: RiskEngine) -> None:
    """Planned entry 1.0995 but the ask is now 1.1001: the real stop distance (26 pips) drives the size."""
    from backend.core.types import TradeSetup
    s = TradeSetup("EURUSD", Direction.LONG, 1.0995, 1.0975, 1.1055, 1.0975, "t", "M15", 0.6)
    d = engine.evaluate(make_proposal(s), make_state())  # quote ask = 1.1000 + 0.4 pip
    assert d.approved, d.reasons
    real_risk = d.units * (1.10004 - 1.0975)
    assert real_risk <= 250.0 + 1e-6  # never above 0.25 % of 100k at the real fill price
    assert d.units < 125_000  # smaller than the 20-pip sizing based on the planned entry


def test_reward_risk_rechecked_at_current_price(engine: RiskEngine) -> None:
    from backend.core.types import TradeSetup
    # planned RR 2.0 from 1.0990, but at the current ask (~1.1000) RR falls to ~1.33 -> refused
    s = TradeSetup("EURUSD", Direction.LONG, 1.0990, 1.0960, 1.1050, 1.0960, "t", "M15", 0.6)
    d = engine.evaluate(make_proposal(s), make_state())
    assert not d.checks["reward_risk"][0]
