"""Consensus engine: hard rules first, weighted score second.

A candidate direction is rejected when any hard rule fails, *whatever* the
number of favourable agents (the Master can refuse a trade with 6/7 agents in
favour). Hard rules:

1. any agent in ``BLOCK`` status (macro blackout, risk pre-check, execution conditions);
2. a core analysis agent in ``ERROR`` / ``INSUFFICIENT_DATA`` (unverified data);
3. macro data unavailable (unless explicitly allowed, e.g. in simulation);
4. any core agent opposing the direction (contradiction);
5. fewer than ``min_supporting`` directional agents in favour;
6. strategy incompatible with the market regime.

Only then is the weighted confidence score computed and compared to a threshold.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.core.types import AgentReport, AgentStatus, Direction

CORE_AGENTS = ("market_analyst", "technical_analyst", "price_action_analyst")
DIRECTIONAL_WEIGHTS = {"market_analyst": 0.30, "technical_analyst": 0.25, "price_action_analyst": 0.25,
                       "strategy_optimizer": 0.20}
FILTER_AGENTS = ("macro_analyst", "risk_manager", "execution_monitor")

# Which strategy families make sense in which regime.
REGIME_COMPATIBILITY = {
    "trend_following": {"trending", "transition"},
    "pullback": {"trending", "transition"},
    "bos_continuation": {"trending", "transition"},
    "momentum": {"trending", "transition"},
    "breakout": {"ranging", "transition", "trending"},
    "mean_reversion": {"ranging"},
}


@dataclass(slots=True)
class ConsensusResult:
    direction: Direction
    accepted: bool
    score: float
    supporting: list[str] = field(default_factory=list)
    opposing: list[str] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {"direction": self.direction.value, "accepted": self.accepted, "score": round(self.score, 3),
                "supporting": self.supporting, "opposing": self.opposing, "vetoes": self.vetoes,
                "contradictions": self.contradictions}


class ConsensusEngine:
    def __init__(self, min_supporting: int = 2, min_score: float = 0.45, allow_missing_macro: bool = False) -> None:
        self.min_supporting = min_supporting
        self.min_score = min_score
        self.allow_missing_macro = allow_missing_macro

    @staticmethod
    def detect_contradictions(reports: dict[str, AgentReport]) -> list[str]:
        """List pairs of usable directional agents pointing in opposite directions."""
        dirs = {n: r.signal for n, r in reports.items()
                if n in DIRECTIONAL_WEIGHTS and r.usable and r.signal is not Direction.NEUTRAL}
        names = sorted(dirs)
        out = []
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if dirs[a] is dirs[b].opposite():
                    out.append(f"{a}={dirs[a].value} vs {b}={dirs[b].value}")
        return out

    def evaluate(self, reports: dict[str, AgentReport], direction: Direction, strategy: str,
                 regime: str | None = None) -> ConsensusResult:
        res = ConsensusResult(direction, False, 0.0)
        res.contradictions = self.detect_contradictions(reports)

        for name, r in reports.items():
            if r.status is AgentStatus.BLOCK:
                res.vetoes.append(f"{name} blocks: {r.summary}")
            elif r.status is AgentStatus.ERROR:
                res.vetoes.append(f"{name} error: {r.summary}")
        for name in CORE_AGENTS:
            r = reports.get(name)
            if r is None or r.status in (AgentStatus.INSUFFICIENT_DATA, AgentStatus.DATA_UNAVAILABLE):
                res.vetoes.append(f"{name}: data not verified/insufficient")
        for name in FILTER_AGENTS:
            if name not in reports:
                res.vetoes.append(f"{name}: no report")
        macro = reports.get("macro_analyst")
        if macro is not None and macro.status is AgentStatus.DATA_UNAVAILABLE and not self.allow_missing_macro:
            res.vetoes.append("macro data unavailable")

        for name in DIRECTIONAL_WEIGHTS:
            r = reports.get(name)
            if r is None or not r.usable or r.signal is Direction.NEUTRAL:
                continue
            (res.supporting if r.signal is direction else res.opposing).append(name)
        if res.opposing:
            res.vetoes.append(f"opposed by {res.opposing}")
        if len(res.supporting) < self.min_supporting:
            res.vetoes.append(f"only {len(res.supporting)} supporting agent(s), {self.min_supporting} required")
        if regime is not None and strategy in REGIME_COMPATIBILITY and regime not in REGIME_COMPATIBILITY[strategy]:
            res.vetoes.append(f"strategy {strategy} incompatible with {regime} regime")

        res.score = sum(DIRECTIONAL_WEIGHTS[n] * reports[n].confidence for n in res.supporting)
        if macro is not None and macro.usable:
            res.score *= macro.confidence  # medium-impact news nearby halves the score
        if not res.vetoes and res.score < self.min_score:
            res.vetoes.append(f"consensus score {res.score:.2f} below {self.min_score}")
        res.accepted = not res.vetoes
        return res
