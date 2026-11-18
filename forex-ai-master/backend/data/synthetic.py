"""Synthetic OHLC generator — FOR TESTS AND PIPELINE CHECKS ONLY.

Synthetic prices carry ``df.attrs["synthetic"] = True``. Any backtest run on
them is flagged in reports as having **no predictive value**. They exist so the
engine can be exercised end-to-end when no verified historical data is
available (as was the case in the development environment).
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from backend.core.types import Timeframe

DEFAULT_START_PRICES = {
    "EURUSD": 1.08, "GBPUSD": 1.27, "USDJPY": 150.0, "USDCHF": 0.88,
    "AUDUSD": 0.66, "USDCAD": 1.36, "NZDUSD": 0.60,
}


def generate_ohlc(symbol: str, start: datetime, bars: int, timeframe: Timeframe = Timeframe.M5,
                  seed: int = 42, annual_vol: float = 0.08, drift_regimes: bool = True,
                  skip_weekends: bool = True) -> pd.DataFrame:
    """Generate a regime-switching random walk with intrabar high/low.

    Parameters are deliberately simple; the series is *not* a model of real FX.
    """
    rng = np.random.default_rng(seed)
    freq = timeframe.pandas_rule
    idx = pd.date_range(start, periods=int(bars * 1.6) + 10, freq=freq, tz="UTC")
    if skip_weekends:
        wd, hr = idx.weekday, idx.hour
        open_mask = ~((wd == 5) | ((wd == 6) & (hr < 22)) | ((wd == 4) & (hr >= 22)))
        idx = idx[open_mask]
    idx = idx[:bars]
    n = len(idx)
    bars_per_year = 252 * 24 * 60 / timeframe.minutes
    sigma = annual_vol / np.sqrt(bars_per_year)
    drift = np.zeros(n)
    if drift_regimes:
        regime_len = max(50, n // 20)
        for s in range(0, n, regime_len):
            drift[s : s + regime_len] = rng.choice([-1, 0, 0, 1]) * sigma * 0.15
    vol_mult = np.exp(rng.normal(0, 0.25, n))
    rets = drift + sigma * vol_mult * rng.standard_normal(n)
    p0 = DEFAULT_START_PRICES.get(symbol, 1.0)
    close = p0 * np.exp(np.cumsum(rets))
    open_ = np.empty(n)
    open_[0] = p0
    open_[1:] = close[:-1]
    wick = np.abs(rng.normal(0, sigma * 0.6, (2, n))) * close
    high = np.maximum(open_, close) + wick[0]
    low = np.minimum(open_, close) - wick[1]
    volume = rng.integers(50, 500, n).astype(float)
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)
    df.index.name = "time"
    df.attrs["synthetic"] = True
    df.attrs["source"] = f"synthetic(seed={seed})"
    return df


def trending_series(n: int = 300, start: float = 1.10, step: float = 0.0004, noise: float = 0.0002,
                    seed: int = 1, timeframe: Timeframe = Timeframe.H1, direction: int = 1) -> pd.DataFrame:
    """Deterministic trending series with pullbacks — used by unit tests."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-06", periods=n, freq=timeframe.pandas_rule, tz="UTC")
    base = start + direction * step * np.arange(n) + 6 * step * np.sin(np.arange(n) / 6.0)
    close = base + rng.normal(0, noise, n)
    open_ = np.r_[start, close[:-1]]
    high = np.maximum(open_, close) + np.abs(rng.normal(0, noise, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, noise, n))
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                       "volume": np.full(n, 100.0)}, index=idx)
    df.attrs["synthetic"] = True
    return df


def ranging_series(n: int = 300, center: float = 1.10, amplitude: float = 0.002, noise: float = 0.0002,
                   seed: int = 2, timeframe: Timeframe = Timeframe.H1) -> pd.DataFrame:
    """Deterministic oscillating (range-bound) series — used by unit tests."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-06", periods=n, freq=timeframe.pandas_rule, tz="UTC")
    close = center + amplitude * np.sin(np.arange(n) / 4.0) + rng.normal(0, noise, n)
    open_ = np.r_[center, close[:-1]]
    high = np.maximum(open_, close) + np.abs(rng.normal(0, noise, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, noise, n))
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                       "volume": np.full(n, 100.0)}, index=idx)
    df.attrs["synthetic"] = True
    return df
