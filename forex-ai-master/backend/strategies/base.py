"""Strategy interface.

Strategies are *vectorised and causal*: :meth:`Strategy.generate` returns, for each
bar ``t``, a signal computed from bars ``<= t`` (closed bars). The backtester
executes it at the open of bar ``t+1``; stop and target are expressed as
distances so they are re-anchored on the real fill price.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from backend.analysis.indicators import compute_all
from backend.core.types import Direction, TradeSetup


class Strategy(ABC):
    name: ClassVar[str] = "strategy"
    default_params: ClassVar[dict[str, Any]] = {}
    param_grid: ClassVar[dict[str, list[Any]]] = {}

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"unknown parameters for {self.name}: {sorted(unknown)}")
        self.params = {**self.default_params, **params}

    @abstractmethod
    def _signals(self, ind: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
        """Return (signal in {-1,0,1}, stop distance, target distance) aligned on ``ind``."""

    def generate(self, df: pd.DataFrame, indicators: pd.DataFrame | None = None) -> pd.DataFrame:
        ind = indicators if indicators is not None else compute_all(df)
        sig, stop, target = self._signals(ind)
        valid = sig.ne(0) & stop.gt(0) & target.gt(0) & stop.notna() & target.notna()
        out = pd.DataFrame({"signal": sig.where(valid, 0).fillna(0).astype(int),
                            "stop_dist": stop.where(valid), "target_dist": target.where(valid)}, index=ind.index)
        return out

    def latest_setup(self, symbol: str, df: pd.DataFrame, timeframe: str,
                     indicators: pd.DataFrame | None = None) -> TradeSetup | None:
        """Setup implied by the last closed bar, or ``None``."""
        sig = self.generate(df, indicators)
        last = sig.iloc[-1]
        if last.signal == 0:
            return None
        direction = Direction.LONG if last.signal > 0 else Direction.SHORT
        entry = float(df["close"].iloc[-1])
        stop = entry - direction.sign * float(last.stop_dist)
        tp = entry + direction.sign * float(last.target_dist)
        return TradeSetup(symbol, direction, entry, stop, tp, stop, self.name, timeframe, 0.5,
                          f"{self.name} signal {self.params}")

    @staticmethod
    def _cross_up(a: pd.Series, b: pd.Series) -> pd.Series:
        return (a > b) & (a.shift(1) <= b.shift(1))

    @staticmethod
    def _cross_down(a: pd.Series, b: pd.Series) -> pd.Series:
        return (a < b) & (a.shift(1) >= b.shift(1))

    @staticmethod
    def _combine(long_cond: pd.Series, short_cond: pd.Series) -> pd.Series:
        return pd.Series(np.where(long_cond, 1, np.where(short_cond, -1, 0)), index=long_cond.index)
