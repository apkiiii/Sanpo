"""Demo account: fictitious deposits/withdrawals, persistence, first-launch setup, served dashboard."""

from __future__ import annotations

import asyncio
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from backend.config.settings import Settings
from backend.core.errors import ConfigurationError, OrderRejectedError
from backend.core.types import ClosedTrade, Direction, OrderRequest, OrderSide, new_id
from backend.database.repository import Repository
from backend.main import create_app
from backend.runtime import TradingRuntime
from backend.trading.portfolio_manager import PortfolioManager
from backend.trading.simulated_broker import SimulatedBroker
from backend.tests.conftest import NOW

pytestmark = pytest.mark.critical


def _settings(tmp_path: Path, **kw: object) -> Settings:
    base = dict(_env_file=None, symbols=["EURUSD"], timeframes=["M15", "H1", "H4"], initial_balance=40,
                simulation_data_source="synthetic", simulation_step_seconds=0.0, synthetic_bars=30_000,
                database_url=f"sqlite:///{tmp_path}/d.db", strategy_validation_path=str(tmp_path / "v.json"),
                local_auth_path=str(tmp_path / "auth.json"), jwt_secret="y" * 40, block_when_calendar_unavailable=False)
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def test_cash_flow_is_never_profit_loss_or_drawdown() -> None:
    pm = PortfolioManager(40.0)
    pm.record_cash_flow(100.0)
    assert pm.balance == 140 and pm.net_deposits == 100
    assert pm.daily_pnl() == 0 and pm.weekly_pnl() == 0 and pm.drawdown_pct == 0
    assert pm.snapshot()["all_time"]["net_realized"] == 0
    pm.record_cash_flow(-120.0)
    assert pm.balance == 20 and pm.drawdown_pct == 0 and pm.daily_pnl() == 0
    t = ClosedTrade("T", "EURUSD", Direction.LONG, 10, 1.1, 1.0, NOW, NOW, -1.0, 0, 0, 0, "stop_loss")
    pm.record_trade(t, NOW)
    pm.equity -= 1.0
    assert pm.daily_pnl() == pytest.approx(-1.0)  # a real loss still counts


def test_withdraw_limited_to_free_margin(broker: SimulatedBroker) -> None:
    broker.deposit(5.0)
    q = broker.get_quote("EURUSD")
    broker.place_order(OrderRequest(new_id("C"), "EURUSD", OrderSide.BUY, 1_000_000, q.bid - 0.002, None))
    with pytest.raises(OrderRejectedError, match="Retrait refusé"):
        broker.withdraw(broker.get_account().balance)
    with pytest.raises(ValueError):
        broker.deposit(-1)


def test_broker_state_roundtrip(broker: SimulatedBroker) -> None:
    broker.deposit(10)
    q = broker.get_quote("EURUSD")
    r = broker.place_order(OrderRequest("CID-X", "EURUSD", OrderSide.BUY, 1000, q.bid - 0.002, q.ask + 0.004))
    state = broker.export_state()
    other = SimulatedBroker()
    other.import_state(state)
    other.connect()
    other.set_mid("EURUSD", q.mid, 0.8, NOW)
    assert other.get_account().balance == pytest.approx(broker.get_account().balance)
    assert [p.position_id for p in other.get_positions()] == [r.position_id]
    assert other.get_order_status("CID-X") is not None  # idempotency survives a restart
    assert other.cash_history() == broker.cash_history()


def test_cash_flow_only_in_simulation(tmp_path: Path) -> None:
    rt = TradingRuntime(_settings(tmp_path))
    rt.broker.environment = "practice"
    rt.master._mode = rt.master._mode.__class__("demo")
    with pytest.raises(ConfigurationError):
        rt.master.cash_flow(10, "admin")


def test_demo_account_persists_across_restart(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    rt = TradingRuntime(s, Repository(s.database_url, create_schema=True))

    async def run() -> None:
        await rt.start()
        while rt.cycles < 3:
            await asyncio.sleep(0.02)
        await rt.stop()

    asyncio.run(run())
    rt.cash_flow(60, "admin")
    cursor = rt.replay_cursor
    rt2 = TradingRuntime(s, Repository(s.database_url))
    rt2.broker.connect()
    assert rt2.broker.get_account().balance == pytest.approx(rt.broker.get_account().balance)
    assert rt2.master.portfolio.net_deposits == 60
    assert rt2.replay_cursor == cursor  # the simulated market resumes where it stopped


def test_api_deposit_withdraw_and_first_launch_setup(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    app = create_app(s, TradingRuntime(s, Repository(s.database_url, create_schema=True)), start_loop=False)
    with TestClient(app) as c:
        assert c.get("/api/auth/status").json() == {"login_enabled": False, "setup_available": True}
        assert c.post("/api/auth/setup", json={"username": "william", "password": "short"}).status_code == 422
        r = c.post("/api/auth/setup", json={"username": "william", "password": "a-long-password"})
        assert r.status_code == 200
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        # setup can never run twice (it would let anyone replace the password)
        assert c.post("/api/auth/setup", json={"username": "intruder", "password": "another-long-pass"}).status_code == 403
        assert "$2" in Path(s.local_auth_path).read_text() and "a-long-password" not in Path(s.local_auth_path).read_text()
        assert c.post("/api/account/deposit", json={"amount": 100}, headers=h).json()["balance"] == pytest.approx(140)
        assert c.post("/api/account/withdraw", json={"amount": 50}, headers=h).json()["balance"] == pytest.approx(90)
        assert c.post("/api/account/withdraw", json={"amount": 10_000}, headers=h).status_code == 409
        assert c.post("/api/account/deposit", json={"amount": -5}, headers=h).status_code == 422
        assert c.post("/api/account/deposit", json={"amount": 5}).status_code == 401
        ov = c.get("/api/overview", headers=h).json()
        assert ov["portfolio"]["net_deposits"] == 50 and len(ov["cash_history"]) == 2
        assert ov["portfolio"]["all_time"]["net_realized"] == 0
        actions = [e["action"] for e in c.get("/api/audit", headers=h).json()["entries"]]
        assert "demo_deposit" in actions and "demo_withdrawal" in actions
    # the stored password is reloaded at the next start
    app2 = create_app(s, TradingRuntime(s, Repository(s.database_url)), start_loop=False)
    with TestClient(app2) as c:
        assert c.get("/api/auth/status").json()["setup_available"] is False
        assert c.post("/api/auth/login", json={"username": "william", "password": "a-long-password"}).status_code == 200


def test_setup_refused_from_another_computer(tmp_path: Path) -> None:
    from backend.api.security import AuthService
    auth = AuthService(_settings(tmp_path))
    assert auth.setup_available("127.0.0.1") and not auth.setup_available("192.168.1.20")
    with pytest.raises(Exception):
        auth.setup("x", "a-long-password", "10.0.0.5")
    assert bcrypt  # imported for parity with other auth tests


def test_dashboard_served_with_its_own_csp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Forex AI Master</title>")
    monkeypatch.setenv("FRONTEND_DIST", str(dist))
    s = _settings(tmp_path)
    with TestClient(create_app(s, TradingRuntime(s), start_loop=False)) as c:
        r = c.get("/")
        assert r.status_code == 200 and "Forex AI Master" in r.text
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert c.get("/api/health").headers["content-security-policy"].startswith("default-src 'none'")
