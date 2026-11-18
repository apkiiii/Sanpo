"""Market structure: swing points, support/resistance, BOS/CHoCH, candle patterns.

Swing points are *confirmed* only ``right`` bars after they form; the function
returns, for each bar, the swings known at that bar to avoid lookahead bias.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from backend.core.types import Direction


@dataclass(frozen=True, slots=True)
class Swing:
    index: int  # bar position where the swing extreme occurred
    confirmed_at: int  # bar position from which the swing is known
    price: float
    kind: str  # "high" | "low"


def find_swings(df: pd.DataFrame, left: int = 3, right: int = 3) -> list[Swing]:
    """Fractal swing highs/lows, confirmed ``right`` bars later."""
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    swings: list[Swing] = []
    for i in range(left, n - right):
        window_h = highs[i - left : i + right + 1]
        window_l = lows[i - left : i + right + 1]
        if highs[i] == window_h.max() and np.argmax(window_h) == left:
            swings.append(Swing(i, i + right, float(highs[i]), "high"))
        if lows[i] == window_l.min() and np.argmin(window_l) == left:
            swings.append(Swing(i, i + right, float(lows[i]), "low"))
    return swings


def known_swings(swings: list[Swing], at: int) -> list[Swing]:
    """Swings confirmed at or before bar ``at``."""
    return [s for s in swings if s.confirmed_at <= at]


def support_resistance(swings: list[Swing], tolerance: float, min_touches: int = 2) -> list[dict[str, float]]:
    """Cluster swing prices into horizontal levels.

    Returns levels sorted by strength (number of touches), each as
    ``{"price": float, "touches": int}``.
    """
    prices = sorted(s.price for s in swings)
    clusters: list[list[float]] = []
    for p in prices:
        if clusters and abs(p - np.mean(clusters[-1])) <= tolerance:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    levels = [{"price": float(np.mean(c)), "touches": len(c)} for c in clusters if len(c) >= min_touches]
    return sorted(levels, key=lambda x: x["touches"], reverse=True)


@dataclass(frozen=True, slots=True)
class StructureState:
    trend: Direction  # direction implied by the swing sequence
    last_event: str  # "BOS", "CHOCH" or "NONE"
    event_direction: Direction
    broken_level: float | None
    last_swing_high: float | None
    last_swing_low: float | None


def market_structure(df: pd.DataFrame, swings: list[Swing]) -> StructureState:
    """Determine trend and the most recent Break of Structure / Change of Character.

    * Uptrend: higher highs and higher lows; downtrend: lower highs and lower lows.
    * BOS: close beyond the last swing in the direction of the trend.
    * CHoCH: close beyond the last swing *against* the trend (potential reversal).
    """
    at = len(df) - 1
    sw = known_swings(swings, at)
    hs = [s for s in sw if s.kind == "high"]
    ls = [s for s in sw if s.kind == "low"]
    if len(hs) < 2 or len(ls) < 2:
        return StructureState(Direction.NEUTRAL, "NONE", Direction.NEUTRAL, None,
                              hs[-1].price if hs else None, ls[-1].price if ls else None)
    hh, hl = hs[-1].price > hs[-2].price, ls[-1].price > ls[-2].price
    lh, ll = hs[-1].price < hs[-2].price, ls[-1].price < ls[-2].price
    trend = Direction.LONG if (hh and hl) else Direction.SHORT if (lh and ll) else Direction.NEUTRAL
    last_high, last_low = hs[-1], ls[-1]
    close = float(df["close"].iloc[-1])
    # Only consider closes after the swing was confirmed.
    event, ev_dir, level = "NONE", Direction.NEUTRAL, None
    if close > last_high.price:
        ev_dir, level = Direction.LONG, last_high.price
        event = "BOS" if trend is Direction.LONG else "CHOCH" if trend is Direction.SHORT else "BOS"
    elif close < last_low.price:
        ev_dir, level = Direction.SHORT, last_low.price
        event = "BOS" if trend is Direction.SHORT else "CHOCH" if trend is Direction.LONG else "BOS"
    return StructureState(trend, event, ev_dir, level, last_high.price, last_low.price)


def candle_pattern(df: pd.DataFrame) -> tuple[str, Direction]:
    """Detect the pattern of the last closed candle: engulfing, pin bar (rejection), doji."""
    if len(df) < 2:
        return "none", Direction.NEUTRAL
    o, h, l, c = (float(df[k].iloc[-1]) for k in ("open", "high", "low", "close"))
    po, pc = float(df["open"].iloc[-2]), float(df["close"].iloc[-2])
    rng = h - l
    if rng <= 0:
        return "none", Direction.NEUTRAL
    body = abs(c - o)
    upper_wick, lower_wick = h - max(o, c), min(o, c) - l
    if c > o and pc < po and c >= po and o <= pc and body > abs(pc - po):
        return "bullish_engulfing", Direction.LONG
    if c < o and pc > po and c <= po and o >= pc and body > abs(pc - po):
        return "bearish_engulfing", Direction.SHORT
    if lower_wick >= 2 * body and lower_wick >= 0.6 * rng:
        return "bullish_pin_bar", Direction.LONG
    if upper_wick >= 2 * body and upper_wick >= 0.6 * rng:
        return "bearish_pin_bar", Direction.SHORT
    if body <= 0.1 * rng:
        return "doji", Direction.NEUTRAL
    return "none", Direction.NEUTRAL


def is_consolidating(df: pd.DataFrame, atr_series: pd.Series, lookback: int = 20, factor: float = 3.0) -> bool:
    """True when the last ``lookback`` bars fit in a range narrower than ``factor`` x ATR."""
    if len(df) < lookback or pd.isna(atr_series.iloc[-1]):
        return False
    window = df.iloc[-lookback:]
    return float(window["high"].max() - window["low"].min()) <= factor * float(atr_series.iloc[-1])


def rsi_divergence(close: pd.Series, rsi_series: pd.Series, swings: list[Swing]) -> Direction:
    """Regular divergence between price swings and RSI at those swings.

    Bearish: price higher high, RSI lower high. Bullish: price lower low, RSI higher low.
    """
    at = len(close) - 1
    sw = known_swings(swings, at)
    hs = [s for s in sw if s.kind == "high"][-2:]
    ls = [s for s in sw if s.kind == "low"][-2:]
    r = rsi_series.to_numpy()
    if len(hs) == 2 and not np.isnan(r[hs[0].index]) and not np.isnan(r[hs[1].index]):
        if hs[1].price > hs[0].price and r[hs[1].index] < r[hs[0].index]:
            return Direction.SHORT
    if len(ls) == 2 and not np.isnan(r[ls[0].index]) and not np.isnan(r[ls[1].index]):
        if ls[1].price < ls[0].price and r[ls[1].index] > r[ls[0].index]:
            return Direction.LONG
    return Direction.NEUTRAL
