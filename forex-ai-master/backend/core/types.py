"""Core domain types shared by all modules."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    """Generate a unique identifier with a readable prefix."""
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"

    @property
    def sign(self) -> int:
        return {"long": 1, "short": -1, "neutral": 0}[self.value]

    def opposite(self) -> "Direction":
        if self is Direction.LONG:
            return Direction.SHORT
        if self is Direction.SHORT:
            return Direction.LONG
        return Direction.NEUTRAL


class Timeframe(StrEnum):
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    @property
    def minutes(self) -> int:
        return {"M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}[self.value]

    @property
    def pandas_rule(self) -> str:
        return {"M5": "5min", "M15": "15min", "M30": "30min", "H1": "1h", "H4": "4h", "D1": "1D"}[self.value]


class AgentStatus(StrEnum):
    OK = "ok"
    INSUFFICIENT_DATA = "insufficient_data"
    DATA_UNAVAILABLE = "data_unavailable"
    BLOCK = "block"  # the agent explicitly blocks new entries
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AgentReport:
    """Structured output of an analysis agent, consumed by the Master bot."""

    agent: str
    symbol: str
    status: AgentStatus
    signal: Direction = Direction.NEUTRAL
    confidence: float = 0.0  # 0..1, never used alone to take a decision
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    timestamp: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")

    @property
    def usable(self) -> bool:
        return self.status is AgentStatus.OK


@dataclass(frozen=True, slots=True)
class TradeSetup:
    """A concrete trade idea with entry, stop-loss, take-profit and invalidation."""

    symbol: str
    direction: Direction
    entry: float
    stop_loss: float
    take_profit: float
    invalidation: float
    strategy: str
    timeframe: str
    confidence: float
    rationale: str = ""

    @property
    def risk_distance(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def reward_distance(self) -> float:
        return abs(self.take_profit - self.entry)

    @property
    def reward_risk(self) -> float:
        return self.reward_distance / self.risk_distance if self.risk_distance > 0 else 0.0


@dataclass(frozen=True, slots=True)
class TradeProposal:
    """A setup accepted by the Master bot and submitted to the Risk Manager."""

    proposal_id: str
    setup: TradeSetup
    consensus_score: float
    supporting_agents: tuple[str, ...]
    exceptional: bool = False  # request for exceptional risk (still capped by RiskLimits)
    created_at: datetime = field(default_factory=utcnow)


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    PENDING = "pending"  # created locally, not yet sent
    SENT = "sent"  # sent, waiting for confirmation
    UNKNOWN = "unknown"  # timeout / disconnect: must be resolved by querying the broker
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass(slots=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    time: datetime = field(default_factory=utcnow)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(slots=True)
class OrderRequest:
    """Order sent to a broker. ``client_order_id`` guarantees idempotency."""

    client_order_id: str
    symbol: str
    side: OrderSide
    units: float
    stop_loss: float
    take_profit: float | None
    max_slippage_price: float | None = None
    comment: str = ""


@dataclass(slots=True)
class OrderResult:
    client_order_id: str
    status: OrderStatus
    broker_order_id: str | None = None
    position_id: str | None = None
    filled_units: float = 0.0
    fill_price: float | None = None
    commission: float = 0.0
    reason: str = ""
    time: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class BrokerPosition:
    """Position as reported by the broker (source of truth)."""

    position_id: str
    symbol: str
    direction: Direction
    units: float
    entry_price: float
    stop_loss: float | None
    take_profit: float | None
    unrealized_pnl: float
    swap: float = 0.0
    commission: float = 0.0
    client_order_id: str | None = None
    open_time: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class AccountSnapshot:
    balance: float
    equity: float
    margin_used: float
    free_margin: float
    currency: str = "USD"
    time: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class ClosedTrade:
    """Realised trade with full cost breakdown (all amounts in account currency)."""

    trade_id: str
    symbol: str
    direction: Direction
    units: float
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    gross_pnl: float
    commission: float
    swap: float
    slippage_cost: float
    exit_reason: str
    strategy: str = ""

    @property
    def net_pnl(self) -> float:
        # commission and swap are signed costs (negative = paid); slippage is already in gross.
        return self.gross_pnl + self.commission + self.swap
