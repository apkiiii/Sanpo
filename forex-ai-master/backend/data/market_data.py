"""Market data access, validation and resampling.

Supported sources:

* :class:`CSVMarketDataProvider` — verified historical files (HistData, MetaTrader
  exports or a generic ``time,open,high,low,close[,volume]`` CSV). Supports a
  replay cursor so the simulation never sees future bars.
* :class:`BrokerMarketDataProvider` — candles and quotes from a connected broker.

Every dataset is validated (:func:`validate_ohlc`) before use; agents refuse to
analyse data flagged as invalid or stale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import numpy as np
import pandas as pd

from backend.core.errors import DataError, InsufficientDataError
from backend.core.instruments import normalize_symbol
from backend.core.types import Quote, Timeframe

if TYPE_CHECKING:
    from backend.trading.broker_interface import BrokerInterface

OHLC_COLUMNS = ["open", "high", "low", "close"]


@dataclass(slots=True)
class DataQualityReport:
    ok: bool
    bars: int
    issues: list[str] = field(default_factory=list)
    synthetic: bool = False
    last_bar: datetime | None = None


def validate_ohlc(df: pd.DataFrame, timeframe: Timeframe | None = None, now: datetime | None = None,
                  max_stale_bars: int = 3, min_bars: int = 1) -> DataQualityReport:
    """Check structure, OHLC consistency, NaNs, duplicates, ordering, gaps and staleness."""
    issues: list[str] = []
    synthetic = bool(df.attrs.get("synthetic", False))
    if df is None or len(df) == 0:
        return DataQualityReport(False, 0, ["empty dataset"], synthetic)
    missing = [c for c in OHLC_COLUMNS if c not in df.columns]
    if missing:
        return DataQualityReport(False, len(df), [f"missing columns: {missing}"], synthetic)
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        issues.append("index must be a timezone-aware DatetimeIndex")
    if df[OHLC_COLUMNS].isna().any().any():
        issues.append(f"{int(df[OHLC_COLUMNS].isna().any(axis=1).sum())} bars with NaN prices")
    if (df[OHLC_COLUMNS] <= 0).any().any():
        issues.append("non-positive prices")
    bad = (df["high"] < df[["open", "close", "low"]].max(axis=1)) | (df["low"] > df[["open", "close", "high"]].min(axis=1))
    if bad.any():
        issues.append(f"{int(bad.sum())} bars with inconsistent high/low")
    if df.index.has_duplicates:
        issues.append("duplicate timestamps")
    if not df.index.is_monotonic_increasing:
        issues.append("timestamps not sorted")
    if len(df) < min_bars:
        issues.append(f"only {len(df)} bars, {min_bars} required")
    last = df.index[-1].to_pydatetime() if isinstance(df.index, pd.DatetimeIndex) else None
    if timeframe is not None and now is not None and last is not None:
        if now - last > timedelta(minutes=timeframe.minutes * (max_stale_bars + 1)):
            issues.append(f"stale data: last bar {last.isoformat()}")
    return DataQualityReport(not issues, len(df), issues, synthetic, last)


def resample_ohlc(df: pd.DataFrame, timeframe: Timeframe) -> pd.DataFrame:
    """Aggregate candles to a higher timeframe (left-labelled, closed-left)."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df.columns:
        agg["volume"] = "sum"
    out = df.resample(timeframe.pandas_rule, label="left", closed="left").agg(agg).dropna(subset=["open"])
    out.attrs = dict(df.attrs)
    return out


def drop_incomplete_last_bar(df: pd.DataFrame, timeframe: Timeframe, now: datetime) -> pd.DataFrame:
    """Remove the last bar if it is still forming at ``now`` (signals use closed bars only)."""
    if len(df) and df.index[-1] + timedelta(minutes=timeframe.minutes) > now:
        return df.iloc[:-1]
    return df


def load_csv(path: str | Path) -> pd.DataFrame:
    """Load an OHLC CSV in one of the supported formats and return a UTC-indexed frame.

    Formats detected automatically:

    * Generic: header with ``time``/``datetime``/``date`` and ``open,high,low,close[,volume]``.
    * HistData ASCII: ``YYYYMMDD HHMMSS;open;high;low;close;volume`` (no header, EST without DST:
      converted to UTC by adding 5 hours, as documented by HistData).
    * MetaTrader 5 export: ``<DATE>\t<TIME>\t<OPEN>...`` (assumed already in UTC; check your broker).
    """
    path = Path(path)
    head = path.read_text(encoding="utf-8", errors="replace").splitlines()[:2]
    if not head:
        raise DataError(f"empty file: {path}")
    first = head[0]
    if first.startswith("<DATE>"):
        raw = pd.read_csv(path, sep="\t")
        raw.columns = [c.strip("<>").lower() for c in raw.columns]
        idx = pd.to_datetime(raw["date"] + " " + raw["time"], format="%Y.%m.%d %H:%M:%S", utc=True)
        vol = raw["tickvol"] if "tickvol" in raw else raw.get("vol", 0)
        df = pd.DataFrame({"open": raw["open"], "high": raw["high"], "low": raw["low"], "close": raw["close"],
                           "volume": vol}).set_index(idx)
    elif ";" in first and first[:8].isdigit():
        raw = pd.read_csv(path, sep=";", header=None, names=["dt", "open", "high", "low", "close", "volume"])
        idx = pd.to_datetime(raw["dt"], format="%Y%m%d %H%M%S") + pd.Timedelta(hours=5)
        df = raw.drop(columns=["dt"]).set_index(idx.dt.tz_localize("UTC"))
    else:
        raw = pd.read_csv(path)
        raw.columns = [c.strip().lower() for c in raw.columns]
        tcol = next((c for c in ("time", "datetime", "timestamp", "date") if c in raw.columns), None)
        if tcol is None:
            raise DataError(f"no time column in {path}")
        idx = pd.to_datetime(raw[tcol], utc=True)
        cols = [c for c in ("open", "high", "low", "close", "volume") if c in raw.columns]
        df = raw[cols].set_index(idx)
    df = df.astype(float).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df.index.name = "time"
    df.attrs["source"] = str(path)
    return df


