"""Position manager: local trade records, management rules and reconciliation.

The broker is the source of truth. Local records enrich broker positions with
the trade rationale (strategy, validating agents, invalidation level, initial
risk). Stop-losses can only be *tightened*; they are never widened.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from backend.core.errors import BrokerConnectionError, RiskViolationError
from backend.core.instruments import get_instrument
from backend.core.logging import get_logger
from backend.core.types import BrokerPosition, ClosedTrade, Direction, PositionStatus, Quote, utcnow

log = get_logger(__name__)


@dataclass(slots=True)
class ManagedPosition:
    """Full trade record required by the specification."""

    trade_id: str  # = broker position id
    symbol: str
    direction: Direction
    units: float
    entry_price: float
    stop_loss: float | None
    take_profit: float | None
    initial_stop: float | None
    initial_risk: float  # account currency
    potential_profit: float
    reward_risk: float
    entry_time: datetime
    status: PositionStatus = PositionStatus.OPEN
    entry_reason: str = ""
    strategy: str = ""
    validated_by: tuple[str, ...] = ()
    invalidation: float | None = None
    client_order_id: str | None = None
    unrealized_pnl: float = 0.0
    managed: bool = True  # False for positions found at the broker but unknown locally
    breakeven_done: bool = False
    exit_reason: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"trade_id": self.trade_id, "symbol": self.symbol, "direction": self.direction.value,
                "units": self.units, "entry_price": self.entry_price, "stop_loss": self.stop_loss,
                "take_profit": self.take_profit, "initial_stop": self.initial_stop, "initial_risk": self.initial_risk,
                "potential_profit": self.potential_profit, "reward_risk": self.reward_risk,
                "entry_time": self.entry_time.isoformat(), "status": self.status.value,
                "entry_reason": self.entry_reason, "strategy": self.strategy, "validated_by": list(self.validated_by),
                "invalidation": self.invalidation, "unrealized_pnl": round(self.unrealized_pnl, 2),
                "managed": self.managed, "breakeven_done": self.breakeven_done, "exit_reason": self.exit_reason,
                "notes": self.notes[-5:]}


@dataclass(frozen=True, slots=True)
class ManagementPolicy:
    breakeven_enabled: bool = True
    breakeven_trigger_r: float = 1.0  # move to break-even after +1R
    breakeven_buffer_pips: float = 0.5
    trailing_enabled: bool = False
    trailing_atr_multiple: float = 2.0
    exit_on_invalidation: bool = True


@dataclass(slots=True)
class ReconciliationReport:
    unknown_at_broker: list[BrokerPosition] = field(default_factory=list)
    missing_at_broker: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    closed_trades: list[ClosedTrade] = field(default_factory=list)

    @property
    def anomalies(self) -> bool:
        return bool(self.unknown_at_broker or self.mismatches)


class PositionManager:
    def __init__(self, broker: Any, policy: ManagementPolicy | None = None,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.broker = broker
        self.policy = policy or ManagementPolicy()
        self.positions: dict[str, ManagedPosition] = {}
        self.closed: list[ManagedPosition] = []
        self._on_event = on_event or (lambda kind, data: None)

    # ------------------------------------------------------------------ registry
    def register(self, pos: ManagedPosition) -> None:
        self.positions[pos.trade_id] = pos
        self._on_event("position_opened", pos.to_dict())

    def open_positions(self) -> list[ManagedPosition]:
        return [p for p in self.positions.values() if p.status is PositionStatus.OPEN]

    # ------------------------------------------------------------------ stops
    def modify_stop(self, trade_id: str, new_stop: float, quote: Quote | None, reason: str) -> bool:
        """Tighten a stop-loss. Raises :class:`RiskViolationError` if it would widen the risk."""
        p = self.positions[trade_id]
        if p.stop_loss is not None:
            widening = (new_stop < p.stop_loss) if p.direction is Direction.LONG else (new_stop > p.stop_loss)
            if widening:
                raise RiskViolationError(f"refused to widen stop-loss of {trade_id} ({p.stop_loss} -> {new_stop})")
        if quote is not None:
            ref = quote.bid if p.direction is Direction.LONG else quote.ask
            if (new_stop - ref) * p.direction.sign >= 0:
                raise RiskViolationError("new stop would be beyond the current price")
        new_stop = get_instrument(p.symbol).round_price(new_stop)
        if not self.broker.modify_order(trade_id, stop_loss=new_stop):
            return False
        p.notes.append(f"{utcnow().isoformat()} stop {p.stop_loss} -> {new_stop} ({reason})")
        p.stop_loss = new_stop
        self._on_event("stop_modified", {"trade_id": trade_id, "stop_loss": new_stop, "reason": reason})
        return True

    # ------------------------------------------------------------------ management
    def manage(self, quotes: dict[str, Quote], atr_by_symbol: dict[str, float] | None = None,
               closes_by_symbol: dict[str, float] | None = None) -> list[str]:
        """Apply break-even, trailing stop and invalidation exits. Returns actions taken."""
        actions: list[str] = []
        for p in self.open_positions():
            q = quotes.get(p.symbol)
            if q is None or p.initial_stop is None:
                continue
            spec = get_instrument(p.symbol)
            d = p.direction.sign
            price = q.bid if d > 0 else q.ask
            r_dist = abs(p.entry_price - p.initial_stop)
            progress_r = (price - p.entry_price) * d / r_dist if r_dist > 0 else 0.0
            try:
                close = (closes_by_symbol or {}).get(p.symbol)
                if self.policy.exit_on_invalidation and p.invalidation is not None and close is not None \
                        and (close - p.invalidation) * d < 0 and p.stop_loss is not None \
                        and (p.invalidation - p.stop_loss) * d > 0:
                    self.close(p.trade_id, "invalidation")
                    actions.append(f"{p.trade_id}: closed on invalidation")
                    continue
                if self.policy.breakeven_enabled and not p.breakeven_done and progress_r >= self.policy.breakeven_trigger_r:
                    be = p.entry_price + d * self.policy.breakeven_buffer_pips * spec.pip_size
                    if p.stop_loss is None or (be - p.stop_loss) * d > 0:
                        if self.modify_stop(p.trade_id, be, q, "break-even"):
                            p.breakeven_done = True
                            actions.append(f"{p.trade_id}: break-even")
                    else:
                        p.breakeven_done = True
                atr_v = (atr_by_symbol or {}).get(p.symbol)
                if self.policy.trailing_enabled and atr_v and progress_r > 1.0:
                    trail = price - d * self.policy.trailing_atr_multiple * atr_v
                    if p.stop_loss is not None and (trail - p.stop_loss) * d > spec.pip_size:
                        if self.modify_stop(p.trade_id, trail, q, "trailing"):
                            actions.append(f"{p.trade_id}: trailing stop {trail:.5f}")
            except (BrokerConnectionError, RiskViolationError) as exc:
                actions.append(f"{p.trade_id}: management failed ({exc})")
        return actions

    def close(self, trade_id: str, reason: str) -> None:
        p = self.positions[trade_id]
        self.broker.close_position(trade_id, reason=reason)
        p.exit_reason = reason
        self._on_event("close_requested", {"trade_id": trade_id, "reason": reason})

    def close_all(self, reason: str) -> list[str]:
        errors = []
        for p in self.open_positions():
            try:
                self.close(p.trade_id, reason)
            except Exception as exc:  # noqa: BLE001 - try every position
                errors.append(f"{p.trade_id}: {exc}")
        return errors

    def move_all_to_breakeven(self, quotes: dict[str, Quote]) -> list[str]:
        """Used when the session target is reached: tighten stops of profitable positions only."""
        done = []
        for p in self.open_positions():
            q = quotes.get(p.symbol)
            if q is None:
                continue
            d = p.direction.sign
            spec = get_instrument(p.symbol)
            be = p.entry_price + d * self.policy.breakeven_buffer_pips * spec.pip_size
            price = q.bid if d > 0 else q.ask
            if (price - be) * d > spec.pip_size and (p.stop_loss is None or (be - p.stop_loss) * d > 0):
                try:
                    if self.modify_stop(p.trade_id, be, q, "session target reached"):
                        p.breakeven_done = True
                        done.append(p.trade_id)
                except RiskViolationError:
                    pass
        return done

    # ------------------------------------------------------------------ reconciliation
    def reconcile(self, since: datetime | None = None) -> ReconciliationReport:
        """Align local records with the broker. Never silently drops a discrepancy."""
        rep = ReconciliationReport()
        broker_positions = {p.position_id: p for p in self.broker.get_positions()}
        local_open = {p.trade_id: p for p in self.open_positions()}

        for pid, bp in broker_positions.items():
            lp = local_open.get(pid)
            if lp is None:
                rep.unknown_at_broker.append(bp)
                self.positions[pid] = ManagedPosition(
                    pid, bp.symbol, bp.direction, bp.units, bp.entry_price, bp.stop_loss, bp.take_profit,
                    bp.stop_loss, 0.0, 0.0, 0.0, bp.open_time, entry_reason="unknown position found at broker",
                    managed=False, client_order_id=bp.client_order_id)
                continue
            lp.unrealized_pnl = bp.unrealized_pnl
            if abs(lp.units - bp.units) > 1e-6:
                rep.mismatches.append(f"{pid}: units local={lp.units} broker={bp.units}")
                lp.units = bp.units
            if bp.stop_loss is None and lp.stop_loss is not None:
                rep.mismatches.append(f"{pid}: stop-loss missing at broker")
                try:
                    if self.broker.modify_order(pid, stop_loss=lp.stop_loss):
                        rep.fixed.append(f"{pid}: stop-loss restored")
                except BrokerConnectionError:
                    pass
            elif bp.stop_loss is not None and lp.stop_loss is not None and \
                    abs(bp.stop_loss - lp.stop_loss) > 0.5 * 10 ** -get_instrument(lp.symbol).price_precision:
                # Adopt the broker's stop only if it is tighter; otherwise restore ours.
                tighter = (bp.stop_loss - lp.stop_loss) * lp.direction.sign > 0
                if tighter:
                    lp.stop_loss = bp.stop_loss
                else:
                    rep.mismatches.append(f"{pid}: broker stop wider than local")
                    try:
                        if self.broker.modify_order(pid, stop_loss=lp.stop_loss):
                            rep.fixed.append(f"{pid}: stop-loss re-tightened")
                    except BrokerConnectionError:
                        pass

        missing = [pid for pid in local_open if pid not in broker_positions]
        if missing:
            history = {t.trade_id: t for t in self.broker.get_transaction_history(since)}
            for pid in missing:
                lp = local_open[pid]
                lp.status = PositionStatus.CLOSED
                trade = history.get(pid)
                if trade is not None:
                    lp.exit_reason = lp.exit_reason or trade.exit_reason
                    rep.closed_trades.append(trade)
                    self._on_event("position_closed", {**lp.to_dict(), "net_pnl": trade.net_pnl,
                                                       "exit_price": trade.exit_price})
                else:
                    rep.missing_at_broker.append(pid)
                    lp.notes.append("closed at broker but no transaction found")
                self.closed.append(lp)
                del self.positions[pid]
        return rep
