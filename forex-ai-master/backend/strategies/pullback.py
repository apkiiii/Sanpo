"""Pullback: trade the resumption of a trend after a retracement to the EMA20."""

from __future__ import annotations

import pandas as pd

from backend.strategies.base import Strategy


class Pullback(Strategy):
    name = "pullback"
    default_params = {"rsi_floor": 45.0, "atr_stop": 1.5, "rr": 2.0}
    param_grid = {"rsi_floor": [40.0, 45.0, 50.0], "atr_stop": [1.0, 1.5, 2.0], "rr": [1.5, 2.0, 2.5]}

    def _signals(self, ind: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
        p = self.params
        up = (ind["close"] > ind["ema200"]) & (ind["ema20"] > ind["ema50"]) & (ind["ema50"] > ind["ema200"])
        down = (ind["close"] < ind["ema200"]) & (ind["ema20"] < ind["ema50"]) & (ind["ema50"] < ind["ema200"])
        long_c = up & (ind["low"] <= ind["ema20"]) & (ind["close"] > ind["ema20"]) & (ind["rsi"] > p["rsi_floor"])
        short_c = down & (ind["high"] >= ind["ema20"]) & (ind["close"] < ind["ema20"]) & \
            (ind["rsi"] < 100 - p["rsi_floor"])
        stop = ind["atr"] * p["atr_stop"]
        return self._combine(long_c, short_c), stop, stop * p["rr"]
