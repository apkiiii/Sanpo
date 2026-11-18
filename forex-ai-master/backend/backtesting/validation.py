"""Robustness validation: walk-forward, out-of-sample, parameter stability, Monte Carlo.

Parameters are selected on in-sample data with a *risk-adjusted* score (not
raw profit) and judged only on the following out-of-sample segment.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pandas as pd

from backend.analysis.indicators import compute_all
from backend.backtesting.engine import BacktestConfig, BacktestEngine, BacktestResult, BacktestTrade
from backend.backtesting.metrics import compute_metrics, trade_stats
from backend.strategies.base import Strategy

MIN_TRADES_FOR_SCORE = 10


def robust_score(result: BacktestResult) -> float:
    """Risk-adjusted score: expectancy in R scaled by sample size, penalised by drawdown."""
    st = trade_stats(result.trades)
    if st["trades"] < MIN_TRADES_FOR_SCORE:
        return float("-inf")
    eq = result.equity_curve
    peak = eq.cummax()
    dd_pct = float(((peak - eq) / peak).max() * 100)
    return st["expectancy_r"] * math.sqrt(min(st["trades"], 100)) - 0.1 * dd_pct


def param_combinations(grid: dict[str, list[Any]], limit: int = 81) -> list[dict[str, Any]]:
    keys = list(grid)
    combos = [dict(zip(keys, vals, strict=True)) for vals in itertools.product(*(grid[k] for k in keys))]
    return combos[:limit]


@dataclass(slots=True)
class FoldResult:
    fold: int
    train: tuple[str, str]
    test: tuple[str, str]
    best_params: dict[str, Any]
    in_sample_score: float
    in_sample_expectancy_r: float
    out_of_sample: dict[str, float]
    plateau_ratio: float  # share of the grid with positive in-sample expectancy


@dataclass(slots=True)
class WalkForwardResult:
    symbol: str
    strategy: str
    folds: list[FoldResult]
    oos_trades: list[BacktestTrade]
    oos_metrics: dict[str, Any]
    efficiency: float  # OOS expectancy_r / IS expectancy_r
    synthetic: bool
    monte_carlo: dict[str, float] = field(default_factory=dict)


def _run_slice(engine_cfg: BacktestConfig, symbol: str, df: pd.DataFrame, ind: pd.DataFrame, a: int, b: int,
               strategy: Strategy) -> BacktestResult:
    cfg = replace(engine_cfg, warmup_bars=engine_cfg.warmup_bars if a == 0 else 0)
    sub = df.iloc[a:b]
    sub.attrs = dict(df.attrs)
    return BacktestEngine(cfg).run(symbol, sub, strategy, ind.iloc[a:b])


def walk_forward(symbol: str, df: pd.DataFrame, strategy_cls: type[Strategy], config: BacktestConfig | None = None,
                 n_folds: int = 4, grid_limit: int = 27, mc_runs: int = 1000) -> WalkForwardResult:
    """Rolling walk-forward: optimise on segment k, test on segment k+1."""
    cfg = config or BacktestConfig()
    ind = compute_all(df)
    n = len(df)
    bounds = np.linspace(0, n, n_folds + 2, dtype=int)
    combos = param_combinations(strategy_cls.param_grid or {}, grid_limit) or [{}]
    folds: list[FoldResult] = []
    oos_trades: list[BacktestTrade] = []
    oos_equity_parts: list[pd.Series] = []
    is_exp, oos_exp = [], []
    balance = cfg.initial_balance
    for k in range(n_folds):
        a, b, c = bounds[k], bounds[k + 1], bounds[k + 2]
        scored = []
        for params in combos:
            res = _run_slice(cfg, symbol, df, ind, a, b, strategy_cls(**params))
            scored.append((robust_score(res), params, trade_stats(res.trades)["expectancy_r"]))
        valid = [s for s in scored if s[0] != float("-inf")]
        plateau = float(np.mean([s[2] > 0 for s in valid])) if valid else 0.0
        best_score, best_params, best_exp = max(scored, key=lambda s: s[0])
        test_cfg = replace(cfg, initial_balance=balance)
        test = _run_slice(test_cfg, symbol, df, ind, b, c, strategy_cls(**best_params))
        balance = float(test.equity_curve.iloc[-1]) if len(test.equity_curve) else balance
        oos_trades.extend(test.trades)
        oos_equity_parts.append(test.equity_curve)
        st = trade_stats(test.trades)
        if best_score != float("-inf"):
            is_exp.append(best_exp)
            oos_exp.append(st["expectancy_r"])
        folds.append(FoldResult(k, (str(df.index[a]), str(df.index[b - 1])), (str(df.index[b]), str(df.index[c - 1])),
                                best_params, best_score, best_exp, st, plateau))
    oos_equity = pd.concat(oos_equity_parts) if oos_equity_parts else pd.Series(dtype=float)
    oos_equity = oos_equity[~oos_equity.index.duplicated(keep="last")]
    metrics = compute_metrics(oos_trades, oos_equity, cfg.initial_balance)
    mean_is = float(np.mean(is_exp)) if is_exp else 0.0
    efficiency = float(np.mean(oos_exp)) / mean_is if mean_is > 0 else 0.0
    mc = monte_carlo([t.net_pnl for t in oos_trades], cfg.initial_balance, cfg.max_drawdown_pct, runs=mc_runs)
    return WalkForwardResult(symbol, strategy_cls.name, folds, oos_trades, metrics, efficiency,
                             bool(df.attrs.get("synthetic", False)), mc)


def monte_carlo(pnls: list[float], initial_balance: float, ruin_dd_pct: float, runs: int = 1000,
                seed: int = 11) -> dict[str, float]:
    """Bootstrap the trade sequence to estimate the distribution of returns and drawdowns."""
    if len(pnls) < 5:
        return {"runs": 0}
    rng = np.random.default_rng(seed)
    arr = np.asarray(pnls, dtype=float)
    samples = rng.choice(arr, size=(runs, len(arr)), replace=True)
    curves = initial_balance + np.cumsum(samples, axis=1)
    curves = np.concatenate([np.full((runs, 1), initial_balance), curves], axis=1)
    peaks = np.maximum.accumulate(curves, axis=1)
    dd_pct = ((peaks - curves) / peaks).max(axis=1) * 100
    final_ret = (curves[:, -1] - initial_balance) / initial_balance * 100
    return {
        "runs": runs,
        "return_p5": float(np.percentile(final_ret, 5)),
        "return_p50": float(np.percentile(final_ret, 50)),
        "return_p95": float(np.percentile(final_ret, 95)),
        "max_dd_p50": float(np.percentile(dd_pct, 50)),
        "max_dd_p95": float(np.percentile(dd_pct, 95)),
        "prob_loss": float((final_ret < 0).mean()),
        "prob_dd_limit": float((dd_pct >= ruin_dd_pct).mean()),
    }


@dataclass(frozen=True, slots=True)
class EligibilityCriteria:
    min_oos_trades: int = 30
    min_profit_factor: float = 1.1
    min_expectancy_r: float = 0.0
    min_efficiency: float = 0.3
    max_mc_dd_p95_pct: float = 5.0
    min_plateau_ratio: float = 0.4


def assess_eligibility(wf: WalkForwardResult, criteria: EligibilityCriteria | None = None) -> tuple[bool, list[str]]:
    """Return (eligible, reasons). Synthetic data can never make a strategy eligible."""
    cr = criteria or EligibilityCriteria()
    m = wf.oos_metrics
    reasons: list[str] = []
    if wf.synthetic:
        reasons.append("validated on SYNTHETIC data only")
    if m["trades"] < cr.min_oos_trades:
        reasons.append(f"only {m['trades']} out-of-sample trades (min {cr.min_oos_trades})")
    if m["profit_factor"] < cr.min_profit_factor:
        reasons.append(f"OOS profit factor {m['profit_factor']:.2f} < {cr.min_profit_factor}")
    if m["expectancy_r"] <= cr.min_expectancy_r:
        reasons.append(f"OOS expectancy {m['expectancy_r']:.3f}R not positive")
    if wf.efficiency < cr.min_efficiency:
        reasons.append(f"walk-forward efficiency {wf.efficiency:.2f} < {cr.min_efficiency}")
    if wf.monte_carlo.get("runs", 0) and wf.monte_carlo["max_dd_p95"] > cr.max_mc_dd_p95_pct:
        reasons.append(f"Monte Carlo p95 drawdown {wf.monte_carlo['max_dd_p95']:.1f}% too high")
    plateau = float(np.mean([f.plateau_ratio for f in wf.folds])) if wf.folds else 0.0
    if plateau < cr.min_plateau_ratio:
        reasons.append(f"parameter plateau {plateau:.0%} < {cr.min_plateau_ratio:.0%} (fragile parameters)")
    return not reasons, reasons
