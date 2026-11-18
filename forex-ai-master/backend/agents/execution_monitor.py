"""BOT 7 — Execution & Trade Monitor.

Executes *only* proposals carrying a valid Risk Manager approval (through the
:class:`OrderManager`), records each confirmed fill as a fully documented
:class:`ManagedPosition`, monitors open positions and reconciles them with the
broker.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.agents.base import Agent, MarketContext
from backend.core.errors import BrokerConnectionError
from backend.core.instruments import get_instrument, price_move_value
from backend.core.types import AgentReport, AgentStatus, Direction, OrderStatus, TradeProposal, utcnow
from backend.risk.risk_engine import RiskDecision
from backend.trading.broker_interface import BrokerInterface
from backend.trading.order_manager import ManagedOrder, OrderManager
from backend.trading.position_manager import ManagedPosition, ManagementPolicy, PositionManager, ReconciliationReport


class ExecutionMonitor(Agent):
    name = "execution_monitor"
    min_bars = 1

    def __init__(self, broker: BrokerInterface, verify_approval: Callable[[Any], bool],
                 entries_blocked: Callable[[], bool], max_spread_pips: float, max_slippage_pips: float,
                 policy: ManagementPolicy | None = None, account_ccy: str = "USD",
                 on_order_event: Callable[[str, ManagedOrder], None] | None = None,
                 on_position_event: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        super().__init__()
        self.broker = broker
        self.account_ccy = account_ccy
        self.max_spread_pips = max_spread_pips
        self.orders = OrderManager(broker, verify_approval, entries_blocked, max_slippage_pips,
                                   on_event=on_order_event)
        self.positions = PositionManager(broker, policy, on_event=on_position_event)
        self.last_reconciliation: ReconciliationReport | None = None

    def execute(self, proposal: TradeProposal, decision: RiskDecision, mid_prices: dict[str, float]) -> ManagedOrder:
        """Send an approved proposal and register the resulting position once confirmed."""
        order = self.orders.submit(proposal, decision)
        if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and order.result is not None \
                and order.result.position_id and order.result.position_id not in self.positions.positions:
            self._register(order, proposal, mid_prices)
        return order

    def _register(self, order: ManagedOrder, proposal: TradeProposal, mid_prices: dict[str, float]) -> None:
        assert order.result is not None and order.result.fill_price is not None
        s = proposal.setup
        fill = order.result.fill_price
        units = order.result.filled_units
        stop = order.request.stop_loss  # exactly what the broker holds (rounded towards the position)
        tp = order.request.take_profit if order.request.take_profit is not None else s.take_profit
        risk = price_move_value(s.symbol, abs(fill - stop), units, self.account_ccy, mid_prices)
        reward = price_move_value(s.symbol, abs(tp - fill), units, self.account_ccy, mid_prices)
        self.positions.register(ManagedPosition(
            order.result.position_id or "", s.symbol, s.direction, units, fill, stop, tp,
            stop, risk, reward, reward / risk if risk > 0 else 0.0, order.result.time,
            entry_reason=s.rationale, strategy=s.strategy, validated_by=proposal.supporting_agents,
            invalidation=s.invalidation, client_order_id=order.client_order_id))

    def reconcile(self) -> ReconciliationReport:
        rep = self.positions.reconcile()
        self.last_reconciliation = rep
        for order in self.orders.unresolved():
            if self.orders.resolve_unknown(order) and order.result and order.result.position_id \
                    and order.result.position_id not in self.positions.positions \
                    and order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                self._register(order, order.proposal, {})
        return rep

    def _analyze(self, ctx: MarketContext) -> AgentReport:
        details: dict[str, Any] = {"connected": False, "open_positions": [], "unresolved_orders": 0}
        reasons: list[str] = []
        try:
            connected = self.broker.is_connected()
            details["connected"] = connected
            if not connected:
                return AgentReport(self.name, ctx.symbol, AgentStatus.BLOCK, summary="broker disconnected",
                                   details=details, reasons=("broker disconnected",), timestamp=ctx.now)
            info = self.broker.get_symbol_info(ctx.symbol)
            quote = self.broker.get_quote(ctx.symbol)
            spread = get_instrument(ctx.symbol).pips(quote.spread)
            details.update({"tradeable": info.tradeable, "spread_pips": round(spread, 2),
                            "quote_age_s": (utcnow() - quote.time).total_seconds()})
            if not info.tradeable:
                reasons.append(f"not tradeable: {info.reason}")
            if spread > self.max_spread_pips:
                reasons.append(f"spread {spread:.1f} pips too wide")
        except BrokerConnectionError as exc:
            return AgentReport(self.name, ctx.symbol, AgentStatus.BLOCK, summary=f"broker error: {exc}",
                               details=details, reasons=(str(exc),), timestamp=ctx.now)
        details["open_positions"] = [p.to_dict() for p in self.positions.open_positions() if p.symbol == ctx.symbol]
        details["unresolved_orders"] = len(self.orders.unresolved())
        if details["unresolved_orders"]:
            reasons.append("unresolved orders pending")
        status = AgentStatus.BLOCK if reasons else AgentStatus.OK
        return AgentReport(self.name, ctx.symbol, status, Direction.NEUTRAL, 1.0 if not reasons else 0.0,
                           "execution conditions OK" if not reasons else "; ".join(reasons), details,
                           tuple(reasons) or ("broker connected, spread acceptable",), ctx.now)
