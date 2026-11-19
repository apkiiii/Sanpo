"""Start / pause / speed of the trading simulation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.config.settings import Settings
from backend.database.repository import Repository
from backend.main import create_app
from backend.runtime import TradingRuntime

pytestmark = pytest.mark.critical


def _settings(tmp_path: Path, **kw: object) -> Settings:
    base = dict(_env_file=None, symbols=["EURUSD"], timeframes=["M15", "H1", "H4"], initial_balance=40,
                simulation_data_source="synthetic", simulation_step_seconds=0.02, synthetic_bars=30_000,
                simulation_autostart=False, database_url=f"sqlite:///{tmp_path}/s.db",
                strategy_validation_path=str(tmp_path / "v.json"), local_auth_path=str(tmp_path / "auth.json"),
                jwt_secret="z" * 40, block_when_calendar_unavailable=False)
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def test_paused_by_default_then_started(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    rt = TradingRuntime(s, Repository(s.database_url, create_schema=True))
    start_time = rt.replay_cursor

    async def run() -> None:
        await rt.start()
        await asyncio.sleep(0.6)
        assert rt.cycles == 0  # paused: no analysis, no trade, market frozen
        rt.set_running(True, "admin")
        for _ in range(200):
            if rt.cycles >= 3:
                break
            await asyncio.sleep(0.02)
        rt.set_running(False, "admin")
        await asyncio.sleep(0.2)
        frozen = rt.replay_cursor
        await asyncio.sleep(0.5)
        assert rt.replay_cursor == frozen
        await rt.stop()

    asyncio.run(run())
    assert rt.cycles >= 3 and rt.replay_cursor > start_time
    actions = [e.action for e in rt.audit.entries]
    assert "simulation_started" in actions and "simulation_paused" in actions


def test_control_state_survives_restart(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    rt = TradingRuntime(s, Repository(s.database_url, create_schema=True))
    rt.set_running(True, "admin")
    rt.set_speed(0.2, "admin")
    rt2 = TradingRuntime(s, Repository(s.database_url))
    assert rt2.running and rt2.step_seconds == 0.2
    with pytest.raises(ValueError):
        rt2.set_speed(0.001, "admin")


def test_api_buttons(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    app = create_app(s, TradingRuntime(s, Repository(s.database_url, create_schema=True)), start_loop=False)
    with TestClient(app) as c:
        tok = c.post("/api/auth/setup", json={"username": "owner", "password": "a-long-password"}).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        assert c.get("/api/overview", headers=h).json()["simulation"]["running"] is False
        assert c.post("/api/simulation/start").status_code == 401
        assert c.post("/api/simulation/start", headers=h).json()["running"] is True
        assert c.put("/api/simulation/speed", json={"step_seconds": 0.05}, headers=h).json()["step_seconds"] == 0.05
        assert c.put("/api/simulation/speed", json={"step_seconds": 0}, headers=h).status_code == 422
        assert c.post("/api/simulation/pause", headers=h).json()["running"] is False
