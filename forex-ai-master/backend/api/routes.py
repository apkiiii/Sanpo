"""REST API for the dashboard.

There is deliberately **no** endpoint to change the trading mode or the risk
limits: those come only from the configuration at start-up.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from backend.api.security import Principal, current_user, get_auth, require_admin
from backend.runtime import TradingRuntime

router = APIRouter(prefix="/api")


def rt(request: Request) -> TradingRuntime:
    return request.app.state.runtime  # type: ignore[no-any-return]


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class KillSwitchBody(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


class ConfirmBody(BaseModel):
    confirm: bool


class RiskOffBody(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class TargetBody(BaseModel):
    enabled: bool
    target: float = Field(ge=0, le=1_000_000)


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    r = rt(request)
    return {"status": "ok", "mode": r.settings.trading_mode.value, "cycles": r.cycles,
            "broker_connected": r.broker.is_connected()}


@router.post("/auth/login")
def login(body: LoginBody, request: Request) -> dict[str, Any]:
    client = request.client.host if request.client else "unknown"
    token = get_auth(request).authenticate(body.username, body.password, client)
    rt(request).audit.record(body.username, "login", {"client": client})
    return {"access_token": token, "token_type": "bearer",
            "expires_in": rt(request).settings.jwt_expiry_minutes * 60}


@router.get("/me")
def me(user: Principal = Depends(current_user)) -> dict[str, str]:
    return {"username": user.username, "role": user.role}


@router.get("/overview")
def overview(request: Request, _: Principal = Depends(current_user)) -> dict[str, Any]:
    return rt(request).snapshot()


@router.get("/agents")
def agents(request: Request, _: Principal = Depends(current_user)) -> list[dict[str, Any]]:
    return rt(request).master.agents_view()


@router.get("/master")
def master(request: Request, _: Principal = Depends(current_user)) -> dict[str, Any]:
    m = rt(request).master
    decisions = [d.to_dict() for d in list(m.decisions)[:200]]
    return {"last_cycle": m.status()["last_cycle"],
            "accepted": [d for d in decisions if d["status"] in ("executed", "proposed")],
            "rejected": [d for d in decisions if d["status"] in ("rejected_master", "rejected_risk",
                                                                  "execution_failed")],
            "skipped": [d for d in decisions if d["status"] == "skipped"][:50]}


@router.get("/positions")
def positions(request: Request, _: Principal = Depends(current_user)) -> dict[str, Any]:
    r = rt(request)
    closed = r.repo.closed_trades(200) if r.repo else []
    return {"open": [p.to_dict() for p in r.master.execution.positions.open_positions()], "closed": closed}


@router.get("/risk")
def risk(request: Request, _: Principal = Depends(current_user)) -> dict[str, Any]:
    m = rt(request).master
    return {"limits": m.risk_manager.limits.model_dump(), "safety": m.safety.to_dict(),
            "rejections": dict(m.risk_manager.rejection_counts),
            "portfolio": {k: v for k, v in m.portfolio.snapshot().items() if k in ("drawdown_pct", "equity",
                                                                                   "peak_equity", "day", "week")},
            "emergency_close_policy": m.settings.emergency_close_policy.value}


@router.get("/strategies")
def strategies(request: Request, _: Principal = Depends(current_user)) -> list[dict[str, Any]]:
    return rt(request).master.optimizer.ranking()


@router.get("/alerts")
def alerts(request: Request, _: Principal = Depends(current_user)) -> list[dict[str, Any]]:
    return rt(request).alerts.recent(200)


@router.get("/audit")
def audit(request: Request, _: Principal = Depends(require_admin)) -> dict[str, Any]:
    a = rt(request).audit
    ok, bad = a.verify()
    return {"valid": ok, "first_invalid": bad, "entries": a.tail(200)}


@router.post("/killswitch/activate")
def kill_on(body: KillSwitchBody, request: Request, user: Principal = Depends(require_admin)) -> dict[str, Any]:
    rt(request).master.safety.activate_kill_switch(body.reason, user.username)
    return rt(request).master.safety.to_dict()


@router.post("/killswitch/release")
def kill_off(body: ConfirmBody, request: Request, user: Principal = Depends(require_admin)) -> dict[str, Any]:
    m = rt(request).master
    try:
        m.safety.deactivate_kill_switch(user.username, body.confirm)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    m.audit.record(user.username, "kill_switch_released", {})
    return m.safety.to_dict()


@router.post("/safety/reset")
def safety_reset(body: ConfirmBody, request: Request, user: Principal = Depends(require_admin)) -> dict[str, Any]:
    m = rt(request).master
    try:
        m.safety.reset_safety_mode(user.username, body.confirm)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    m.audit.record(user.username, "safety_mode_reset", {})
    return m.safety.to_dict()


@router.post("/macro/risk-off")
def risk_off(body: RiskOffBody, request: Request, user: Principal = Depends(require_admin)) -> dict[str, Any]:
    m = rt(request).master
    m.macro.set_manual_risk_off(body.reason)
    m.audit.record(user.username, "manual_risk_off", {"reason": body.reason})
    return {"manual_risk_off": body.reason}


@router.put("/session/target")
def set_target(body: TargetBody, request: Request, user: Principal = Depends(require_admin)) -> dict[str, Any]:
    """The target is a performance indicator only; it never changes sizing or risk."""
    m = rt(request).master
    m.portfolio.target.enabled = body.enabled
    m.portfolio.target.target = body.target
    m.audit.record(user.username, "session_target_changed", body.model_dump())
    return m.portfolio.snapshot()["target"]
