"""BOT 1 — Market Analyst.

Multi-timeframe market reading: trend, regime (trend vs range), support /
resistance, breakouts, volatility and liquidity zones (equal highs / lows).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from backend.agents.base import Agent, MarketContext
from backend.analysis.indicators import adx, atr, efficiency_ratio, ema
from backend.analysis.structure import find_swings, is_consolidating, market_structure, support_resistance
from backend.core.types import AgentReport, AgentStatus, Direction, Timeframe

TF_WEIGHTS: dict[Timeframe, float] = {
    Timeframe.H4: 0.30, Timeframe.H1: 0.25, Timeframe.M30: 0.20, Timeframe.M15: 0.15, Timeframe.M5: 0.10,
}


class MarketAnalyst(Agent):
    name = "market_analyst"

    def analyze_timeframe(self, df: pd.DataFrame) -> dict[str, Any]:
        """Analyse one timeframe and return a structured dictionary."""
        close = df["close"]
        e50, e200 = ema(close, 50), ema(close, 200)
        a = atr(df)
        adx_df = adx(df)
        last_close, last_atr = float(close.iloc[-1]), float(a.iloc[-1])
        swings = find_swings(df, 3, 3)
        structure = market_structure(df, swings)

        # Trend score in [-1, 1] from three independent components.
        score = 0.0
        score += 0.35 if last_close > e200.iloc[-1] else -0.35
        slope = (e50.iloc[-1] - e50.iloc[-10]) / last_atr if last_atr > 0 else 0.0
        score += 0.35 * float(np.clip(slope, -1, 1))
        score += 0.30 * structure.trend.sign

        adx_val = float(adx_df["adx"].iloc[-1])
        er = float(efficiency_ratio(close, 50).iloc[-1])
        consolidating = is_consolidating(df, a)
        if adx_val >= 25 and er >= 0.3 and not consolidating:
            regime = "trending"
        elif adx_val < 20 or er < 0.15 or consolidating:
            regime = "ranging"
        else:
            regime = "transition"

        prior = df.iloc[-21:-1]
        breakout = Direction.NEUTRAL
        if last_close > float(prior["high"].max()):
            breakout = Direction.LONG
        elif last_close < float(prior["low"].min()):
            breakout = Direction.SHORT

        atr_hist = a.iloc[-200:].dropna()
        atr_pct = float((atr_hist < last_atr).mean()) if len(atr_hist) else 0.5
        atr_spike = float(last_atr / atr_hist.iloc[-100:].median()) if len(atr_hist) else 1.0

        recent = [s for s in swings if s.confirmed_at <= len(df) - 1][-40:]
        levels = support_resistance(recent, tolerance=0.5 * last_atr)
        supports = sorted([lv["price"] for lv in levels if lv["price"] < last_close], reverse=True)[:3]
        resistances = sorted([lv["price"] for lv in levels if lv["price"] > last_close])[:3]
        liq = support_resistance(recent, tolerance=0.15 * last_atr, min_touches=2)
        liquidity = [lv["price"] for lv in liq][:4]

        return {
            "trend_score": round(float(np.clip(score, -1, 1)), 3),
            "structure_trend": structure.trend.value,
            "structure_event": structure.last_event,
            "regime": regime,
            "adx": round(adx_val, 2),
            "efficiency_ratio": round(er, 3),
            "breakout": breakout.value,
            "atr": last_atr,
            "atr_percentile": round(atr_pct, 3),
            "atr_spike_ratio": round(atr_spike, 3),
            "supports": supports,
            "resistances": resistances,
            "liquidity_zones": liquidity,
        }

    def _analyze(self, ctx: MarketContext) -> AgentReport:
        per_tf: dict[str, dict[str, Any]] = {}
        problems: list[str] = []
        for tf in TF_WEIGHTS:
            if tf not in ctx.candles:
                continue
            err = self._check_frame(ctx, tf)
            if err:
                problems.append(err)
                continue
            per_tf[tf.value] = self.analyze_timeframe(ctx.candles[tf])
        if len(per_tf) < 2:
            return self._insufficient(ctx, "need at least two valid timeframes: " + "; ".join(problems))

        total_w = sum(TF_WEIGHTS[Timeframe(k)] for k in per_tf)
        score = sum(TF_WEIGHTS[Timeframe(k)] * v["trend_score"] for k, v in per_tf.items()) / total_w
        signs = [np.sign(v["trend_score"]) for v in per_tf.values() if abs(v["trend_score"]) > 0.2]
        agreement = abs(sum(signs)) / len(signs) if signs else 0.0

        higher = [per_tf[t]["trend_score"] for t in ("H4", "H1") if t in per_tf]
        higher_conflict = any(np.sign(h) != np.sign(score) and abs(h) > 0.3 for h in higher)

        reasons = [f"weighted multi-TF trend score {score:+.2f}", f"timeframe agreement {agreement:.0%}"]
        signal = Direction.NEUTRAL
        if abs(score) >= 0.3 and not higher_conflict:
            signal = Direction.LONG if score > 0 else Direction.SHORT
        elif higher_conflict:
            reasons.append("higher timeframe opposes the aggregate trend")
        regimes = {v["regime"] for v in per_tf.values()}
        confidence = float(np.clip(abs(score) * (0.5 + 0.5 * agreement), 0, 1)) if signal is not Direction.NEUTRAL else 0.0
        summary = f"{signal.value.upper()} bias, regimes={sorted(regimes)}"
        return AgentReport(self.name, ctx.symbol, AgentStatus.OK, signal, round(confidence, 3), summary,
                           {"timeframes": per_tf, "aggregate_score": round(score, 3), "agreement": agreement,
                            "problems": problems}, tuple(reasons), ctx.now)
