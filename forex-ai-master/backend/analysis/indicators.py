"""Vectorised technical indicators (pandas/NumPy).

All indicators are causal: the value at bar ``t`` uses only bars ``<= t``.
Wilder smoothing is used for RSI, ATR and ADX, matching common platforms.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (span = period). NaN until ``period`` bars exist."""
    out = series.ewm(span=period, adjust=False, min_periods=period).mean()
    return out


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder). Range 0..100."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(avg_loss != 0.0, 100.0)  # only gains -> RSI 100
    return out.where(avg_gain.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD line, signal line and histogram."""
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder)."""
    return _wilder(true_range(df), period)


def bollinger(close: pd.Series, period: int = 20, std: float = 2.0) -> pd.DataFrame:
    mid = sma(close, period)
    dev = close.rolling(period, min_periods=period).std(ddof=0)
    upper, lower = mid + std * dev, mid - std * dev
    width = (upper - lower) / mid
    pct_b = (close - lower) / (upper - lower)
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "width": width, "pct_b": pct_b})


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Average Directional Index with +DI / -DI (Wilder)."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr_s = _wilder(true_range(df), period)
    plus_di = 100.0 * _wilder(plus_dm, period) / tr_s
    minus_di = 100.0 * _wilder(minus_dm, period) / tr_s
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return pd.DataFrame({"adx": _wilder(dx.fillna(0.0), period), "plus_di": plus_di, "minus_di": minus_di})


def efficiency_ratio(close: pd.Series, period: int = 50) -> pd.Series:
    """Kaufman efficiency ratio: |net move| / sum of |bar moves| over ``period`` (0 = noise, 1 = straight line)."""
    net = (close - close.shift(period)).abs()
    path = close.diff().abs().rolling(period, min_periods=period).sum()
    return net / path.replace(0.0, np.nan)


def vwap(df: pd.DataFrame) -> pd.Series | None:
    """Daily-anchored VWAP. Returns ``None`` when volume is absent or meaningless.

    Spot FX has no centralised volume; broker tick volume is only a proxy, so the
    caller must treat VWAP as secondary information.
    """
    if "volume" not in df or df["volume"].fillna(0).sum() <= 0:
        return None
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    day = df.index.floor("1D")
    pv = (typical * df["volume"]).groupby(day).cumsum()
    vol = df["volume"].groupby(day).cumsum()
    return pv / vol.replace(0, np.nan)


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """Return ``df`` enriched with the standard indicator set used by the agents."""
    out = df.copy()
    close = out["close"]
    out["ema20"] = ema(close, 20)
    out["ema50"] = ema(close, 50)
    out["ema200"] = ema(close, 200)
    out["rsi"] = rsi(close, 14)
    m = macd(close)
    out["macd"], out["macd_signal"], out["macd_hist"] = m["macd"], m["signal"], m["hist"]
    out["atr"] = atr(out, 14)
    b = bollinger(close)
    out["bb_upper"], out["bb_mid"], out["bb_lower"], out["bb_width"] = b["upper"], b["mid"], b["lower"], b["width"]
    a = adx(out)
    out["adx"], out["plus_di"], out["minus_di"] = a["adx"], a["plus_di"], a["minus_di"]
    v = vwap(out)
    out["vwap"] = v if v is not None else np.nan
    return out
