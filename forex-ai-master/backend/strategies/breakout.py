"""Breakout: Donchian channel break after a Bollinger squeeze."""

from __future__ import annotations

import pandas as pd

from backend.strategies.base import Strategy


class Breakout(Strategy):
    name = "breakout"
    default_params = {"lookback": 20, "squeeze_quantile": 0.3, "atr_stop": 1.5, "rr": 2.0}
    param_grid = {"lookback": [20, 30, 40], "squeeze_quantile": [0.2, 0.3, 0.4], "atr_stop": [1.0, 1.5, 2.0],
                  "rr": [1.5, 2.0, 2.5]}

    def _signals(self, ind: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
        p = self.params
        n = int(p["lookback"])
        upper = ind["high"].rolling(n).max().shift(1)
        lower = ind["low"].rolling(n).min().shift(1)
        width_q = ind["bb_width"].rolling(200, min_periods=100).quantile(p["squeeze_quantile"])
        squeeze = (ind["bb_width"].shift(1) <= width_q.shift(1))
        long_c = (ind["close"] > upper) & squeeze
        short_c = (ind["close"] < lower) & squeeze
        stop = ind["atr"] * p["atr_stop"]
        return self._combine(long_c, short_c), stop, stop * p["rr"]
