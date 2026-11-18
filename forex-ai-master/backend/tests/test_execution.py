"""Execution layer: order manager, duplicates, broker failures, reconciliation, position management."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from backend.core.errors import BrokerConnectionError, RiskViolationError, SafetyLockError
from backend.core.types import Direction, OrderRequest, OrderSide, OrderStatus, new_id
from backend.risk.risk_engine import RiskEngine
from backend.trading.order_manager import OrderManager
from backend.trading.position_manager import ManagedPosition, ManagementPolicy, PositionManager
from backend.trading.simulated_broker import SimulatedBroker
from backend.tests.conftest import NOW, make_proposal, make_setup, make_state

pytestmark = pytest.mark.critical


def approved(engine: RiskEngine, broker: SimulatedBroker, stop_pips: float = 20.0, direction=Direction.LONG):  # type: ignore[no-untyped-def]
    q = broker.get_quote("EURUSD")
    setup = make_setup(entry=q.ask if direction is Direction.LONG else q.bid, stop_pips=stop_pips, direction=direction)
    proposal = make_proposal(setup)
    decision = engine.evaluate(proposal, make_state())
    assert decision.approved, decision.reasons
    return proposal, decision


def om(engine: RiskEngine, broker: SimulatedBroker, blocked: bool = False, **kw) -> OrderManager:  # type: ignore[no-untyped-def]
    return OrderManager(broker, lambda ap: ap is not None and engine.verify_approval(ap, NOW), lambda: blocked,
                        retry_delay_s=0.0, **kw)


def test_order_requires_valid_approval(engine: RiskEngine, broker: SimulatedBroker) -> None:
    proposal, decision = approved(engine, broker)
    manager = om(engine, broker)
    decision.approval = replace(decision.approval, units=decision.approval.units * 10)  # tampered
    with pytest.raises(RiskViolationError):
        manager.submit(proposal, decision)
    assert broker.get_positions() == []


def test_order_without_approval_or_for_other_proposal(engine: RiskEngine, broker: SimulatedBroker) -> None:
    proposal, decision = approved(engine, broker)
    other = make_proposal(proposal.setup)
    with pytest.raises(RiskViolationError):
        om(engine, broker).submit(other, decision)
    decision.approved = False
    with pytest.raises(RiskViolationError):
        om(engine, broker).submit(proposal, decision)


def test_order_blocked_by_safety(engine: RiskEngine, broker: SimulatedBroker) -> None:
    proposal, decision = approved(engine, broker)
    with pytest.raises(SafetyLockError):
        om(engine, broker, blocked=True).submit(proposal, decision)


def test_successful_order_is_confirmed(engine: RiskEngine, broker: SimulatedBroker) -> None:
    proposal, decision = approved(engine, broker)
    order = om(engine, broker).submit(proposal, decision)
    assert order.status is OrderStatus.FILLED
    pos = broker.get_positions()
    assert len(pos) == 1 and pos[0].client_order_id == order.client_order_id
    assert pos[0].stop_loss == pytest.approx(proposal.setup.stop_loss)


def test_duplicate_submission_is_ignored(engine: RiskEngine, broker: SimulatedBroker) -> None:
    proposal, decision = approved(engine, broker)
    manager = om(engine, broker)
    a = manager.submit(proposal, decision)
    b = manager.submit(proposal, decision)
    assert a is b and len(broker.get_positions()) == 1


def test_broker_level_idempotency(broker: SimulatedBroker) -> None:
    q = broker.get_quote("EURUSD")
    req = OrderRequest("CID-1", "EURUSD", OrderSide.BUY, 10_000, q.bid - 0.002, q.ask + 0.004)
    r1 = broker.place_order(req)
    r2 = broker.place_order(req)
    assert r1.position_id == r2.position_id and len(broker.get_positions()) == 1


def test_timeout_after_execution_does_not_duplicate(engine: RiskEngine, broker: SimulatedBroker) -> None:
    """Response lost after the broker executed: status UNKNOWN, resolved by query, never re-sent blindly."""
    proposal, decision = approved(engine, broker)
    broker.faults.timeout_after_execution = 1
    order = om(engine, broker).submit(proposal, decision)
    assert order.status is OrderStatus.FILLED
    assert len(broker.get_positions()) == 1


def test_network_failure_before_execution_is_retried_safely(engine: RiskEngine, broker: SimulatedBroker) -> None:
    proposal, decision = approved(engine, broker)
    manager = om(engine, broker)
    # pre-trade checks succeed, then the first place_order and the status query fail
    original = broker.place_order
    calls = {"n": 0}

    def flaky(req):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise BrokerConnectionError("network")
        return original(req)

    broker.place_order = flaky  # type: ignore[method-assign]
    order = manager.submit(proposal, decision)
    assert order.status is OrderStatus.FILLED and order.attempts == 2
    assert len(broker.get_positions()) == 1


def test_rejected_order_is_not_retried(engine: RiskEngine, broker: SimulatedBroker) -> None:
    proposal, decision = approved(engine, broker)
    broker.faults.reject_next = 1
    order = om(engine, broker).submit(proposal, decision)
    assert order.status is OrderStatus.REJECTED and order.attempts == 1
    assert broker.get_positions() == []


def test_high_slippage_rejected_by_limit(engine: RiskEngine, broker: SimulatedBroker) -> None:
    proposal, decision = approved(engine, broker)
    broker.faults.extra_slippage_pips = 5.0
    order = om(engine, broker, max_slippage_pips=2.0).submit(proposal, decision)
    assert order.status is OrderStatus.REJECTED and "slippage" in order.error
    assert broker.get_positions() == []


def test_broker_unavailable(engine: RiskEngine, broker: SimulatedBroker) -> None:
    proposal, decision = approved(engine, broker)
    broker.faults.disconnected = True
    with pytest.raises(BrokerConnectionError):
        om(engine, broker).submit(proposal, decision)


def test_unresolved_when_broker_stays_down(engine: RiskEngine, broker: SimulatedBroker) -> None:
    proposal, decision = approved(engine, broker)
    manager = om(engine, broker, max_attempts=2)
    original_ensure = broker._ensure
    state = {"checks_done": False}

    def ensure() -> None:
        if state["checks_done"]:
            raise BrokerConnectionError("down")
        original_ensure()

    def place(req):  # type: ignore[no-untyped-def]
        state["checks_done"] = True
        raise BrokerConnectionError("down")

    broker._ensure = ensure  # type: ignore[method-assign]
    broker.place_order = place  # type: ignore[method-assign]
    order = manager.submit(proposal, decision)
    assert order.status is OrderStatus.UNKNOWN and manager.unresolved() == [order]


def test_insufficient_margin(engine: RiskEngine, broker: SimulatedBroker) -> None:
    proposal, decision = approved(engine, broker)
    broker._balance = 100.0
    order_manager = om(engine, broker)
    from backend.core.errors import OrderRejectedError
    with pytest.raises(OrderRejectedError):
        order_manager.submit(proposal, decision)


# ------------------------------------------------------------------ positions
def _open(broker: SimulatedBroker, pm: PositionManager, direction: Direction = Direction.LONG, stop_pips: float = 20):
    q = broker.get_quote("EURUSD")
    d = direction.sign
    entry = q.ask if d > 0 else q.bid
    sl, tp = entry - d * stop_pips * 1e-4, entry + d * 2 * stop_pips * 1e-4
    res = broker.place_order(OrderRequest(new_id("CID"), "EURUSD",
                                          OrderSide.BUY if d > 0 else OrderSide.SELL, 10_000, sl, tp))
    pm.register(ManagedPosition(res.position_id, "EURUSD", direction, 10_000, res.fill_price, sl, tp, sl, 20.0, 40.0,
                                2.0, NOW, strategy="t", invalidation=sl))
    return res.position_id


def test_stop_loss_and_take_profit_execution(broker: SimulatedBroker) -> None:
    pm = PositionManager(broker)
    pid = _open(broker, pm)
    broker.set_mid("EURUSD", 1.0950, 0.8, NOW)
    rep = pm.reconcile()
    assert pid not in pm.positions and rep.closed_trades[0].exit_reason == "stop_loss"
    assert rep.closed_trades[0].net_pnl < 0
    broker.set_mid("EURUSD", 1.10, 0.8, NOW)
    pid2 = _open(broker, pm)
    broker.set_mid("EURUSD", 1.1050, 0.8, NOW)
    rep2 = pm.reconcile()
    assert rep2.closed_trades[0].trade_id == pid2 and rep2.closed_trades[0].exit_reason == "take_profit"


def test_stop_can_never_be_widened(broker: SimulatedBroker) -> None:
    pm = PositionManager(broker)
    pid = _open(broker, pm)
    p = pm.positions[pid]
    with pytest.raises(RiskViolationError):
        pm.modify_stop(pid, p.stop_loss - 0.0010, broker.get_quote("EURUSD"), "test")
    assert pm.modify_stop(pid, p.stop_loss + 0.0005, broker.get_quote("EURUSD"), "tighten")
    with pytest.raises(RiskViolationError):  # beyond current price
        pm.modify_stop(pid, 1.2, broker.get_quote("EURUSD"), "bad")


def test_short_stop_never_widened(broker: SimulatedBroker) -> None:
    pm = PositionManager(broker)
    pid = _open(broker, pm, Direction.SHORT)
    with pytest.raises(RiskViolationError):
        pm.modify_stop(pid, pm.positions[pid].stop_loss + 0.001, broker.get_quote("EURUSD"), "x")


def test_breakeven_and_trailing(broker: SimulatedBroker) -> None:
    pm = PositionManager(broker, ManagementPolicy(trailing_enabled=True, trailing_atr_multiple=1.0))
    pid = _open(broker, pm)
    entry = pm.positions[pid].entry_price
    broker.set_mid("EURUSD", entry + 0.0025, 0.8, NOW)  # > +1R
    actions = pm.manage({"EURUSD": broker.get_quote("EURUSD")}, {"EURUSD": 0.0005})
    p = pm.positions[pid]
    assert p.breakeven_done and p.stop_loss >= entry
    assert any("trailing" in a for a in actions) or any("break-even" in a for a in actions)
    assert broker.get_positions()[0].stop_loss == pytest.approx(p.stop_loss)


def test_invalidation_exit(broker: SimulatedBroker) -> None:
    pm = PositionManager(broker)
    pid = _open(broker, pm)
    pm.positions[pid].invalidation = pm.positions[pid].entry_price - 0.0005  # tighter than the stop
    actions = pm.manage({"EURUSD": broker.get_quote("EURUSD")}, {}, {"EURUSD": pm.positions[pid].entry_price - 0.0008})
    assert any("invalidation" in a for a in actions)
    assert broker.get_positions() == []


def test_reconciliation_restores_missing_stop(broker: SimulatedBroker) -> None:
    pm = PositionManager(broker)
    pid = _open(broker, pm)
    broker._positions[pid].pos.stop_loss = None  # broker lost the stop
    rep = pm.reconcile()
    assert rep.mismatches and rep.fixed
    assert broker.get_positions()[0].stop_loss == pytest.approx(pm.positions[pid].stop_loss)


def test_reconciliation_unknown_and_missing(broker: SimulatedBroker) -> None:
    pm = PositionManager(broker)
    unknown = broker.inject_unknown_position("EURUSD", Direction.SHORT, 5_000, 1.1)
    pid = _open(broker, pm)
    del broker._positions[pid]  # vanished without a transaction
    rep = pm.reconcile()
    assert [p.position_id for p in rep.unknown_at_broker] == [unknown]
    assert rep.missing_at_broker == [pid]
    assert not pm.positions[unknown].managed


def test_swap_and_commissions_accounted(broker: SimulatedBroker) -> None:
    pm = PositionManager(broker)
    pid = _open(broker, pm)
    broker.apply_swap(3)
    pm.close(pid, "manual")
    trade = broker.get_transaction_history()[-1]
    assert trade.commission < 0 and trade.swap < 0
    assert trade.net_pnl == pytest.approx(trade.gross_pnl + trade.commission + trade.swap)


def test_api_slow_does_not_block_approval_expiry(engine: RiskEngine, broker: SimulatedBroker) -> None:
    """An approval used after its TTL (slow pipeline / API) is refused."""
    proposal, decision = approved(engine, broker)
    manager = OrderManager(broker, lambda ap: engine.verify_approval(ap, NOW + timedelta(minutes=2)), lambda: False)
    with pytest.raises(RiskViolationError):
        manager.submit(proposal, decision)


def test_drifted_but_approved_trade_executes(engine: RiskEngine, broker: SimulatedBroker) -> None:
    """Regression: a trade approved with an allowed entry drift must not be refused for 'slippage'."""
    q = broker.get_quote("EURUSD")
    setup = make_setup(entry=q.ask - 0.0005, stop_pips=30)  # planned 5 pips below the current ask
    proposal = make_proposal(setup)
    decision = engine.evaluate(proposal, make_state())
    assert decision.approved, decision.reasons
    order = om(engine, broker).submit(proposal, decision)
    assert order.status is OrderStatus.FILLED, order.error
    assert abs(order.slippage_pips or 0) < 0.5  # measured against the executable price


def test_realised_risk_never_exceeds_limit_even_with_max_slippage(engine: RiskEngine, broker: SimulatedBroker) -> None:
    proposal, decision = approved(engine, broker)
    broker.faults.extra_slippage_pips = 1.99  # just under the 2-pip tolerance
    order = om(engine, broker).submit(proposal, decision)
    assert order.status is OrderStatus.FILLED
    pos = broker.get_positions()[0]
    realised_risk = (pos.entry_price - pos.stop_loss) * pos.units  # EURUSD: quote = USD
    assert realised_risk <= 100_000 * 0.0025 + 1e-6


def test_stop_rounding_never_widens_and_matches_broker(engine: RiskEngine, broker: SimulatedBroker) -> None:
    from backend.core.instruments import get_instrument
    spec = get_instrument("EURUSD")
    assert spec.round_stop(1.0979949, 1) == 1.09800 and spec.round_stop(1.1020051, -1) == 1.10200
    assert spec.round_stop(1.09800, 1) == 1.09800  # already on the grid: unchanged
    for direction in (Direction.LONG, Direction.SHORT):
        q = broker.get_quote("EURUSD")
        entry = q.ask if direction is Direction.LONG else q.bid
        setup = replace(make_setup(entry=entry, direction=direction, stop_pips=20),
                        stop_loss=entry - direction.sign * 0.00200437)  # off-grid stop
        proposal = make_proposal(setup)
        decision = engine.evaluate(proposal, make_state())
        order = om(engine, broker).submit(proposal, decision)
        assert order.status is OrderStatus.FILLED
        sent = order.request.stop_loss
        assert (sent - setup.stop_loss) * direction.sign >= 0  # tighter or equal, never wider
        broker._positions[order.result.position_id].pos.symbol  # exists
    from backend.trading.position_manager import PositionManager as PM
    pm = PM(broker)
    rep = pm.reconcile()
    assert not rep.mismatches
