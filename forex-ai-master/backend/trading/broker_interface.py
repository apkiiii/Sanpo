"""Abstract broker interface.

Every broker connector must implement this contract. Requirements for
implementations (checked by the shared contract tests in
``backend/tests/test_broker_contract.py``):

* ``place_order`` is **idempotent** on ``client_order_id``: sending the same id
  twice must never open two positions (use the broker's client-id / client
  extensions feature, or query before re-sending).
* ``get_order_status`` returns ``None`` only if the broker never received the order.
* Connection problems raise :class:`BrokerConnectionError` / :class:`BrokerTimeoutError`;
  explicit refusals raise :class:`OrderRejectedError`.
* Prices and volumes are returned as floats in units (not lots).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from backend.core.instruments import InstrumentSpec
from backend.core.types import (
    AccountSnapshot,
    BrokerPosition,
    ClosedTrade,
    OrderRequest,
    OrderResult,
    Quote,
    Timeframe,
)


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    spec: InstrumentSpec
    tradeable: bool
    reason: str = ""


class BrokerInterface(ABC):
    """Contract between the trading engine and a broker connector."""

    #: "simulation", "practice" (demo) or "live"
    environment: str = "simulation"
    name: str = "abstract"

    @property
    def is_live(self) -> bool:
        return self.environment == "live"

    # --- session ---------------------------------------------------------
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    # --- account ---------------------------------------------------------
    @abstractmethod
    def get_account(self) -> AccountSnapshot: ...

    def get_account_balance(self) -> float:
        return self.get_account().balance

    def get_account_equity(self) -> float:
        return self.get_account().equity

    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]: ...

    # --- market data -----------------------------------------------------
    @abstractmethod
    def get_market_data(self, symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame: ...

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def get_symbol_info(self, symbol: str) -> SymbolInfo: ...

    @abstractmethod
    def calculate_margin(self, symbol: str, units: float) -> float: ...

    # --- orders ----------------------------------------------------------
    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def modify_order(self, position_id: str, stop_loss: float | None = None,
                     take_profit: float | None = None) -> bool:
        """Modify the protective orders attached to an open position."""

    @abstractmethod
    def close_position(self, position_id: str, units: float | None = None, reason: str = "") -> OrderResult: ...

    @abstractmethod
    def cancel_pending_orders(self) -> int:
        """Cancel all pending (not yet filled) orders. Returns the number cancelled."""

    @abstractmethod
    def get_order_status(self, client_order_id: str) -> OrderResult | None: ...

    @abstractmethod
    def get_transaction_history(self, since: datetime | None = None) -> list[ClosedTrade]: ...