class MarketDataProvider(Protocol):
    """Read-only access to candles and quotes."""

    def get_candles(self, symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame: ...

    def get_quote(self, symbol: str) -> Quote: ...


class CSVMarketDataProvider:
    """Serve candles from historical files with a replay cursor (no future data leakage).

    Files are looked up as ``<dir>/<SYMBOL>_<TF>.csv`` first, then a base file
    ``<dir>/<SYMBOL>_M1.csv`` / ``<SYMBOL>_M5.csv`` which is resampled.
    """

    def __init__(self, data_dir: str | Path, spread_pips: dict[str, float] | None = None) -> None:
        self.data_dir = Path(data_dir)
        self._cache: dict[tuple[str, Timeframe], pd.DataFrame] = {}
        self._frames: dict[str, pd.DataFrame] = {}
        self.cursor: datetime | None = None
        self.spread_pips = spread_pips or {}

    def add_frame(self, symbol: str, df: pd.DataFrame) -> None:
        """Register an in-memory base frame (M5 or lower) for ``symbol``."""
        self._frames[normalize_symbol(symbol)] = df
        self._cache = {k: v for k, v in self._cache.items() if k[0] != normalize_symbol(symbol)}

    def _base(self, symbol: str) -> pd.DataFrame:
        if symbol in self._frames:
            return self._frames[symbol]
        for tf in ("M1", "M5"):
            p = self.data_dir / f"{symbol}_{tf}.csv"
            if p.exists():
                self._frames[symbol] = load_csv(p)
                return self._frames[symbol]
        raise DataError(f"no historical data for {symbol} in {self.data_dir}")

    def _full(self, symbol: str, timeframe: Timeframe) -> pd.DataFrame:
        key = (symbol, timeframe)
        if key not in self._cache:
            direct = self.data_dir / f"{symbol}_{timeframe.value}.csv"
            if symbol not in self._frames and direct.exists():
                self._cache[key] = load_csv(direct)
            else:
                base = self._base(symbol)
                self._cache[key] = resample_ohlc(base, timeframe)
        return self._cache[key]

    def get_candles(self, symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame:
        symbol = normalize_symbol(symbol)
        df = self._full(symbol, timeframe)
        if self.cursor is not None:
            df = df.loc[: self.cursor - timedelta(minutes=timeframe.minutes)]  # closed bars only
        if len(df) == 0:
            raise InsufficientDataError(f"no candles for {symbol} {timeframe} before cursor")
        out = df.iloc[-count:]
        out.attrs = dict(df.attrs)
        return out

    def get_quote(self, symbol: str) -> Quote:
        symbol = normalize_symbol(symbol)
        base = self._base(symbol)
        if self.cursor is not None:
            base = base.loc[: self.cursor]
        if len(base) == 0:
            raise InsufficientDataError(f"no price for {symbol}")
        from backend.core.instruments import get_instrument

        mid = float(base["close"].iloc[-1])
        half = self.spread_pips.get(symbol, 1.0) * get_instrument(symbol).pip_size / 2
        return Quote(symbol, mid - half, mid + half, base.index[-1].to_pydatetime())

    def symbols(self) -> list[str]:
        return sorted(set(self._frames) | {p.name.split("_")[0] for p in self.data_dir.glob("*_*.csv")})

    def time_range(self, symbol: str) -> tuple[datetime, datetime]:
        base = self._base(normalize_symbol(symbol))
        return base.index[0].to_pydatetime(), base.index[-1].to_pydatetime()


class BrokerMarketDataProvider:
    """Adapter exposing a :class:`BrokerInterface` as a :class:`MarketDataProvider`."""

    def __init__(self, broker: "BrokerInterface") -> None:
        self.broker = broker

    def get_candles(self, symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame:
        return self.broker.get_market_data(symbol, timeframe, count)

    def get_quote(self, symbol: str) -> Quote:
        return self.broker.get_quote(symbol)


def returns_correlation(frames: dict[str, pd.DataFrame], lookback: int = 200) -> pd.DataFrame:
    """Pearson correlation of close-to-close log returns over the last ``lookback`` bars."""
    rets = {}
    for sym, df in frames.items():
        if len(df) > 2:
            rets[sym] = np.log(df["close"]).diff().iloc[-lookback:]
    if not rets:
        return pd.DataFrame()
    return pd.DataFrame(rets).dropna(how="all").corr()
