"""Indicator and market-structure tests (reference values computed independently)."""

import numpy as np
import pandas as pd
import pytest

from backend.analysis.indicators import adx, atr, bollinger, ema, macd, rsi, vwap
from backend.analysis.structure import candle_pattern, find_swings, market_structure, support_resistance
from backend.core.types import Direction
from backend.data.synthetic import ranging_series, trending_series


def _frame(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2025-01-06", periods=len(closes), freq="1h", tz="UTC")
    c = np.array(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.001, "low": np.minimum(o, c) - 0.001, "close": c},
                        index=idx)


def _rsi_reference(closes: np.ndarray, period: int = 14) -> float:
    """Straightforward loop implementation of Wilder's RSI."""
    deltas = np.diff(closes)
    gains, losses = np.clip(deltas, 0, None), -np.clip(deltas, None, 0)
    ag, al = gains[0], losses[0]
    a = 1 / period
    for g, l in zip(gains[1:], losses[1:], strict=True):
        ag = ag * (1 - a) + g * a
        al = al * (1 - a) + l * a
    return 100 - 100 / (1 + ag / al)


def test_rsi_matches_reference_loop() -> None:
    rng = np.random.default_rng(0)
    closes = 1.1 + np.cumsum(rng.normal(0, 0.001, 300))
    assert rsi(pd.Series(closes)).iloc[-1] == pytest.approx(_rsi_reference(closes), rel=1e-6)


def test_rsi_bounds_and_monotonic() -> None:
    up = pd.Series(np.linspace(1.0, 2.0, 50))
    assert rsi(up).iloc[-1] == pytest.approx(100.0)
    rng = np.random.default_rng(1)
    r = rsi(pd.Series(1 + np.cumsum(rng.normal(0, 0.01, 500)))).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_ema_converges_and_is_causal() -> None:
    s = pd.Series(np.r_[np.zeros(10), np.ones(200)])
    e = ema(s, 20)
    assert e.iloc[-1] == pytest.approx(1.0, abs=1e-6)
    # causality: changing the future does not change the past
    s2 = s.copy()
    s2.iloc[-1] = 100
    assert ema(s2, 20).iloc[-2] == e.iloc[-2]


def test_atr_constant_range() -> None:
    idx = pd.date_range("2025-01-01", periods=60, freq="1h", tz="UTC")
    df = pd.DataFrame({"open": 1.0, "high": 1.002, "low": 0.998, "close": 1.0}, index=idx)
    assert atr(df).iloc[-1] == pytest.approx(0.004)


def test_macd_and_bollinger_shapes() -> None:
    s = pd.Series(np.linspace(1, 2, 100))
    m = macd(s)
    assert m["macd"].iloc[-1] > 0
    pd.testing.assert_series_equal(m["hist"], m["macd"] - m["signal"], check_names=False)
    b = bollinger(s)
    valid = b.dropna()
    assert (valid["upper"] >= valid["lower"]).all()


def test_adx_trend_vs_range() -> None:
    assert adx(trending_series(400))["adx"].iloc[-1] > adx(ranging_series(400))["adx"].iloc[-1]


def test_vwap_none_without_volume() -> None:
    df = _frame([1.0, 1.1, 1.2])
    assert vwap(df) is None
    df["volume"] = [1.0, 1.0, 2.0]
    assert vwap(df) is not None


def test_swings_confirmed_with_delay() -> None:
    df = _frame([1, 2, 3, 4, 5, 4, 3, 2, 1, 2, 3])
    swings = find_swings(df, 2, 2)
    high = [s for s in swings if s.kind == "high"][0]
    assert high.index == 4 and high.confirmed_at == 6


def test_market_structure_uptrend() -> None:
    df = trending_series(300, direction=1)
    st = market_structure(df, find_swings(df, 3, 3))
    assert st.trend is Direction.LONG


def test_support_resistance_clusters() -> None:
    df = ranging_series(300, amplitude=0.003, noise=0.00005)
    levels = support_resistance(find_swings(df, 3, 3), tolerance=0.0005)
    assert len(levels) >= 2 and levels[0]["touches"] >= 2


def test_candle_patterns() -> None:
    idx = pd.date_range("2025-01-01", periods=2, freq="1h", tz="UTC")
    engulf = pd.DataFrame({"open": [1.010, 1.004], "high": [1.011, 1.013], "low": [1.004, 1.003],
                           "close": [1.005, 1.012]}, index=idx)
    assert candle_pattern(engulf) == ("bullish_engulfing", Direction.LONG)
    pin = pd.DataFrame({"open": [1.0, 1.0090], "high": [1.01, 1.0100], "low": [0.99, 1.0000],
                        "close": [1.0, 1.0095]}, index=idx)
    assert candle_pattern(pin)[0] == "bullish_pin_bar"
