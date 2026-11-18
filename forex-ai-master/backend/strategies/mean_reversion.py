"""Mean reversion: fade Bollinger band excursions in a ranging (low ADX) market.

The target is the Bollinger mid-line; signals whose target offers less than
``min_rr`` times the stop are discarded rather than stretched.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.strategies.base import Strategy


class MeanReversion(Strategy):
    name = "mean_reversion"
    default_params = {"adx_max": 20.0, "rsi_low": 30.0, "atr_stop": 1.5, "min_rr": 1.5}
    param_grid = {"adx_max": [18.0, 20.0, 25.0], "rsi_low": [25.0, 30.0, 35.0], "atr_stop": [1.0, 1.5, 2.0],
                  "min_rr": [1.5]}

    def _signals(self, ind: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
        p = self.params
        ranging = ind["adx"] < p["adx_max"]
        long_c = ranging & (ind["close"] < ind["bb_lower"]) & (ind["rsi"] < p["rsi_low"])
        short_c = ranging & (ind["close"] > ind["bb_upper"]) & (ind["rsi"] > 100 - p["rsi_low"])
        stop = ind["atr"] * p["atr_stop"]
        target = (ind["bb_mid"] - ind["close"]).abs()
        enough = target >= stop * p["min_rr"]
        sig = self._combine(long_c & enough, short_c & enough)
        return sig, stop, target.where(enough, np.nan)
