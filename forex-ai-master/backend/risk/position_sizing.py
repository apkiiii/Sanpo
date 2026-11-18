"""Position sizing from a fixed fraction of capital.

Rules:
* the risk base is ``min(balance, equity)`` (never inflated by open profits);
* units are always rounded **down** to the broker step, never up;
* a size below the minimum tradable size returns 0 (the trade is refused);
* sizing never depends on the session target or on past losses.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from backend.core.instruments import get_instrument, price_move_value


@dataclass(frozen=True, slots=True)
class SizingResult:
    units: float
    risk_amount: float  # account currency lost if the stop is hit (excluding slippage)
    risk_pct: float  # in percent of the risk base
    reason: str = ""


def size_position(balance: float, equity: float, risk_pct: float, entry: float, stop: float, symbol: str,
                  account_ccy: str, mid_prices: dict[str, float]) -> SizingResult:
    """Return the number of units risking at most ``risk_pct`` % of capital."""
    base = min(balance, equity)
    if base <= 0:
        return SizingResult(0.0, 0.0, 0.0, "no capital")
    distance = abs(entry - stop)
    if distance <= 0:
        return SizingResult(0.0, 0.0, 0.0, "stop distance is zero")
    spec = get_instrument(symbol)
    budget = base * risk_pct / 100.0
    loss_per_unit = price_move_value(symbol, distance, 1.0, account_ccy, mid_prices)
    raw_units = budget / loss_per_unit
    # tiny epsilon absorbs float noise (e.g. 1.1000-1.0980 = 0.0020000000000000018), still rounding down
    units = math.floor(raw_units / spec.units_step + 1e-6) * spec.units_step
    units = min(units, spec.max_units)
    if units < spec.min_units:
        return SizingResult(0.0, 0.0, 0.0, f"size {raw_units:.0f} below minimum {spec.min_units:.0f} units")
    risk_amount = units * loss_per_unit
    return SizingResult(units, risk_amount, risk_amount / base * 100.0)
