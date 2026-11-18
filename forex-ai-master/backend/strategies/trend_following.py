"""Trend following: EMA20/EMA50 cross in the direction of EMA200 with ADX confirmation."""

from __future__ import annotations

import pandas as pd

from backend.strategies.base import Strategy


class TrendFollowing(Strategy):
    name = "trend_following"
    default_params = {"adx_min": 20.0, "atr_stop": 2.0, "rr": 2.0}
    param_grid = {"adx_min": [18.0, 20.0, 25.0], "atr_stop": [1.5, 2.0, 2.5], "rr": [1.5, 2.0, 2.5]}

    def _signals(self, ind: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
        p = self.params
        strong = ind["adx"] >= p["adx_min"]
        long_c = self._cross_up(ind["ema20"], ind["ema50"]) & (ind["close"] > ind["ema200"]) & strong
        short_c = self._cross_down(ind["ema20"], ind["ema50"]) & (ind["close"] < ind["ema200"]) & strong
        stop = ind["atr"] * p["atr_stop"]
        return self._combine(long_c, short_c), stop, stop * p["rr"]
