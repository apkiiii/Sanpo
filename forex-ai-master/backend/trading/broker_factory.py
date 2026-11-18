"""Broker selection.

Only the simulated broker is implemented. Real connectors (DEMO / LIVE) are
intentionally **not** provided until a broker is chosen and its official API
documentation verified — see ``docs/BROKER_SETUP.md``. Asking for an unknown
broker fails loudly instead of silently falling back to simulation.
"""

from __future__ import annotations

from backend.config.settings import Settings, TradingMode
from backend.core.errors import ConfigurationError
from backend.data.market_data import MarketDataProvider
from backend.trading.broker_interface import BrokerInterface
from backend.trading.simulated_broker import SimulatedBroker, SimulatedBrokerConfig

IMPLEMENTED_CONNECTORS: dict[str, type[BrokerInterface]] = {}


def create_broker(settings: Settings, data: MarketDataProvider | None = None) -> BrokerInterface:
    if settings.trading_mode is TradingMode.SIMULATION:
        if settings.broker_name not in ("simulated", ""):
            raise ConfigurationError("SIMULATION mode always uses the simulated broker (set BROKER_NAME=simulated)")
        return SimulatedBroker(SimulatedBrokerConfig(initial_balance=settings.initial_balance,
                                                     account_ccy=settings.account_currency), data)
    connector = IMPLEMENTED_CONNECTORS.get(settings.broker_name.lower())
    if connector is None:
        raise ConfigurationError(
            f"No connector implemented for broker '{settings.broker_name}'. DEMO/LIVE require a verified connector "
            "built on the broker's official API. See docs/BROKER_SETUP.md.")
    return connector()  # pragma: no cover - no real connector shipped yet
