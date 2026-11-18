"""BOT 3 — Price Action Analyst.

Detects breakouts, pullbacks after a Break of Structure, Change of Character,
rejections at support/resistance and consolidation, and turns valid patterns
into a :class:`TradeSetup` with entry, technical stop-loss, take-profit and
invalidation level. When data is insufficient or no clean pattern exists, it
returns no setup — it never fabricates one.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backend.agents.base import Agent, MarketContext
from backend.analysis.indicators import atr
from backend.analysis.structure import (
    candle_pattern,
    find_swings,
    is_consolidating,
    known_swings,
    market_structure,
    support_resistance,
)
from backend.core.types import AgentReport, AgentStatus, Direction, Timeframe, TradeSetup


class PriceActionAnalyst(Agent):
    name = "price_action_analyst"
    min_bars = 120

    def __init__(self, timeframe: Timeframe = Timeframe.M15, target_r: float = 2.0, sl_buffer_atr: float = 0.3) -> None:
        super().__init__()
        self.timeframe = timeframe
        self.target_r = target_r
        self.sl_buffer_atr = sl_buffer_atr

    def _build_setup(self, symbol: str, direction: Direction, entry: float, stop: float, levels: list[float],
                     name: str, confidence: float, rationale: str) -> TradeSetup | None:
        risk = abs(entry - stop)
        if risk <= 0 or (direction is Direction.LONG and stop >= entry) or (direction is Direction.SHORT and stop <= entry):
            return None
        tp = entry + direction.sign * self.target_r * risk
        # If an opposing level sits before the target, use it only if it still offers >= 1.5R.
        blockers = [lv for lv in levels if (lv - entry) * direction.sign > 0 and abs(lv - entry) < abs(tp - entry)]
        if blockers:
            nearest = min(blockers, key=lambda lv: abs(lv - entry))
            if abs(nearest - entry) / risk >= 1.5:
                tp = nearest
            else:
                return None  # obstacle too close: not a valid setup
        return TradeSetup(symbol, direction, entry, stop, tp, stop, name, self.timeframe.value,
                          round(confidence, 3), rationale)

    def find_setup(self, symbol: str, df: pd.DataFrame) -> tuple[TradeSetup | None, dict[str, Any]]:
        a = atr(df)
        last_atr = float(a.iloc[-1])
        close = float(df["close"].iloc[-1])
        swings = find_swings(df, 3, 3)
        known = known_swings(swings, len(df) - 1)
        st = market_structure(df, swings)
        pattern, pattern_dir = candle_pattern(df)
        levels = [lv["price"] for lv in support_resistance(known[-40:], 0.5 * last_atr)]
        consolidating_before = is_consolidating(df.iloc[:-1], a.iloc[:-1], 20, 3.0)
        info: dict[str, Any] = {
            "structure_trend": st.trend.value, "event": st.last_event, "event_direction": st.event_direction.value,
            "pattern": pattern, "pattern_direction": pattern_dir.value, "consolidation": consolidating_before,
            "last_swing_high": st.last_swing_high, "last_swing_low": st.last_swing_low, "atr": last_atr,
            "levels": levels[:6],
        }
        buf = self.sl_buffer_atr * last_atr
        lows = [s.price for s in known if s.kind == "low"]
        highs = [s.price for s in known if s.kind == "high"]

        # 1) Breakout out of a consolidation, strong close.
        prior = df.iloc[-21:-1]
        body = abs(close - float(df["open"].iloc[-1]))
        if consolidating_before and body >= 0.6 * last_atr:
            if close > float(prior["high"].max()):
                stop = float(prior["low"].min() + prior["high"].max()) / 2 - buf
                return self._build_setup(symbol, Direction.LONG, close, stop, levels, "breakout", 0.55,
                                         "close above consolidation range"), info
            if close < float(prior["low"].min()):
                stop = float(prior["low"].min() + prior["high"].max()) / 2 + buf
                return self._build_setup(symbol, Direction.SHORT, close, stop, levels, "breakout", 0.55,
                                         "close below consolidation range"), info

        # 2) Pullback in an established structure with a rejection / engulfing candle.
        if st.trend is Direction.LONG and pattern_dir is Direction.LONG and lows:
            last_low = lows[-1]
            near_level = any(abs(float(df["low"].iloc[-1]) - lv) <= 0.5 * last_atr for lv in levels + [last_low])
            if close > last_low and near_level:
                stop = min(last_low, float(df["low"].iloc[-1])) - buf
                return self._build_setup(symbol, Direction.LONG, close, stop, levels, "pullback", 0.6,
                                         f"bullish {pattern} on pullback in uptrend"), info
        if st.trend is Direction.SHORT and pattern_dir is Direction.SHORT and highs:
            last_high = highs[-1]
            near_level = any(abs(float(df["high"].iloc[-1]) - lv) <= 0.5 * last_atr for lv in levels + [last_high])
            if close < last_high and near_level:
                stop = max(last_high, float(df["high"].iloc[-1])) + buf
                return self._build_setup(symbol, Direction.SHORT, close, stop, levels, "pullback", 0.6,
                                         f"bearish {pattern} on pullback in downtrend"), info

        # 3) Break of structure in trend direction (momentum continuation).
        if st.last_event == "BOS" and st.event_direction is not Direction.NEUTRAL and st.event_direction is st.trend:
            if st.event_direction is Direction.LONG and lows:
                return self._build_setup(symbol, Direction.LONG, close, lows[-1] - buf, levels, "bos_continuation",
                                         0.5, "break of structure to the upside"), info
            if st.event_direction is Direction.SHORT and highs:
                return self._build_setup(symbol, Direction.SHORT, close, highs[-1] + buf, levels, "bos_continuation",
                                         0.5, "break of structure to the downside"), info
        return None, info

    def _analyze(self, ctx: MarketContext) -> AgentReport:
        err = self._check_frame(ctx, self.timeframe)
        if err:
            return self._insufficient(ctx, err)
        setup, info = self.find_setup(ctx.symbol, ctx.candles[self.timeframe])
        if info["event"] == "CHOCH":
            info["warning"] = f"change of character ({info['event_direction']}): trend may be reversing"
        if setup is None:
            return AgentReport(self.name, ctx.symbol, AgentStatus.OK, Direction.NEUTRAL, 0.0, "no valid setup",
                               info, ("no clean price-action pattern",), ctx.now)
        info["setup"] = {"direction": setup.direction.value, "entry": setup.entry, "stop_loss": setup.stop_loss,
                         "take_profit": setup.take_profit, "invalidation": setup.invalidation,
                         "strategy": setup.strategy, "reward_risk": round(setup.reward_risk, 2)}
        conf = setup.confidence * (0.7 if info["event"] == "CHOCH" and info["event_direction"] != setup.direction.value
                                   else 1.0)
        return AgentReport(self.name, ctx.symbol, AgentStatus.OK, setup.direction, round(conf, 3),
                           f"{setup.strategy} {setup.direction.value} RR {setup.reward_risk:.2f}", info,
                           (setup.rationale,), ctx.now)
