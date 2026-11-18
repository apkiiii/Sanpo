"""BOT 5 — Risk Manager (absolute veto).

Wraps :class:`RiskEngine` and :class:`SafetyManager`. It is the only component
able to issue a signed :class:`RiskApproval`. Its limits are frozen and cannot be
modified by the Master bot (there is no setter, and the object is immutable).
"""

from __future__ import annotations

from collections import Counter

from backend.agents.base import Agent, MarketContext
from backend.analysis.indicators import atr
from backend.config.settings import RiskLimits
from backend.core.instruments import get_instrument
from backend.core.types import AgentReport, AgentStatus, Direction, TradeProposal, Timeframe
from backend.risk.risk_engine import RiskApproval, RiskDecision, RiskEngine, RiskState
from backend.risk.safety_manager import SafetyManager


def atr_spike_ratio(df, period: int = 14, baseline: int = 100) -> float | None:  # type: ignore[no-untyped-def]
    """Current ATR divided by its median over ``baseline`` bars (volatility shock detector)."""
    a = atr(df, period).dropna()
    if len(a) < 20:
        return None
    base = float(a.iloc[-baseline:].median())
    return float(a.iloc[-1] / base) if base > 0 else None


class RiskManager(Agent):
    name = "risk_manager"
    min_bars = 40

    def __init__(self, limits: RiskLimits, safety: SafetyManager, account_ccy: str = "USD",
                 timeframe: Timeframe = Timeframe.M15) -> None:
        super().__init__()
        self._engine = RiskEngine(limits, account_ccy)
        self.safety = safety
        self.timeframe = timeframe
        self.decisions: list[RiskDecision] = []
        self.rejection_counts: Counter[str] = Counter()

    @property
    def limits(self) -> RiskLimits:
        return self._engine.limits

    @property
    def engine(self) -> RiskEngine:
        return self._engine

    def review(self, proposal: TradeProposal, state: RiskState) -> RiskDecision:
        """Evaluate a proposal. The result is final: there is no override path."""
        state.kill_switch = state.kill_switch or self.safety.state.kill_switch
        state.safety_mode = state.safety_mode or self.safety.state.safety_mode
        decision = self._engine.evaluate(proposal, state)
        if decision.trigger_safety_mode:
            self.safety.enter_safety_mode(decision.trigger_safety_mode)
        for name, (ok, _) in decision.checks.items():
            if not ok:
                self.rejection_counts[name] += 1
        self.decisions.append(decision)
        self.decisions = self.decisions[-500:]
        return decision

    def verify_approval(self, approval: RiskApproval | None) -> bool:
        return approval is not None and self._engine.verify_approval(approval)

    def _analyze(self, ctx: MarketContext) -> AgentReport:
        """Per-symbol pre-trade conditions (spread, volatility, global locks)."""
        reasons: list[str] = []
        details: dict[str, object] = {"limits": self.limits.model_dump(), "blocking": self.safety.blocking_reasons()}
        blocked = self.safety.entries_blocked
        if blocked:
            reasons.extend(self.safety.blocking_reasons())
        if ctx.quote is not None:
            spread = get_instrument(ctx.symbol).pips(ctx.quote.spread)
            details["spread_pips"] = round(spread, 2)
            if spread > self.limits.max_spread_pips:
                blocked = True
                reasons.append(f"spread {spread:.1f} pips above limit")
        df = ctx.frame(self.timeframe)
        if df is not None and len(df) >= self.min_bars:
            ratio = atr_spike_ratio(df)
            details["atr_spike_ratio"] = ratio
            if ratio is None or ratio > self.limits.max_atr_spike_ratio:
                blocked = True
                reasons.append("extreme or unknown volatility")
        else:
            blocked = True
            reasons.append("insufficient data for volatility check")
        status = AgentStatus.BLOCK if blocked else AgentStatus.OK
        return AgentReport(self.name, ctx.symbol, status, Direction.NEUTRAL, 1.0 if not blocked else 0.0,
                           "entries blocked" if blocked else "risk conditions acceptable", details,
                           tuple(reasons) or ("all pre-trade conditions acceptable",), ctx.now)
