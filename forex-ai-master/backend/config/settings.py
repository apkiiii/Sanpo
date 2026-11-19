"""Application configuration.

All values come from environment variables (or a ``.env`` file). Secrets are
held in :class:`pydantic.SecretStr` so they are never printed by accident.

Risk limits are exposed as a *frozen* :class:`RiskLimits` object: once built it
cannot be mutated by any component (including the Master bot).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class TradingMode(StrEnum):
    """Operating mode. LIVE is never selected implicitly."""

    SIMULATION = "simulation"
    DEMO = "demo"
    LIVE = "live"


class TargetReachedPolicy(StrEnum):
    """What to do with open positions once the session target is reached."""

    HOLD = "hold"  # keep existing SL/TP untouched
    BREAKEVEN = "breakeven"  # move stops to break-even when allowed (tightening only)
    CLOSE_ALL = "close_all"  # close all positions at market


class EmergencyClosePolicy(StrEnum):
    """What the kill switch does with open positions."""

    KEEP_WITH_STOPS = "keep_with_stops"  # keep positions, stops stay in place
    CLOSE_ALL = "close_all"  # close everything at market


class RiskLimits(BaseModel):
    """Immutable risk limits. Percentages are expressed in percent (0.25 = 0.25 %)."""

    model_config = ConfigDict(frozen=True)

    max_risk_per_trade_pct: float = Field(0.25, gt=0, le=2.0)
    max_exceptional_risk_per_trade_pct: float = Field(0.50, gt=0, le=2.0)
    max_daily_loss_pct: float = Field(1.0, gt=0, le=10.0)
    max_weekly_loss_pct: float = Field(2.5, gt=0, le=20.0)
    max_drawdown_pct: float = Field(5.0, gt=0, le=50.0)
    max_open_positions: int = Field(3, ge=1, le=20)
    max_total_open_risk_pct: float = Field(0.75, gt=0, le=10.0)
    max_currency_exposure_pct: float = Field(0.50, gt=0, le=10.0)
    max_correlation: float = Field(0.80, gt=0, le=1.0)
    min_reward_risk: float = Field(1.5, gt=0)
    max_spread_pips: float = Field(3.0, gt=0)
    max_spread_to_stop_ratio: float = Field(0.20, gt=0, le=1.0)
    max_slippage_pips: float = Field(2.0, ge=0)
    max_atr_spike_ratio: float = Field(3.0, gt=1.0)
    max_margin_usage_pct: float = Field(30.0, gt=0, le=100.0)
    min_stop_pips: float = Field(5.0, gt=0)

    @model_validator(mode="after")
    def _check_consistency(self) -> "RiskLimits":
        if self.max_exceptional_risk_per_trade_pct < self.max_risk_per_trade_pct:
            raise ValueError("exceptional risk must be >= normal risk per trade")
        if self.max_daily_loss_pct > self.max_drawdown_pct:
            raise ValueError("daily loss limit cannot exceed max drawdown")
        return self


class Settings(BaseSettings):
    """Global settings loaded from the environment."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Mode ---------------------------------------------------------------
    trading_mode: TradingMode = TradingMode.SIMULATION
    live_trading_enabled: bool = False
    live_confirmation_phrase: str = ""
    live_min_demo_trades: int = Field(50, ge=1)
    critical_tests_report_path: str = "var/critical_tests.json"
    critical_tests_max_age_days: int = Field(7, ge=1)
    live_approved_strategies: Annotated[list[str], NoDecode] = Field(default_factory=list)  # explicit operator whitelist

    # --- Account / market ---------------------------------------------------
    account_currency: str = "USD"
    initial_balance: float = Field(100_000.0, gt=0)
    symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]
    )
    timeframes: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["M5", "M15", "M30", "H1", "H4"])
    entry_timeframe: str = "M15"
    # Smallest position and size increment accepted by the broker, in units (1,000 = micro lot).
    min_trade_units: float = Field(1_000.0, ge=1)
    trade_units_step: float = Field(1_000.0, ge=1)
    cycle_interval_seconds: int = Field(30, ge=1)

    # --- Risk (each value overridable through RISK_* variables) ------------
    risk_max_risk_per_trade_pct: float = 0.25
    risk_max_exceptional_risk_per_trade_pct: float = 0.50
    risk_max_daily_loss_pct: float = 1.0
    risk_max_weekly_loss_pct: float = 2.5
    risk_max_drawdown_pct: float = 5.0
    risk_max_open_positions: int = 3
    risk_max_total_open_risk_pct: float = 0.75
    risk_max_currency_exposure_pct: float = 0.50
    risk_max_correlation: float = 0.80
    risk_min_reward_risk: float = 1.5
    risk_max_spread_pips: float = 3.0
    risk_max_spread_to_stop_ratio: float = 0.20
    risk_max_slippage_pips: float = 2.0
    risk_max_atr_spike_ratio: float = 3.0
    risk_max_margin_usage_pct: float = 30.0
    risk_min_stop_pips: float = 5.0

    # --- Session target -----------------------------------------------------
    target_session_profit: float = Field(1000.0, ge=0)
    target_enabled: bool = True
    target_reached_policy: TargetReachedPolicy = TargetReachedPolicy.BREAKEVEN
    session_start_utc: str = "07:00"  # London open
    session_end_utc: str = "21:00"  # New York close (approx.)
    trade_only_in_session: bool = True

    # --- Safety -------------------------------------------------------------
    emergency_close_policy: EmergencyClosePolicy = EmergencyClosePolicy.KEEP_WITH_STOPS
    block_when_calendar_unavailable: bool = True
    news_blackout_minutes_before: int = 30
    news_blackout_minutes_after: int = 30
    max_consecutive_errors: int = 5
    broker_heartbeat_timeout_seconds: int = 60

    # --- Data ---------------------------------------------------------------
    historical_data_dir: str = "data/historical"
    simulation_data_source: str = "csv"  # "csv" (verified history replay) or "synthetic" (pipeline demo only)
    simulation_step_seconds: float = Field(1.0, ge=0.0)
    simulation_autostart: bool = True  # False: the simulated market waits for the "Démarrer" button
    synthetic_bars: int = Field(150_000, ge=30_000)  # M5 bars of generated market (~1.5 years)
    simulation_start: str = ""  # ISO date to start the replay; default = 60 days after data start
    strategy_validation_path: str = "var/strategy_validation.json"
    log_level: str = "INFO"
    economic_calendar_path: str = ""  # path to a verified CSV/JSON export

    # --- Database -----------------------------------------------------------
    database_url: str = "sqlite:///var/forex_ai_master.db"

    # --- Dashboard auth -----------------------------------------------------
    dashboard_admin_username: str = "admin"
    dashboard_admin_password_hash: SecretStr = SecretStr("")
    # Desktop app: password created on first launch from this computer only, stored hashed in this file.
    local_auth_path: str = "var/local_auth.json"
    jwt_secret: SecretStr = SecretStr("")
    jwt_expiry_minutes: int = Field(60, ge=5, le=24 * 60)
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:5173", "http://localhost:8080"])
    api_rate_limit_per_minute: int = Field(120, ge=10)

    # --- Alerts -------------------------------------------------------------
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    alert_email_from: str = ""
    alert_email_to: str = ""

    # --- Broker -------------------------------------------------------------
    broker_name: str = "simulated"
    broker_api_key: SecretStr = SecretStr("")
    broker_account_id: str = ""
    broker_environment: str = "practice"

    @field_validator("symbols", "timeframes", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip().upper() for item in value.split(",") if item.strip()]
        return value

    @field_validator("live_approved_strategies", mode="before")
    @classmethod
    def _split_strategies(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip().lower() for item in value.split(",") if item.strip()]
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    def risk_limits(self) -> RiskLimits:
        """Build the frozen risk limits object from the RISK_* settings."""
        return RiskLimits(
            max_risk_per_trade_pct=self.risk_max_risk_per_trade_pct,
            max_exceptional_risk_per_trade_pct=self.risk_max_exceptional_risk_per_trade_pct,
            max_daily_loss_pct=self.risk_max_daily_loss_pct,
            max_weekly_loss_pct=self.risk_max_weekly_loss_pct,
            max_drawdown_pct=self.risk_max_drawdown_pct,
            max_open_positions=self.risk_max_open_positions,
            max_total_open_risk_pct=self.risk_max_total_open_risk_pct,
            max_currency_exposure_pct=self.risk_max_currency_exposure_pct,
            max_correlation=self.risk_max_correlation,
            min_reward_risk=self.risk_min_reward_risk,
            max_spread_pips=self.risk_max_spread_pips,
            max_spread_to_stop_ratio=self.risk_max_spread_to_stop_ratio,
            max_slippage_pips=self.risk_max_slippage_pips,
            max_atr_spike_ratio=self.risk_max_atr_spike_ratio,
            max_margin_usage_pct=self.risk_max_margin_usage_pct,
            min_stop_pips=self.risk_min_stop_pips,
        )

    def secret_values(self) -> list[str]:
        """Return all configured secret values (used by log/alert redaction)."""
        secrets = [
            self.dashboard_admin_password_hash,
            self.jwt_secret,
            self.telegram_bot_token,
            self.smtp_password,
            self.broker_api_key,
        ]
        return [s.get_secret_value() for s in secrets if s.get_secret_value()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings (cached)."""
    return Settings()
