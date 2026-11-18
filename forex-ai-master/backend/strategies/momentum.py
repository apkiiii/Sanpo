"""Momentum: MACD histogram zero-cross confirmed by RSI and EMA50."""

from __future__ import annotations

import pandas as pd

from backend.strategies.base import Strategy


class Momentum(Strategy):
    name = "momentum"
    default_params = {"rsi_threshold": 55.0, "atr_stop": 1.5, "rr": 2.0}
    param_grid = {"rsi_threshold": [52.0, 55.0, 60.0], "atr_stop": [1.0, 1.5, 2.0], "rr": [1.5, 2.0, 2.5]}

    def _signals(self, ind: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
        p = self.params
        h = ind["macd_hist"]
        long_c = (h > 0) & (h.shift(1) <= 0) & (ind["rsi"] > p["rsi_threshold"]) & (ind["close"] > ind["ema50"])
        short_c = (h < 0) & (h.shift(1) >= 0) & (ind["rsi"] < 100 - p["rsi_threshold"]) & \
            (ind["close"] < ind["ema50"])
        stop = ind["atr"] * p["atr_stop"]
        return self._combine(long_c, short_c), stop, stop * p["rr"]
