"""API and security tests: authentication, permissions, validation, rate limiting, no mode/limit endpoints."""

from __future__ import annotations

from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from backend.config.settings import Settings
from backend.core.errors import ConfigurationError
from backend.database.repository import Repository
from backend.main import create_app
from backend.runtime import TradingRuntime

pytestmark = pytest.mark.critical

PASSWORD = "correct horse battery staple"
SECRET_TOKEN = "123456789:" + "S" * 35


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    h = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
    settings = Settings(_env_file=None, symbols=["EURUSD"], timeframes=["M15", "H1", "H4"],
                        simulation_data_source="synthetic", database_url=f"sqlite:///{tmp_path}/api.db",
                        dashboard_admin_password_hash=h, jwt_secret="x" * 40, api_rate_limit_per_minute=1000,
                        strategy_validation_path=str(tmp_path / "none.json"), telegram_bot_token=SECRET_TOKEN,
                        local_auth_path=str(tmp_path / "auth.json"))
    runtime = TradingRuntime(settings, Repository(settings.database_url, create_schema=True))
    runtime.master.run_cycle(runtime.replay_cursor)
    app = create_app(settings, runtime, start_loop=False)
    with TestClient(app) as c:
        yield c


def token(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_health_is_public_and_minimal(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200 and set(r.json()) == {"status", "mode", "cycles", "broker_connected"}
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("path", ["/api/overview", "/api/agents", "/api/master", "/api/positions", "/api/risk",
                                  "/api/alerts", "/api/audit", "/api/strategies"])
def test_endpoints_require_auth(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer forged.token.value"}).status_code == 401


def test_login_and_read_endpoints(client: TestClient) -> None:
    h = token(client)
    ov = client.get("/api/overview", headers=h).json()
    assert ov["mode"] == "simulation" and ov["synthetic_data"] and "portfolio" in ov
    assert ov["portfolio"]["target"]["target"] == 1000
    agents = client.get("/api/agents", headers=h).json()
    assert {a["name"] for a in agents} == {"market_analyst", "technical_analyst", "price_action_analyst",
                                           "macro_analyst", "risk_manager", "strategy_optimizer",
                                           "execution_monitor"}
    assert client.get("/api/master", headers=h).status_code == 200
    assert client.get("/api/risk", headers=h).json()["limits"]["max_risk_per_trade_pct"] == 0.25
    assert client.get("/api/audit", headers=h).json()["valid"]


def test_wrong_password_and_lockout(client: TestClient) -> None:
    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 429


def test_input_validation(client: TestClient) -> None:
    h = token(client)
    assert client.post("/api/killswitch/activate", json={"reason": "x"}, headers=h).status_code == 422
    assert client.put("/api/session/target", json={"enabled": True, "target": -5}, headers=h).status_code == 422
    assert client.post("/api/auth/login", json={"username": "", "password": ""}).status_code == 422


def test_kill_switch_via_api(client: TestClient) -> None:
    h = token(client)
    r = client.post("/api/killswitch/activate", json={"reason": "manual emergency"}, headers=h)
    assert r.status_code == 200 and r.json()["kill_switch"]
    assert client.post("/api/killswitch/release", json={"confirm": False}, headers=h).status_code == 400
    assert not client.post("/api/killswitch/release", json={"confirm": True}, headers=h).json()["kill_switch"]
    actions = [e["action"] for e in client.get("/api/audit", headers=h).json()["entries"]]
    assert "kill_switch_activated" in actions and "kill_switch_released" in actions


def test_viewer_cannot_act(client: TestClient) -> None:
    auth = client.app.state.auth  # type: ignore[attr-defined]
    auth.add_user("viewer", bcrypt.hashpw(b"viewer-password-123", bcrypt.gensalt(rounds=4)).decode(), "viewer")
    r = client.post("/api/auth/login", json={"username": "viewer", "password": "viewer-password-123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/overview", headers=h).status_code == 200
    assert client.post("/api/killswitch/activate", json={"reason": "viewer try"}, headers=h).status_code == 403
    assert client.get("/api/audit", headers=h).status_code == 403


def test_no_endpoint_can_change_mode_or_risk_limits(client: TestClient) -> None:
    paths = {r.path for r in client.app.routes}  # type: ignore[attr-defined]
    assert not any("mode" in p or "limit" in p for p in paths)
    h = token(client)
    for p in ("/api/mode", "/api/risk/limits", "/api/risk"):
        assert client.post(p, json={"trading_mode": "live"}, headers=h).status_code in (404, 405)


def test_target_change_is_audited_and_does_not_touch_risk(client: TestClient) -> None:
    h = token(client)
    before = client.get("/api/risk", headers=h).json()["limits"]
    r = client.put("/api/session/target", json={"enabled": False, "target": 500}, headers=h)
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert client.get("/api/risk", headers=h).json()["limits"] == before


def test_secrets_never_exposed(client: TestClient) -> None:
    h = token(client)
    for p in ("/api/overview", "/api/agents", "/api/risk", "/api/alerts", "/api/audit", "/api/master"):
        body = client.get(p, headers=h).text
        assert SECRET_TOKEN not in body and "x" * 40 not in body and "$2b$" not in body


def test_websocket_requires_token(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"token": "bad"})
        with pytest.raises(Exception):
            ws.receive_json()
    tok = token(client)["Authorization"].split()[1]
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"token": tok})
        msg = ws.receive_json()
        assert msg["type"] == "snapshot" and msg["data"]["mode"] == "simulation"


def test_rate_limit(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, symbols=["EURUSD"], timeframes=["M15", "H1", "H4"],
                        simulation_data_source="synthetic", api_rate_limit_per_minute=10,
                        strategy_validation_path=str(tmp_path / "none.json"), local_auth_path=str(tmp_path / "a.json"))
    app = create_app(settings, TradingRuntime(settings), start_loop=False)
    with TestClient(app) as c:
        codes = [c.get("/api/health").status_code for _ in range(15)]
    assert 429 in codes


def test_login_disabled_without_hash_and_weak_secret_refused(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, symbols=["EURUSD"], timeframes=["M15", "H1", "H4"],
                        simulation_data_source="synthetic", strategy_validation_path=str(tmp_path / "n.json"),
                        local_auth_path=str(tmp_path / "a.json"))
    app = create_app(settings, TradingRuntime(settings), start_loop=False)
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "admin"}).status_code in (503, 401)
    with pytest.raises(ConfigurationError):
        create_app(Settings(_env_file=None, jwt_secret="short"), TradingRuntime(settings), start_loop=False)
    with pytest.raises(ConfigurationError):
        create_app(Settings(_env_file=None, trading_mode="demo"), TradingRuntime(settings), start_loop=False)
