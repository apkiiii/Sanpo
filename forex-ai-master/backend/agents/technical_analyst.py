"""BOT 2 — Technical Analyst.

Indicator voting on the entry timeframe. A signal requires at least
``min_confirmations`` independent indicator families to agree and no strong
contradiction; a single indicator is never sufficient.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from backend.agents.base import Agent, MarketContext
from backend.analysis.indicators import compute_all
from backend.analysis.structure import find_swings, rsi_divergence
from backend.core.types import AgentReport, AgentStatus, Direction, Timeframe


class TechnicalAnalyst(Agent):
    name = "technical_analyst"

    def __init__(self, timeframe: Timeframe = Timeframe.M15, min_confirmations: int = 3) -> None:
        super().__init__()
        self.timeframe = timeframe
        self.min_confirmations = max(2, min_confirmations)

    def votes(self, df: Any) -> tuple[dict[str, int], dict[str, Any]]:
        """Return one vote (-1, 0, +1) per indicator family plus the raw values."""
        ind = compute_all(df)
        last, prev = ind.iloc[-1], ind.iloc[-2]
        v: dict[str, int] = {}
        # Trend family: EMA stack
        if last.ema20 > last.ema50 > last.ema200:
            v["ema_stack"] = 1
        elif last.ema20 < last.ema50 < last.ema200:
            v["ema_stack"] = -1
        else:
            v["ema_stack"] = 0
        # Momentum family: MACD histogram sign and direction
        if last.macd_hist > 0 and last.macd_hist >= prev.macd_hist:
            v["macd"] = 1
        elif last.macd_hist < 0 and last.macd_hist <= prev.macd_hist:
            v["macd"] = -1
        else:
            v["macd"] = 0
        # Oscillator family: RSI regime (avoid extremes)
        if 50 < last.rsi < 70:
            v["rsi"] = 1
        elif 30 < last.rsi < 50:
            v["rsi"] = -1
        else:
            v["rsi"] = 0
        # Directional strength: ADX + DI
        if last.adx >= 20:
            v["adx"] = 1 if last.plus_di > last.minus_di else -1
        else:
            v["adx"] = 0
        # Volatility envelope: price relative to Bollinger mid
        v["bollinger"] = 1 if last.close > last.bb_mid else -1 if last.close < last.bb_mid else 0
        # VWAP only when meaningful volume exists (secondary)
        if not np.isnan(last.vwap):
            v["vwap"] = 1 if last.close > last.vwap else -1
        values = {k: (None if (isinstance(val, float) and np.isnan(val)) else round(float(val), 6))
                  for k, val in last[["rsi", "macd", "macd_signal", "macd_hist", "ema20", "ema50", "ema200",
                                      "atr", "adx", "plus_di", "minus_di", "bb_upper", "bb_lower", "vwap"]].items()}
        extremes = {"rsi_overbought": bool(last.rsi >= 70), "rsi_oversold": bool(last.rsi <= 30),
                    "outside_upper_band": bool(last.close > last.bb_upper),
                    "outside_lower_band": bool(last.close < last.bb_lower)}
        divergence = rsi_divergence(ind["close"], ind["rsi"], find_swings(ind, 3, 3))
        return v, {"values": values, "extremes": extremes, "divergence": divergence.value}

    def _analyze(self, ctx: MarketContext) -> AgentReport:
        err = self._check_frame(ctx, self.timeframe)
        if err:
            return self._insufficient(ctx, err)
        df = ctx.candles[self.timeframe]
        votes, info = self.votes(df)
        if any(val is None for k, val in info["values"].items() if k != "vwap"):
            return self._insufficient(ctx, "indicators not yet warmed up")
        bulls = [k for k, x in votes.items() if x > 0]
        bears = [k for k, x in votes.items() if x < 0]
        net = len(bulls) - len(bears)
        reasons: list[str] = [f"bullish: {bulls}", f"bearish: {bears}"]
        contradictory = min(len(bulls), len(bears)) >= 2
        signal = Direction.NEUTRAL
        if not contradictory:
            if len(bulls) >= self.min_confirmations and net >= self.min_confirmations - 1:
                signal = Direction.LONG
            elif len(bears) >= self.min_confirmations and -net >= self.min_confirmations - 1:
                signal = Direction.SHORT
        else:
            reasons.append("contradictory indicator signals")
        div = Direction(info["divergence"])
        if signal is not Direction.NEUTRAL and div is signal.opposite():
            reasons.append(f"RSI divergence against signal ({div.value})")
            signal = Direction.NEUTRAL
        ext = info["extremes"]
        if signal is Direction.LONG and (ext["rsi_overbought"] or ext["outside_upper_band"]):
            reasons.append("long signal into overbought extreme: rejected")
            signal = Direction.NEUTRAL
        if signal is Direction.SHORT and (ext["rsi_oversold"] or ext["outside_lower_band"]):
            reasons.append("short signal into oversold extreme: rejected")
            signal = Direction.NEUTRAL
        support = len(bulls) if signal is Direction.LONG else len(bears) if signal is Direction.SHORT else 0
        confidence = support / len(votes) if signal is not Direction.NEUTRAL else 0.0
        return AgentReport(self.name, ctx.symbol, AgentStatus.OK, signal, round(confidence, 3),
                           f"{signal.value.upper()} ({support}/{len(votes)} indicator families)",
                           {"votes": votes, **info, "contradictory": contradictory, "timeframe": self.timeframe.value},
                           tuple(reasons), ctx.now)
