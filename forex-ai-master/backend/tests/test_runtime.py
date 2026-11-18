"""End-to-end runtime: synthetic replay through the full stack with a real database, then restart."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.config.settings import Settings
from backend.core.errors import ConfigurationError
from backend.database.repository import Repository
from backend.runtime import TradingRuntime

pytestmark = pytest.mark.critical


def _settings(tmp_path: Path, **kw: object) -> Settings:
    base = dict(_env_file=None, symbols=["EURUSD", "USDJPY"], timeframes=["M15", "H1", "H4"],
                simulation_data_source="synthetic", simulation_step_seconds=0.0, trade_only_in_session=False,
                block_when_calendar_unavailable=False, database_url=f"sqlite:///{tmp_path}/t.db",
                strategy_validation_path=str(tmp_path / "none.json"))
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


async def _run_cycles(rt: TradingRuntime, n: int) -> None:
    await rt.start()
    for _ in range(400):
        if rt.cycles >= n:
            break
        await asyncio.sleep(0.02)
    await rt.stop()


def test_runtime_replay_and_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = Repository(settings.database_url, create_schema=True)
    rt = TradingRuntime(settings, repo)
    assert rt.synthetic and any(a["title"] == "SYNTHETIC DATA" for a in rt.alerts.recent())
    q = rt.subscribe()
    asyncio.run(_run_cycles(rt, 5))
    assert rt.cycles >= 5
    snap = rt.snapshot()
    assert snap["mode"] == "simulation" and snap["synthetic_data"] and snap["replay_time"]
    assert snap["last_cycle"]["errors"] == []
    assert not q.empty()
    assert rt.master.agents_view()[0]["runs"] >= 5
    # audit persisted with an intact hash chain
    entries = repo.load_audit()
    assert {"restart_restore", "startup"} <= {e.action for e in entries}
    # restart: a new runtime on the same database verifies and continues the chain
    rt.master.safety.activate_kill_switch("persist me", "admin")
    rt2 = TradingRuntime(settings, Repository(settings.database_url))
    assert rt2.master.safety.state.kill_switch
    assert rt2.audit.verify()[0]


def test_runtime_refuses_missing_history(tmp_path: Path) -> None:
    settings = _settings(tmp_path, simulation_data_source="csv", historical_data_dir=str(tmp_path / "empty"))
    with pytest.raises(ConfigurationError, match="no historical data"):
        TradingRuntime(settings)


def test_runtime_refuses_demo_without_connector(tmp_path: Path) -> None:
    settings = _settings(tmp_path, trading_mode="demo", broker_name="somebroker")
    with pytest.raises(ConfigurationError, match="No connector implemented"):
        TradingRuntime(settings)


def test_tampered_audit_log_is_detected(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = Repository(settings.database_url, create_schema=True)
    TradingRuntime(settings, repo)
    from backend.database.models import AuditRecord
    with repo.session() as s, s.begin():
        row = s.get(AuditRecord, 1)
        assert row is not None
        row.details = {"tampered": True}
    with pytest.raises(ConfigurationError, match="audit log integrity"):
        TradingRuntime(settings, Repository(settings.database_url))


def test_replay_quote_never_uses_a_forming_bar() -> None:
    """Regression (look-ahead): at time t, the quote must come from the bar that closed at t, not the one opening at t."""
    from datetime import UTC, datetime, timedelta

    from backend.core.types import Timeframe
    from backend.data.market_data import CSVMarketDataProvider
    from backend.data.synthetic import generate_ohlc

    df = generate_ohlc("EURUSD", datetime(2026, 1, 5, tzinfo=UTC), 500, Timeframe.M5, seed=1)
    p = CSVMarketDataProvider("/nonexistent")
    p.add_frame("EURUSD", df)
    t = df.index[100].to_pydatetime()
    p.cursor = t
    assert p.get_quote("EURUSD").mid == pytest.approx(float(df["close"].iloc[99]))
    assert p.bar_path("EURUSD")[-1] == pytest.approx(float(df["close"].iloc[99]))
    assert p.get_candles("EURUSD", Timeframe.M5, 10).index[-1] == df.index[99]
    _ = timedelta


def test_synthetic_replay_is_on_the_bar_grid(tmp_path: Path) -> None:
    rt = TradingRuntime(_settings(tmp_path))
    assert rt.replay_cursor is not None
    for _ in range(5):
        t = rt._advance_replay()
        assert t is not None and t.second == 0 and t.microsecond == 0 and t.minute % 5 == 0
