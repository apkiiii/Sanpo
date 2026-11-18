"""Order manager: the only path from an approved proposal to a broker order.

Guarantees:
* no order without a valid, unexpired, matching :class:`RiskApproval`;
* one proposal -> one deterministic ``client_order_id`` (idempotent across retries
  and restarts) and no second submission for the same proposal;
* after a timeout/disconnect the status is UNKNOWN and is resolved by querying the
  broker *before* any retry;
* an order is considered executed only after the broker reports it FILLED and the
  position is visible in the broker's position list;
* explicit rejections are never retried.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from backend.core.errors import (
    BrokerConnectionError,
    OrderRejectedError,
    RiskViolationError,
    SafetyLockError,
)
from backend.core.instruments import get_instrument
from backend.core.logging import get_logger
from backend.core.types import Direction, OrderRequest, OrderResult, OrderSide, OrderStatus, TradeProposal, utcnow
from backend.risk.risk_engine import RiskApproval, RiskDecision
from backend.trading.broker_interface import BrokerInterface

log = get_logger(__name__)


@dataclass(slots=True)
class ManagedOrder:
    client_order_id: str
    proposal: TradeProposal
    request: OrderRequest
    status: OrderStatus = OrderStatus.PENDING
    result: OrderResult | None = None
    attempts: int = 0
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    error: str = ""
    slippage_pips: float | None = None
    reference_price: float | None = None  # executable price when the order was sent


class OrderManager:
    def __init__(self, broker: BrokerInterface, verify_approval: Callable[[RiskApproval | None], bool],
                 entries_blocked: Callable[[], bool], max_slippage_pips: float = 2.0, max_attempts: int = 3,
                 retry_delay_s: float = 0.5, on_event: Callable[[str, ManagedOrder], None] | None = None) -> None:
        self.broker = broker
        self._verify = verify_approval
        self._entries_blocked = entries_blocked
        self.max_slippage_pips = max_slippage_pips
        self.max_attempts = max_attempts
        self.retry_delay_s = retry_delay_s
        self.orders: dict[str, ManagedOrder] = {}
        self._on_event = on_event or (lambda kind, order: None)

    @staticmethod
    def client_order_id(proposal_id: str) -> str:
        """Deterministic id: re-submitting the same proposal can never create a new order."""
        return f"FAM-{proposal_id}"[:64]

    def _emit(self, kind: str, order: ManagedOrder) -> None:
        order.updated_at = utcnow()
        try:
            self._on_event(kind, order)
        except Exception:  # noqa: BLE001 - persistence/alert failures must not break execution
            log.exception("order event handler failed")

    def _check_approval(self, proposal: TradeProposal, decision: RiskDecision) -> RiskApproval:
        ap = decision.approval
        if not decision.approved or not self._verify(ap):
            raise RiskViolationError("missing, invalid or expired risk approval")
        assert ap is not None
        s = proposal.setup
        if ap.proposal_id != proposal.proposal_id or ap.symbol != s.symbol or ap.direction != s.direction.value \
                or abs(ap.stop_loss - s.stop_loss) > 1e-9:
            raise RiskViolationError("risk approval does not match the proposal")
        return ap

    def submit(self, proposal: TradeProposal, decision: RiskDecision) -> ManagedOrder:
        """Validate, send and confirm an order. Returns the managed order (check ``status``)."""
        cid = self.client_order_id(proposal.proposal_id)
        if cid in self.orders:  # duplicate protection
            log.warning("duplicate submission ignored", extra={"ctx": {"client_order_id": cid}})
            return self.orders[cid]
        if self._entries_blocked():
            raise SafetyLockError("new entries are blocked (kill switch / safety mode)")
        approval = self._check_approval(proposal, decision)
        s = proposal.setup
        spec = get_instrument(s.symbol)

        # --- pre-trade broker checks
        if not self.broker.is_connected():
            raise BrokerConnectionError("broker not connected")
        info = self.broker.get_symbol_info(s.symbol)
        if not info.tradeable:
            raise OrderRejectedError(f"{s.symbol} not tradeable: {info.reason}")
        quote = self.broker.get_quote(s.symbol)
        ref = quote.ask if s.direction is Direction.LONG else quote.bid
        if (s.direction is Direction.LONG and s.stop_loss >= ref) or (s.direction is Direction.SHORT and s.stop_loss <= ref):
            raise OrderRejectedError("market already beyond the stop-loss")
        acct = self.broker.get_account()
        margin = self.broker.calculate_margin(s.symbol, approval.units)
        if margin > acct.free_margin:
            raise OrderRejectedError(f"insufficient free margin ({margin:.2f} > {acct.free_margin:.2f})")
        # Slippage is bounded relative to the executable price seen now (the Risk Manager sized the trade on it).
        max_price = ref + s.direction.sign * self.max_slippage_pips * spec.pip_size

        request = OrderRequest(cid, s.symbol, OrderSide.BUY if s.direction is Direction.LONG else OrderSide.SELL,
                               approval.units, spec.round_price(s.stop_loss), spec.round_price(s.take_profit),
                               max_price, comment=s.strategy)
        order = ManagedOrder(cid, proposal, request, reference_price=ref)
        self.orders[cid] = order
        self._emit("created", order)
        self._send_with_recovery(order)
        return order

    def _send_with_recovery(self, order: ManagedOrder) -> None:
        while order.attempts < self.max_attempts:
            order.attempts += 1
            try:
                order.status = OrderStatus.SENT
                result = self.broker.place_order(order.request)
                self._handle_result(order, result)
                return
            except OrderRejectedError as exc:
                order.status, order.error = OrderStatus.REJECTED, str(exc)
                self._emit("rejected", order)
                return
            except BrokerConnectionError as exc:
                order.status, order.error = OrderStatus.UNKNOWN, str(exc)
                self._emit("unknown", order)
                if self.resolve_unknown(order):
                    return
                time.sleep(self.retry_delay_s * order.attempts)
        if order.status is OrderStatus.UNKNOWN:
            log.error("order status unresolved", extra={"ctx": {"client_order_id": order.client_order_id}})
            self._emit("unresolved", order)

    def resolve_unknown(self, order: ManagedOrder) -> bool:
        """Query the broker for an UNKNOWN order. True if its final state is now known."""
        try:
            status = self.broker.get_order_status(order.client_order_id)
        except BrokerConnectionError:
            return False
        if status is None:
            return False  # never reached the broker: safe to retry with the same id
        self._handle_result(order, status)
        return True

    def _handle_result(self, order: ManagedOrder, result: OrderResult) -> None:
        order.result = result
        if result.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            positions = {p.position_id for p in self.broker.get_positions()}
            if result.position_id not in positions:
                # Filled then immediately closed (stop hit) is possible; check history.
                closed = {t.trade_id for t in self.broker.get_transaction_history(order.created_at)}
                if result.position_id not in closed:
                    order.status, order.error = OrderStatus.UNKNOWN, "fill reported but position not found"
                    self._emit("anomaly", order)
                    return
            order.status = result.status
            spec = get_instrument(order.request.symbol)
            if result.fill_price is not None:
                d = order.proposal.setup.direction.sign
                ref = order.reference_price if order.reference_price is not None else order.proposal.setup.entry
                order.slippage_pips = (result.fill_price - ref) * d / spec.pip_size
            self._emit("filled", order)
        elif result.status is OrderStatus.REJECTED:
            order.status, order.error = OrderStatus.REJECTED, result.reason
            self._emit("rejected", order)
        else:
            order.status = result.status
            self._emit("updated", order)

    def unresolved(self) -> list[ManagedOrder]:
        return [o for o in self.orders.values() if o.status in (OrderStatus.UNKNOWN, OrderStatus.SENT)]
