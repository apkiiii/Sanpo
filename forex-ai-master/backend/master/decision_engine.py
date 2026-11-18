"""Decision engine: from agent reports to ranked trade proposals.

Candidate setups come from the Price Action agent and from strategies validated
by the Strategy Optimizer. Mode-dependent rules:

* SIMULATION: price-action setups are allowed (pipeline exercise).
* DEMO / LIVE: the setup's strategy must be *validated on real historical data*
  by the Strategy Optimizer (synthetic validations are refused).
* LIVE: additionally, the strategy must be in the operator's explicit whitelist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from backend.config.settings import TradingMode
from backend.core.types import AgentReport, Direction, TradeProposal, TradeSetup, new_id, utcnow
from backend.master.consensus_engine import ConsensusEngine, ConsensusResult


@dataclass(slots=True)
class Decision:
    """Record of a candidate opportunity and what happened to it (shown on the dashboard)."""

    decision_id: str
    symbol: str
    time: datetime
    setup: dict[str, Any] | None
    consensus: dict[str, Any] | None
    status: str  # rejected_master | proposed | rejected_risk | executed | execution_failed | skipped
    reasons: list[str] = field(default_factory=list)
    quality: float = 0.0
    risk_checks: dict[str, Any] | None = None
    order_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"decision_id": self.decision_id, "symbol": self.symbol, "time": self.time.isoformat(),
                "setup": self.setup, "consensus": self.consensus, "status": self.status, "reasons": self.reasons,
                "quality": round(self.quality, 3), "risk_checks": self.risk_checks, "order_id": self.order_id}


def setup_to_dict(s: TradeSetup) -> dict[str, Any]:
    return {"symbol": s.symbol, "direction": s.direction.value, "entry": s.entry, "stop_loss": s.stop_loss,
            "take_profit": s.take_profit, "invalidation": s.invalidation, "strategy": s.strategy,
            "timeframe": s.timeframe, "reward_risk": round(s.reward_risk, 2), "rationale": s.rationale}


class DecisionEngine:
    def __init__(self, consensus: ConsensusEngine, mode: TradingMode, live_whitelist: list[str] | None = None,
                 max_candidates_per_cycle: int = 3) -> None:
        self.consensus = consensus
        self.mode = mode
        self.live_whitelist = set(live_whitelist or [])
        self.max_candidates = max_candidates_per_cycle

    def candidates(self, symbol: str, reports: dict[str, AgentReport],
                   validated_setups: list[TradeSetup], validated_names: set[str]) -> list[TradeSetup]:
        out: list[TradeSetup] = list(validated_setups)
        pa = reports.get("price_action_analyst")
        if pa is not None and pa.usable and "setup" in pa.details:
            s = pa.details["setup"]
            setup = TradeSetup(symbol, Direction(s["direction"]), s["entry"], s["stop_loss"], s["take_profit"],
                               s["invalidation"], s["strategy"], pa.details.get("timeframe", ""), pa.confidence,
                               pa.reasons[0] if pa.reasons else "")
            if self.mode is TradingMode.SIMULATION or setup.strategy in validated_names:
                out.append(setup)
        return out

    def _mode_filter(self, setup: TradeSetup, validated_names: set[str]) -> list[str]:
        reasons = []
        if self.mode in (TradingMode.DEMO, TradingMode.LIVE) and setup.strategy not in validated_names:
            reasons.append(f"strategy {setup.strategy} not validated on real historical data")
        if self.mode is TradingMode.LIVE and setup.strategy not in self.live_whitelist:
            reasons.append(f"strategy {setup.strategy} not whitelisted for LIVE by the operator")
        return reasons

    def evaluate_symbol(self, symbol: str, reports: dict[str, AgentReport], validated_setups: list[TradeSetup],
                        validated_names: set[str], regime: str | None, now: datetime | None = None,
                        validated_score: dict[str, float] | None = None) -> list[tuple[Decision, TradeProposal | None]]:
        now = now or utcnow()
        results: list[tuple[Decision, TradeProposal | None]] = []
        cands = self.candidates(symbol, reports, validated_setups, validated_names)
        if not cands:
            d = Decision(new_id("DEC"), symbol, now, None, None, "skipped", ["no candidate setup"])
            return [(d, None)]
        for setup in cands:
            reasons = self._mode_filter(setup, validated_names)
            cons: ConsensusResult = self.consensus.evaluate(reports, setup.direction, setup.strategy, regime)
            reasons += cons.vetoes
            robustness = (validated_score or {}).get(setup.strategy, 0.5)
            quality = cons.score * min(setup.reward_risk / 2.0, 1.25) * (0.5 + robustness)
            if reasons:
                d = Decision(new_id("DEC"), symbol, now, setup_to_dict(setup), cons.to_dict(), "rejected_master",
                             reasons, quality)
                results.append((d, None))
                continue
            proposal = TradeProposal(new_id("PROP"), setup, cons.score, tuple(cons.supporting), False, now)
            d = Decision(new_id("DEC"), symbol, now, setup_to_dict(setup), cons.to_dict(), "proposed",
                         ["consensus reached"], quality)
            results.append((d, proposal))
        return results

    def rank(self, items: list[tuple[Decision, TradeProposal | None]], free_slots: int) -> list[tuple[Decision, TradeProposal]]:
        """Best proposals first, at most one per symbol and ``free_slots`` in total."""
        proposed = sorted([(d, p) for d, p in items if p is not None], key=lambda x: x[0].quality, reverse=True)
        chosen: list[tuple[Decision, TradeProposal]] = []
        symbols: set[str] = set()
        for d, p in proposed:
            if p.setup.symbol in symbols:
                d.status, d.reasons = "skipped", ["another setup on the same symbol ranked higher"]
                continue
            if len(chosen) >= min(free_slots, self.max_candidates):
                d.status, d.reasons = "skipped", ["no free position slot"]
                continue
            symbols.add(p.setup.symbol)
            chosen.append((d, p))
        return chosen
