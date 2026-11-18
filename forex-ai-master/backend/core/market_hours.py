"""Forex market hours and trading-session helpers (all times UTC).

The spot FX market is treated as open from Sunday 22:00 UTC to Friday 22:00 UTC.
Bank holidays are not modelled: the broker's ``tradeable`` flag remains the
final authority in DEMO/LIVE.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta


def is_market_open(now: datetime) -> bool:
    """Return True when the spot FX market is open at ``now`` (UTC)."""
    now = now.astimezone(UTC)
    wd, t = now.weekday(), now.time()
    if wd == 5:  # Saturday
        return False
    if wd == 6:  # Sunday
        return t >= time(22, 0)
    if wd == 4:  # Friday
        return t < time(22, 0)
    return True


def market_session_name(now: datetime) -> str:
    """Name of the dominant liquidity session at ``now`` (UTC)."""
    h = now.astimezone(UTC).hour
    if 12 <= h < 16:
        return "london_newyork_overlap"
    if 7 <= h < 12:
        return "london"
    if 16 <= h < 21:
        return "newyork"
    return "asia"


def parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def trading_session_bounds(now: datetime, start: str, end: str) -> tuple[datetime, datetime]:
    """Return the [start, end) UTC bounds of the configured trading session containing ``now``.

    If ``now`` is outside the window, the bounds of the *next* session are returned.
    Windows crossing midnight (e.g. 22:00 -> 06:00) are supported.
    """
    now = now.astimezone(UTC)
    s, e = parse_hhmm(start), parse_hhmm(end)
    base = datetime.combine(now.date(), s, UTC)
    duration = (datetime.combine(now.date(), e, UTC) - base) % timedelta(days=1)
    if duration == timedelta(0):
        duration = timedelta(days=1)
    for offset in (-1, 0, 1):
        start_dt = base + timedelta(days=offset)
        if start_dt <= now < start_dt + duration:
            return start_dt, start_dt + duration
    next_start = base if base > now else base + timedelta(days=1)
    return next_start, next_start + duration


def in_trading_session(now: datetime, start: str, end: str) -> bool:
    s, e = trading_session_bounds(now, start, end)
    return s <= now.astimezone(UTC) < e


def week_start(now: datetime) -> datetime:
    """Start of the FX trading week (Sunday 22:00 UTC) containing ``now``."""
    now = now.astimezone(UTC)
    days_since_sunday = (now.weekday() + 1) % 7
    candidate = datetime.combine((now - timedelta(days=days_since_sunday)).date(), time(22, 0), UTC)
    if candidate > now:
        candidate -= timedelta(days=7)
    return candidate


def trading_day_start(now: datetime) -> datetime:
    """Start of the FX trading day (22:00 UTC rollover) containing ``now``."""
    now = now.astimezone(UTC)
    candidate = datetime.combine(now.date(), time(22, 0), UTC)
    if candidate > now:
        candidate -= timedelta(days=1)
    return candidate
