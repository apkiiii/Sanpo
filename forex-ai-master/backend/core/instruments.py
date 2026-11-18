"""Instrument specifications and currency conversion helpers.

Pip values are computed exactly from prices, never hard-coded, so that the
position size is correct for XXX/USD, USD/XXX and cross pairs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from backend.core.errors import ConfigurationError, DataError


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    symbol: str
    base: str
    quote: str
    pip_size: float
    price_precision: int
    contract_size: float = 100_000.0  # units per standard lot
    min_units: float = 1_000.0  # micro lot
    units_step: float = 1_000.0
    max_units: float = 10_000_000.0
    margin_rate: float = 1.0 / 30.0  # 30:1 (ESMA retail major pairs) by default

    def pips(self, price_distance: float) -> float:
        """Convert a price distance into pips."""
        return price_distance / self.pip_size

    def round_price(self, price: float) -> float:
        return round(price, self.price_precision)

    def round_stop(self, price: float, direction_sign: int) -> float:
        """Round a stop-loss to the price precision *towards the position* (it can only get tighter)."""
        f = 10 ** self.price_precision
        if direction_sign > 0:  # long: stop below entry -> round up
            return math.ceil(price * f - 1e-6) / f
        return math.floor(price * f + 1e-6) / f


def _spec(symbol: str) -> InstrumentSpec:
    base, quote = symbol[:3], symbol[3:]
    jpy = quote == "JPY"
    return InstrumentSpec(
        symbol=symbol,
        base=base,
        quote=quote,
        pip_size=0.01 if jpy else 0.0001,
        price_precision=3 if jpy else 5,
    )


MAJOR_PAIRS: tuple[str, ...] = ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD")

INSTRUMENTS: dict[str, InstrumentSpec] = {s: _spec(s) for s in MAJOR_PAIRS}

_SIZE_RULES = {"min_units": 1_000.0, "units_step": 1_000.0}


def configure_trade_sizes(min_units: float, units_step: float) -> None:
    """Set the smallest tradable size and the size increment (in units) for all instruments.

    Must match what the broker accepts: 1,000 = micro lot (most brokers); some brokers accept 1 unit.
    Called once at start-up from the configuration.
    """
    if units_step < 1 or min_units < units_step or (min_units / units_step) % 1:
        raise ConfigurationError("MIN_TRADE_UNITS must be a multiple of TRADE_UNITS_STEP, both >= 1")
    _SIZE_RULES.update(min_units=float(min_units), units_step=float(units_step))
    for sym, spec in list(INSTRUMENTS.items()):
        INSTRUMENTS[sym] = replace(spec, min_units=float(min_units), units_step=float(units_step))


def get_instrument(symbol: str) -> InstrumentSpec:
    """Return the specification of ``symbol`` (e.g. ``EURUSD``)."""
    symbol = normalize_symbol(symbol)
    if symbol not in INSTRUMENTS:
        if len(symbol) == 6 and symbol.isalpha():
            INSTRUMENTS[symbol] = replace(_spec(symbol), **_SIZE_RULES)
        else:
            raise ConfigurationError(f"unknown instrument: {symbol}")
    return INSTRUMENTS[symbol]


def normalize_symbol(symbol: str) -> str:
    """Normalise ``EUR/USD``, ``EUR_USD`` or ``eurusd`` to ``EURUSD``."""
    return symbol.replace("/", "").replace("_", "").replace("-", "").upper()


def conversion_rate(from_ccy: str, to_ccy: str, mid_prices: dict[str, float]) -> float:
    """Return how many ``to_ccy`` one unit of ``from_ccy`` is worth.

    ``mid_prices`` maps symbols (``EURUSD``) to mid prices. Direct and inverse
    pairs are supported; a missing conversion raises :class:`DataError` rather
    than guessing.
    """
    if from_ccy == to_ccy:
        return 1.0
    direct = f"{from_ccy}{to_ccy}"
    inverse = f"{to_ccy}{from_ccy}"
    if direct in mid_prices and mid_prices[direct] > 0:
        return mid_prices[direct]
    if inverse in mid_prices and mid_prices[inverse] > 0:
        return 1.0 / mid_prices[inverse]
    # one hop through USD
    if "USD" not in (from_ccy, to_ccy):
        return conversion_rate(from_ccy, "USD", mid_prices) * conversion_rate("USD", to_ccy, mid_prices)
    raise DataError(f"no price available to convert {from_ccy} to {to_ccy}")


def pip_value_per_unit(symbol: str, account_ccy: str, mid_prices: dict[str, float]) -> float:
    """Value in account currency of a 1-pip move for 1 unit of ``symbol``."""
    spec = get_instrument(symbol)
    return spec.pip_size * conversion_rate(spec.quote, account_ccy, mid_prices)


def price_move_value(symbol: str, price_distance: float, units: float, account_ccy: str,
                     mid_prices: dict[str, float]) -> float:
    """Value in account currency of ``price_distance`` for ``units`` units."""
    spec = get_instrument(symbol)
    return price_distance * units * conversion_rate(spec.quote, account_ccy, mid_prices)


def margin_required(symbol: str, units: float, account_ccy: str, mid_prices: dict[str, float]) -> float:
    """Approximate margin required in account currency (notional x margin rate)."""
    spec = get_instrument(symbol)
    notional = abs(units) * conversion_rate(spec.base, account_ccy, mid_prices)
    return notional * spec.margin_rate
