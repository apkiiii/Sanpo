"""Contract tests every BrokerInterface implementation must pass.

To validate a new connector, add a factory to ``CONNECTORS`` that returns a
*demo/practice* instance with at least one priced symbol (EURUSD).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from backend.core.errors import BrokerConnectionError, OrderRejectedError
from backend.core.types import Direction, OrderRequest, OrderSide, OrderStatus, Timeframe, new_id
from backend.trading.broker_interface import BrokerInterface
from backend.trading.simulated_broker import SimulatedBroker, SimulatedBrokerConfig
from backend.tests.conftest import NOW

pytestmark = pytest.mark.critical


def _simulated() -> BrokerInterface:
    b = SimulatedBroker(SimulatedBrokerConfig(slippage_pips=0.0))
    b.clock = NOW
    b.connect()
    b.set_mid("EURUSD", 1.10, 0.8, NOW)
    return b


CONNECTORS: dict[str, Callable[[], BrokerInterface]] = {"simulated": _simulated}


@pytest.fixture(params=list(CONNECTORS))
def broker(request: pytest.FixtureRequest) -> BrokerInterface:
    return CONNECTORS[request.param]()


def _req(b: BrokerInterface, cid: str | None = None, units: float = 10_000) -> OrderRequest:
    q = b.get_quote("EURUSD")
    return OrderRequest(cid or new_id("C"), "EURUSD", OrderSide.BUY, units, q.bid - 0.0030, q.ask + 0.0060)


def test_environment_declared(broker: BrokerInterface) -> None:
    assert broker.environment in ("simulation", "practice", "live")


def test_account_and_symbol_info(broker: BrokerInterface) -> None:
    acct = broker.get_account()
    assert acct.balance > 0 and acct.equity > 0 and acct.free_margin <= acct.equity
    info = broker.get_symbol_info("EURUSD")
    assert info.spec.pip_size == 0.0001
    assert broker.calculate_margin("EURUSD", 10_000) > 0


def test_place_is_idempotent_and_confirmed(broker: BrokerInterface) -> None:
    req = _req(broker)
    r1, r2 = broker.place_order(req), broker.place_order(req)
    assert r1.status is OrderStatus.FILLED and r1.position_id == r2.position_id
    assert broker.get_order_status(req.client_order_id) is not None
    assert broker.get_order_status("never-sent") is None
    positions = [p for p in broker.get_positions() if p.position_id == r1.position_id]
    assert len(positions) == 1 and positions[0].stop_loss is not None and positions[0].direction is Direction.LONG


def test_modify_and_close(broker: BrokerInterface) -> None:
    r = broker.place_order(_req(broker))
    assert r.position_id
    q = broker.get_quote("EURUSD")
    assert broker.modify_order(r.position_id, stop_loss=q.bid - 0.0010)
    closed = broker.close_position(r.position_id, reason="contract test")
    assert closed.status is OrderStatus.FILLED
    assert r.position_id in {t.trade_id for t in broker.get_transaction_history()}
    with pytest.raises(OrderRejectedError):
        broker.close_position(r.position_id)


def test_invalid_orders_rejected(broker: BrokerInterface) -> None:
    with pytest.raises(OrderRejectedError):
        broker.place_order(_req(broker, units=1))
    q = broker.get_quote("EURUSD")
    with pytest.raises(OrderRejectedError):
        broker.place_order(OrderRequest(new_id("C"), "EURUSD", OrderSide.BUY, 10_000, q.ask + 0.001, None))


def test_market_data(broker: BrokerInterface) -> None:
    if isinstance(broker, SimulatedBroker) and broker.data is None:
        with pytest.raises(BrokerConnectionError):
            broker.get_market_data("EURUSD", Timeframe.M15, 10)
    else:  # pragma: no cover - real connectors
        assert len(broker.get_market_data("EURUSD", Timeframe.M15, 10)) > 0
