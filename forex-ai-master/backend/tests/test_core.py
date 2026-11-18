"""Tests for configuration, instruments, market hours and log redaction."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.config.settings import RiskLimits, Settings, TradingMode
from backend.core.errors import DataError
from backend.core.instruments import (
    conversion_rate,
    get_instrument,
    margin_required,
    normalize_symbol,
    pip_value_per_unit,
)
from backend.core.logging import redact, register_secrets
from backend.core.market_hours import (
    in_trading_session,
    is_market_open,
    trading_day_start,
    trading_session_bounds,
    week_start,
)

PRICES = {"EURUSD": 1.10, "USDJPY": 150.0, "GBPUSD": 1.25, "USDCHF": 0.90, "USDCAD": 1.35}


def test_default_mode_is_simulation_and_live_disabled() -> None:
    s = Settings(_env_file=None)
    assert s.trading_mode is TradingMode.SIMULATION
    assert s.live_trading_enabled is False


@pytest.mark.critical
def test_risk_limits_are_frozen() -> None:
    limits = Settings(_env_file=None).risk_limits()
    assert limits.max_risk_per_trade_pct == 0.25
    with pytest.raises(ValidationError):
        limits.max_risk_per_trade_pct = 5.0  # type: ignore[misc]


def test_risk_limits_consistency_checks() -> None:
    with pytest.raises(ValidationError):
        RiskLimits(max_risk_per_trade_pct=0.5, max_exceptional_risk_per_trade_pct=0.25)
    with pytest.raises(ValidationError):
        RiskLimits(max_daily_loss_pct=6, max_drawdown_pct=5)
    with pytest.raises(ValidationError):
        RiskLimits(max_risk_per_trade_pct=10)  # above hard cap of 2 %


def test_symbols_parsed_from_csv() -> None:
    s = Settings(_env_file=None, symbols="eurusd, gbpusd")
    assert s.symbols == ["EURUSD", "GBPUSD"]


def test_instrument_specs() -> None:
    assert get_instrument("EUR/USD").pip_size == 0.0001
    assert get_instrument("USD_JPY").pip_size == 0.01
    assert normalize_symbol("eur-usd") == "EURUSD"


def test_pip_values() -> None:
    assert pip_value_per_unit("EURUSD", "USD", PRICES) == pytest.approx(0.0001)
    # USDJPY: 0.01 JPY per unit -> 0.01/150 USD
    assert pip_value_per_unit("USDJPY", "USD", PRICES) == pytest.approx(0.01 / 150)
    assert pip_value_per_unit("USDCHF", "USD", PRICES) == pytest.approx(0.0001 / 0.90)


def test_conversion_cross_and_missing() -> None:
    assert conversion_rate("EUR", "JPY", PRICES) == pytest.approx(1.10 * 150)
    with pytest.raises(DataError):
        conversion_rate("NZD", "USD", PRICES)


def test_margin_required() -> None:
    assert margin_required("EURUSD", 100_000, "USD", PRICES) == pytest.approx(110_000 / 30)


def test_market_hours() -> None:
    assert not is_market_open(datetime(2026, 11, 14, 12, tzinfo=UTC))  # Saturday
    assert not is_market_open(datetime(2026, 11, 15, 21, 59, tzinfo=UTC))  # Sunday before open
    assert is_market_open(datetime(2026, 11, 15, 22, 0, tzinfo=UTC))
    assert is_market_open(datetime(2026, 11, 18, 3, tzinfo=UTC))
    assert not is_market_open(datetime(2026, 11, 20, 22, 0, tzinfo=UTC))  # Friday close


def test_trading_session_bounds() -> None:
    now = datetime(2026, 11, 18, 10, tzinfo=UTC)
    s, e = trading_session_bounds(now, "07:00", "21:00")
    assert (s.hour, e.hour) == (7, 21) and s.day == 18
    assert in_trading_session(now, "07:00", "21:00")
    assert not in_trading_session(datetime(2026, 11, 18, 22, tzinfo=UTC), "07:00", "21:00")
    # overnight session
    s, e = trading_session_bounds(datetime(2026, 11, 18, 2, tzinfo=UTC), "22:00", "06:00")
    assert s == datetime(2026, 11, 17, 22, tzinfo=UTC) and e == datetime(2026, 11, 18, 6, tzinfo=UTC)


def test_day_and_week_start() -> None:
    now = datetime(2026, 11, 18, 10, tzinfo=UTC)  # Wednesday
    assert trading_day_start(now) == datetime(2026, 11, 17, 22, tzinfo=UTC)
    assert week_start(now) == datetime(2026, 11, 15, 22, tzinfo=UTC)
    assert week_start(datetime(2026, 11, 15, 23, tzinfo=UTC)) == datetime(2026, 11, 15, 22, tzinfo=UTC)


@pytest.mark.critical
def test_secret_redaction() -> None:
    register_secrets(["super-secret-value-123"])
    text = "connect api_key=abcd1234 token: xyz with super-secret-value-123 Bearer abc.def"
    out = redact(text)
    assert "abcd1234" not in out and "xyz" not in out
    assert "super-secret-value-123" not in out and "abc.def" not in out
    assert "https://api.telegram.org/bot123456789:" + "A" * 30 not in redact(
        "https://api.telegram.org/bot123456789:" + "A" * 30 + "/sendMessage"
    )


def test_list_settings_from_real_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: comma-separated lists in .env / environment must parse (they were JSON-decoded)."""
    monkeypatch.setenv("SYMBOLS", "EURUSD, GBPUSD")
    monkeypatch.setenv("TIMEFRAMES", "M15,H1")
    monkeypatch.setenv("CORS_ORIGINS", "http://127.0.0.1:8765")
    monkeypatch.setenv("LIVE_APPROVED_STRATEGIES", "pullback,breakout")
    monkeypatch.setenv("MIN_TRADE_UNITS", "1")
    s = Settings(_env_file=None)
    assert s.symbols == ["EURUSD", "GBPUSD"] and s.timeframes == ["M15", "H1"]
    assert s.cors_origins == ["http://127.0.0.1:8765"] and s.live_approved_strategies == ["pullback", "breakout"]
    assert s.min_trade_units == 1


def test_env_example_parses(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """The shipped .env.example must load as-is."""
    from pathlib import Path
    example = Path(__file__).resolve().parents[2] / ".env.example"
    s = Settings(_env_file=str(example))
    assert s.trading_mode is TradingMode.SIMULATION and len(s.symbols) == 7
