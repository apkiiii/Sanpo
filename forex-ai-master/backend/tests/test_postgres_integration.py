"""Integration test against a real PostgreSQL (run with FAM_TEST_POSTGRES_URL=postgresql+psycopg://...).

The schema must have been created with ``alembic upgrade head``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from backend.config.settings import Settings
from backend.database.repository import Repository
from backend.runtime import TradingRuntime

URL = os.environ.get("FAM_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="FAM_TEST_POSTGRES_URL not set")


def test_runtime_on_postgres(tmp_path: Path) -> None:
    assert URL is not None
    repo = Repository(URL)
    with repo.engine.begin() as c:
        for t in ("audit_log", "alerts", "decisions", "orders", "closed_trades", "positions", "system_state"):
            c.execute(text(f"DELETE FROM {t}"))
    settings = Settings(_env_file=None, symbols=["EURUSD", "GBPUSD"], timeframes=["M15", "H1", "H4"],
                        simulation_data_source="synthetic", simulation_step_seconds=0.0, trade_only_in_session=False,
                        block_when_calendar_unavailable=False, database_url=URL,
                        strategy_validation_path=str(tmp_path / "none.json"))
    rt = TradingRuntime(settings, repo)

    async def go() -> None:
        await rt.start()
        while rt.cycles < 20:
            await asyncio.sleep(0.05)
        await rt.stop()

    asyncio.run(go())
    rt.master.safety.activate_kill_switch("pg restart test", "admin")
    rt2 = TradingRuntime(settings, Repository(URL))
    assert rt2.master.safety.state.kill_switch and rt2.audit.verify()[0]
    assert len(rt2.audit.entries) >= len(rt.audit.entries)
