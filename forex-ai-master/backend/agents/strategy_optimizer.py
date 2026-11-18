"""BOT 6 — Strategy Optimizer.

Runs walk-forward / out-of-sample / Monte Carlo validation for each
(strategy, symbol) pair and maintains an *eligibility table*. The Master bot
only considers signals from eligible strategies. Eligibility is necessary but
never sufficient for LIVE: strategies must also be explicitly whitelisted by
the operator.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from backend.agents.base import Agent, MarketContext
from backend.backtesting.engine import BacktestConfig
from backend.backtesting.validation import EligibilityCriteria, WalkForwardResult, assess_eligibility, walk_forward
from backend.core.types import AgentReport, AgentStatus, Direction, Timeframe, TradeSetup, utcnow
from backend.strategies import STRATEGIES, Strategy


@dataclass(slots=True)
class StrategyRecord:
    strategy: str
    symbol: str
    timeframe: str
    eligible: bool
    reasons: list[str]
    params: dict[str, Any]
    oos_metrics: dict[str, Any]
    efficiency: float
    monte_carlo: dict[str, float]
    synthetic: bool
    validated_at: datetime = field(default_factory=utcnow)

    def summary(self) -> dict[str, Any]:
        m = self.oos_metrics
        return {"strategy": self.strategy, "symbol": self.symbol, "timeframe": self.timeframe,
                "eligible": self.eligible, "reasons": self.reasons, "params": self.params,
                "trades": m.get("trades"), "profit_factor": m.get("profit_factor"),
                "expectancy_r": m.get("expectancy_r"), "win_rate": m.get("win_rate"),
                "max_drawdown_pct": m.get("max_drawdown_pct"), "efficiency": self.efficiency,
                "mc_dd_p95": self.monte_carlo.get("max_dd_p95"), "synthetic": self.synthetic,
                "validated_at": self.validated_at.isoformat()}


class StrategyOptimizer(Agent):
    name = "strategy_optimizer"

    def __init__(self, timeframe: Timeframe = Timeframe.M15, config: BacktestConfig | None = None,
                 criteria: EligibilityCriteria | None = None, n_folds: int = 4,
                 allow_synthetic_for_simulation: bool = False) -> None:
        super().__init__()
        self.timeframe = timeframe
        self.config = config or BacktestConfig()
        self.criteria = criteria or EligibilityCriteria()
        self.n_folds = n_folds
        self.allow_synthetic_for_simulation = allow_synthetic_for_simulation
        self.records: dict[tuple[str, str], StrategyRecord] = {}

    # ------------------------------------------------------------ validation
    def validate(self, symbol: str, df: pd.DataFrame, strategies: list[str] | None = None) -> list[StrategyRecord]:
        """Run the full validation pipeline for ``symbol`` on historical ``df``."""
        out = []
        for name in strategies or list(STRATEGIES):
            wf: WalkForwardResult = walk_forward(symbol, df, STRATEGIES[name], self.config, self.n_folds)
            eligible, reasons = assess_eligibility(wf, self.criteria)
            if wf.synthetic and self.allow_synthetic_for_simulation and reasons == ["validated on SYNTHETIC data only"]:
                eligible = True  # simulation-only pipeline checks; never accepted in DEMO/LIVE (see master)
            params = wf.folds[-1].best_params if wf.folds else {}
            rec = StrategyRecord(name, symbol, self.timeframe.value, eligible, reasons, params, wf.oos_metrics,
                                 wf.efficiency, wf.monte_carlo, wf.synthetic)
            self.records[(name, symbol)] = rec
            out.append(rec)
        return out

    def eligible_strategies(self, symbol: str, allow_synthetic: bool = False) -> list[StrategyRecord]:
        return [r for (s, sym), r in self.records.items()
                if sym == symbol and r.eligible and (allow_synthetic or not r.synthetic)]

    def save(self, path: str) -> None:
        """Persist the validation table (JSON) so the runtime does not re-run validations at start-up."""
        rows = []
        for r in self.records.values():
            row = r.summary()
            row["oos_metrics"] = {k: v for k, v in r.oos_metrics.items() if not isinstance(v, dict)}
            row["monte_carlo"] = r.monte_carlo
            rows.append(row)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(rows, indent=2, default=str))

    def load(self, path: str) -> int:
        p = Path(path)
        if not p.exists():
            return 0
        for row in json.loads(p.read_text()):
            if row["strategy"] not in STRATEGIES:
                continue
            self.records[(row["strategy"], row["symbol"])] = StrategyRecord(
                row["strategy"], row["symbol"], row["timeframe"], bool(row["eligible"]), list(row["reasons"]),
                dict(row["params"]), dict(row.get("oos_metrics", {})), float(row.get("efficiency") or 0.0),
                dict(row.get("monte_carlo", {})), bool(row["synthetic"]),
                datetime.fromisoformat(row["validated_at"]))
        return len(self.records)

    def ranking(self) -> list[dict[str, Any]]:
        rows = [r.summary() for r in self.records.values()]
        return sorted(rows, key=lambda r: (not r["eligible"], -(r["expectancy_r"] or 0)))

    # ------------------------------------------------------------ live signals
    def strategy_setups(self, symbol: str, df: pd.DataFrame, allow_synthetic: bool = False,
                        indicators: pd.DataFrame | None = None) -> list[TradeSetup]:
        """Setups produced on the last closed bar by eligible strategies (with validated params)."""
        setups = []
        for rec in self.eligible_strategies(symbol, allow_synthetic):
            strat: Strategy = STRATEGIES[rec.strategy](**rec.params)
            setup = strat.latest_setup(symbol, df, self.timeframe.value, indicators)
            if setup is not None:
                setups.append(setup)
        return setups

    def _analyze(self, ctx: MarketContext) -> AgentReport:
        allow_syn = bool(ctx.extras.get("allow_synthetic", False))
        eligible = self.eligible_strategies(ctx.symbol, allow_syn)
        details: dict[str, Any] = {"eligible": [r.summary() for r in eligible],
                                   "evaluated": [r.summary() for (s, sym), r in self.records.items() if sym == ctx.symbol]}
        if not eligible:
            return AgentReport(self.name, ctx.symbol, AgentStatus.OK, Direction.NEUTRAL, 0.0,
                               "no validated strategy for this symbol", details,
                               ("no strategy passed out-of-sample validation",), ctx.now)
        err = self._check_frame(ctx, self.timeframe)
        if err:
            return self._insufficient(ctx, err)
        setups = self.strategy_setups(ctx.symbol, ctx.candles[self.timeframe], allow_syn)
        details["setups"] = [{"strategy": s.strategy, "direction": s.direction.value, "entry": s.entry,
                              "stop_loss": s.stop_loss, "take_profit": s.take_profit} for s in setups]
        dirs = {s.direction for s in setups}
        if len(dirs) == 1:
            d = dirs.pop()
            conf = min(1.0, 0.4 + 0.2 * len(setups))
            return AgentReport(self.name, ctx.symbol, AgentStatus.OK, d, conf,
                               f"{len(setups)} validated strategy signal(s) {d.value}", details,
                               tuple(f"{s.strategy} -> {s.direction.value}" for s in setups), ctx.now)
        reason = "validated strategies disagree" if dirs else "no validated strategy signal on last bar"
        return AgentReport(self.name, ctx.symbol, AgentStatus.OK, Direction.NEUTRAL, 0.0, reason, details,
                           (reason,), ctx.now)
