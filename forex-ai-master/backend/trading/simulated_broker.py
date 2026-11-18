"""Paper-trading broker used in SIMULATION mode and in tests.

It fills orders on the current quote with spread, commission and slippage,
triggers stop-loss / take-profit on each price update, charges swaps at the
rollover and supports fault injection (disconnection, timeouts after execution,
rejections, abnormal spreads) to test the execution layer.

It never contacts any external service.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from backend.core.errors import BrokerConnectionError, BrokerTimeoutError, OrderRejectedError
from backend.core.instruments import get_instrument, margin_required, normalize_symbol, price_move_value
from backend.core.market_hours import is_market_open
from backend.core.types import (
    AccountSnapshot,
    BrokerPosition,
    ClosedTrade,
    Direction,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    Quote,
    Timeframe,
    new_id,
    utcnow,
)
from backend.data.market_data import MarketDataProvider
from backend.trading.broker_interface import BrokerInterface, SymbolInfo


@dataclass(slots=True)
class FaultInjection:
    disconnected: bool = False
    fail_next_requests: int = 0  # raise BrokerConnectionError before execution
    timeout_after_execution: int = 0  # execute, then raise BrokerTimeoutError (status unknown)
    reject_next: int = 0
    reject_reason: str = "insufficient liquidity"
    extra_slippage_pips: float = 0.0
    ignore_market_hours: bool = True
    modify_fails: bool = False


@dataclass(slots=True)
class _SimPosition:
    pos: BrokerPosition
    commission_paid: float = 0.0
    swap: float = 0.0
    slippage_cost: float = 0.0
    strategy: str = ""
    last_swap_check: datetime | None = None


@dataclass(slots=True)
class SimulatedBrokerConfig:
    initial_balance: float = 100_000.0
    account_ccy: str = "USD"
    commission_per_lot_side: float = 3.5
    slippage_pips: float = 0.1
    swap_pips_per_night: float = -0.6
    seed: int = 3


class SimulatedBroker(BrokerInterface):
    environment = "simulation"
    name = "simulated"

    def __init__(self, config: SimulatedBrokerConfig | None = None, data: MarketDataProvider | None = None) -> None:
        self.config = config or SimulatedBrokerConfig()
        self.data = data
        self.faults = FaultInjection()
        self._connected = False
        self._balance = self.config.initial_balance
        self._quotes: dict[str, Quote] = {}
        self._positions: dict[str, _SimPosition] = {}
        self._orders: dict[str, OrderResult] = {}
        self._closed: list[ClosedTrade] = []
        self._rng = np.random.default_rng(self.config.seed)
        self._lock = threading.RLock()
        self.clock: datetime | None = None  # simulated time; None = wall clock
        self.request_count = 0

    # ------------------------------------------------------------------ utils
    def _now(self) -> datetime:
        return self.clock or utcnow()

    def _ensure(self) -> None:
        self.request_count += 1
        if self.faults.disconnected or not self._connected:
            raise BrokerConnectionError("simulated broker disconnected")
        if self.faults.fail_next_requests > 0:
            self.faults.fail_next_requests -= 1
            raise BrokerConnectionError("simulated network failure")

    def _mids(self) -> dict[str, float]:
        return {s: q.mid for s, q in self._quotes.items()}

    def set_quote(self, symbol: str, bid: float, ask: float, time: datetime | None = None) -> None:
        """Push a new price (simulation feed) and process stops/targets."""
        symbol = normalize_symbol(symbol)
        with self._lock:
            self._quotes[symbol] = Quote(symbol, bid, ask, time or self._now())
            self._process_triggers(symbol)

    def set_mid(self, symbol: str, mid: float, spread_pips: float = 1.0, time: datetime | None = None) -> None:
        half = spread_pips * get_instrument(symbol).pip_size / 2
        self.set_quote(symbol, mid - half, mid + half, time)

    # ------------------------------------------------------------------ session
    def connect(self) -> None:
        if self.faults.disconnected:
            raise BrokerConnectionError("simulated broker unreachable")
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and not self.faults.disconnected

    # ------------------------------------------------------------------ account
    def _unrealized(self, sp: _SimPosition) -> float:
        q = self._quotes.get(sp.pos.symbol)
        if q is None:
            return 0.0
        d = sp.pos.direction.sign
        exit_px = q.bid if d > 0 else q.ask
        return price_move_value(sp.pos.symbol, (exit_px - sp.pos.entry_price) * d, sp.pos.units,
                                self.config.account_ccy, self._mids() | {sp.pos.symbol: exit_px})

    def get_account(self) -> AccountSnapshot:
        self._ensure()
        with self._lock:
            unreal = sum(self._unrealized(p) + p.swap - 0.0 for p in self._positions.values())
            equity = self._balance + unreal
            margin = sum(margin_required(p.pos.symbol, p.pos.units, self.config.account_ccy, self._mids())
                         for p in self._positions.values())
            return AccountSnapshot(self._balance, equity, margin, equity - margin, self.config.account_ccy, self._now())

    def get_positions(self) -> list[BrokerPosition]:
        self._ensure()
        with self._lock:
            out = []
            for sp in self._positions.values():
                sp.pos.unrealized_pnl = self._unrealized(sp)
                sp.pos.swap = sp.swap
                out.append(BrokerPosition(**{k: getattr(sp.pos, k) for k in sp.pos.__slots__}))
            return out

    # ------------------------------------------------------------------ market data
    def get_market_data(self, symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame:
        self._ensure()
        if self.data is None:
            raise BrokerConnectionError("no market data source attached to simulated broker")
        return self.data.get_candles(symbol, timeframe, count)

    def get_quote(self, symbol: str) -> Quote:
        self._ensure()
        symbol = normalize_symbol(symbol)
        if symbol not in self._quotes:
            if self.data is None:
                raise BrokerConnectionError(f"no quote for {symbol}")
            self._quotes[symbol] = self.data.get_quote(symbol)
        return self._quotes[symbol]

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        self._ensure()
        spec = get_instrument(symbol)
        open_ = self.faults.ignore_market_hours or is_market_open(self._now())
        return SymbolInfo(spec, open_, "" if open_ else "market closed")

    def calculate_margin(self, symbol: str, units: float) -> float:
        return margin_required(symbol, units, self.config.account_ccy, self._mids())

    # ------------------------------------------------------------------ orders
    def place_order(self, request: OrderRequest) -> OrderResult:
        self._ensure()
        with self._lock:
            if request.client_order_id in self._orders:  # idempotency
                return self._orders[request.client_order_id]
            if self.faults.reject_next > 0:
                self.faults.reject_next -= 1
                res = OrderResult(request.client_order_id, OrderStatus.REJECTED, reason=self.faults.reject_reason)
                self._orders[request.client_order_id] = res
                raise OrderRejectedError(self.faults.reject_reason)
            symbol = normalize_symbol(request.symbol)
            q = self._quotes.get(symbol)
            if q is None:
                raise OrderRejectedError(f"no price for {symbol}")
            spec = get_instrument(symbol)
            if request.units < spec.min_units or request.units % spec.units_step:
                raise OrderRejectedError("invalid units")
            d = Direction.LONG if request.side is OrderSide.BUY else Direction.SHORT
            slip = (abs(self._rng.normal(0, self.config.slippage_pips)) + self.faults.extra_slippage_pips) * spec.pip_size
            fill = (q.ask + slip) if d is Direction.LONG else (q.bid - slip)
            if request.max_slippage_price is not None and (fill - request.max_slippage_price) * d.sign > 0:
                res = OrderResult(request.client_order_id, OrderStatus.REJECTED, reason="slippage limit exceeded")
                self._orders[request.client_order_id] = res
                raise OrderRejectedError("price moved beyond slippage limit")
            if (d is Direction.LONG and request.stop_loss >= fill) or (d is Direction.SHORT and request.stop_loss <= fill):
                raise OrderRejectedError("stop-loss on wrong side of fill price")
            commission = -request.units / 100_000 * self.config.commission_per_lot_side
            pos_id = new_id("POS")
            pos = BrokerPosition(pos_id, symbol, d, request.units, fill, request.stop_loss, request.take_profit, 0.0,
                                 0.0, commission, request.client_order_id, self._now())
            self._positions[pos_id] = _SimPosition(pos, commission, 0.0,
                                                   price_move_value(symbol, slip, request.units,
                                                                    self.config.account_ccy, self._mids()),
                                                   request.comment, self._now())
            self._balance += commission
            res = OrderResult(request.client_order_id, OrderStatus.FILLED, new_id("ORD"), pos_id, request.units,
                              fill, commission, "", self._now())
            self._orders[request.client_order_id] = res
            if self.faults.timeout_after_execution > 0:
                self.faults.timeout_after_execution -= 1
                raise BrokerTimeoutError("response lost after execution")
            return res

    def modify_order(self, position_id: str, stop_loss: float | None = None, take_profit: float | None = None) -> bool:
        self._ensure()
        if self.faults.modify_fails:
            raise BrokerConnectionError("modify failed")
        with self._lock:
            sp = self._positions.get(position_id)
            if sp is None:
                return False
            if stop_loss is not None:
                sp.pos.stop_loss = stop_loss
            if take_profit is not None:
                sp.pos.take_profit = take_profit
            self._process_triggers(sp.pos.symbol)
            return True

    def close_position(self, position_id: str, units: float | None = None, reason: str = "manual") -> OrderResult:
        self._ensure()
        with self._lock:
            sp = self._positions.get(position_id)
            if sp is None:
                raise OrderRejectedError(f"unknown position {position_id}")
            q = self._quotes[sp.pos.symbol]
            px = q.bid if sp.pos.direction is Direction.LONG else q.ask
            trade = self._close(sp, px, reason)
            return OrderResult(new_id("CLOSE"), OrderStatus.FILLED, new_id("ORD"), position_id, sp.pos.units, px,
                               trade.commission, reason, self._now())

    def cancel_pending_orders(self) -> int:
        self._ensure()
        return 0  # market orders only: nothing rests on the book

    def get_order_status(self, client_order_id: str) -> OrderResult | None:
        self._ensure()
        return self._orders.get(client_order_id)

    def get_transaction_history(self, since: datetime | None = None) -> list[ClosedTrade]:
        self._ensure()
        return [t for t in self._closed if since is None or t.exit_time >= since]

    # ------------------------------------------------------------------ internals
    def _close(self, sp: _SimPosition, price: float, reason: str) -> ClosedTrade:
        p = sp.pos
        d = p.direction.sign
        gross = price_move_value(p.symbol, (price - p.entry_price) * d, p.units, self.config.account_ccy,
                                 self._mids() | {p.symbol: price})
        exit_comm = -p.units / 100_000 * self.config.commission_per_lot_side
        self._balance += gross + exit_comm + sp.swap
        trade = ClosedTrade(p.position_id, p.symbol, p.direction, p.units, p.entry_price, price, p.open_time,
                            self._now(), gross, sp.commission_paid + exit_comm, sp.swap, sp.slippage_cost, reason,
                            sp.strategy)
        self._closed.append(trade)
        del self._positions[p.position_id]
        return trade

    def _process_triggers(self, symbol: str) -> None:
        q = self._quotes.get(symbol)
        if q is None:
            return
        for sp in [p for p in self._positions.values() if p.pos.symbol == symbol]:
            p = sp.pos
            if p.direction is Direction.LONG:
                if p.stop_loss is not None and q.bid <= p.stop_loss:
                    self._close(sp, min(q.bid, p.stop_loss), "stop_loss")
                elif p.take_profit is not None and q.bid >= p.take_profit:
                    self._close(sp, p.take_profit, "take_profit")
            else:
                if p.stop_loss is not None and q.ask >= p.stop_loss:
                    self._close(sp, max(q.ask, p.stop_loss), "stop_loss")
                elif p.take_profit is not None and q.ask <= p.take_profit:
                    self._close(sp, p.take_profit, "take_profit")

    def apply_swap(self, nights: int = 1) -> None:
        """Charge ``nights`` rollovers to all open positions (simulation helper)."""
        with self._lock:
            for sp in self._positions.values():
                spec = get_instrument(sp.pos.symbol)
                sp.swap += nights * price_move_value(sp.pos.symbol, self.config.swap_pips_per_night * spec.pip_size,
                                                     sp.pos.units, self.config.account_ccy, self._mids())

    # Test helper: create a position the local system does not know about.
    def inject_unknown_position(self, symbol: str, direction: Direction, units: float, entry: float,
                                stop_loss: float | None = None) -> str:
        pos_id = new_id("POS")
        self._positions[pos_id] = _SimPosition(BrokerPosition(pos_id, symbol, direction, units, entry, stop_loss, None,
                                                              0.0), 0.0, 0.0, 0.0, "", self._now())
        return pos_id
