"""Security tests: LIVE gating, secret hygiene, alert redaction, extreme scenarios."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.alerts.manager import AlertLevel, AlertManager, AlertType
from backend.config.settings import Settings
from backend.security.audit import AuditLog
from backend.security.live_guard import REQUIRED_PHRASE, check_live_requirements, source_hash

pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[2]


def _live_settings(**kw: object) -> Settings:
    base = dict(_env_file=None, trading_mode="live", live_trading_enabled=True,
                live_confirmation_phrase=REQUIRED_PHRASE, live_min_demo_trades=10)
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def _report(tmp_path: Path, **kw: object) -> Path:
    data = {"finished_at": datetime.now(UTC).isoformat(), "passed": 50, "failed": 0, "errors": 0,
            "source_hash": source_hash()}
    data.update(kw)
    p = tmp_path / "critical.json"
    p.write_text(json.dumps(data))
    return p


def test_live_allowed_only_when_every_requirement_met(tmp_path: Path) -> None:
    ok = check_live_requirements(_live_settings(), "live", 10, report_path=_report(tmp_path))
    assert ok.allowed, ok.failures


@pytest.mark.parametrize("settings_kw,env,demo,report_kw,expected", [
    ({"trading_mode": "demo"}, "live", 10, {}, "not 'live'"),
    ({"live_trading_enabled": False}, "live", 10, {}, "LIVE_TRADING_ENABLED"),
    ({"live_confirmation_phrase": "yes"}, "live", 10, {}, "PHRASE"),
    ({}, "practice", 10, {}, "broker environment"),
    ({}, "live", 3, {}, "DEMO trades"),
    ({}, "live", 10, {"failed": 1}, "did not all pass"),
    ({}, "live", 10, {"source_hash": "old"}, "source code changed"),
    ({}, "live", 10, {"finished_at": (datetime.now(UTC) - timedelta(days=30)).isoformat()}, "too old"),
])
def test_live_refused(tmp_path: Path, settings_kw: dict, env: str, demo: int, report_kw: dict, expected: str) -> None:
    res = check_live_requirements(_live_settings(**settings_kw), env, demo, report_path=_report(tmp_path, **report_kw))
    assert not res.allowed and any(expected in f for f in res.failures), res.failures


def test_live_refused_without_report(tmp_path: Path) -> None:
    res = check_live_requirements(_live_settings(), "live", 10, report_path=tmp_path / "missing.json")
    assert not res.allowed


def test_env_example_has_no_real_secrets_and_env_is_ignored() -> None:
    env = (ROOT / ".env.example").read_text()
    for key in ("JWT_SECRET", "DASHBOARD_ADMIN_PASSWORD_HASH", "TELEGRAM_BOT_TOKEN", "SMTP_PASSWORD", "BROKER_API_KEY"):
        m = re.search(rf"^{key}=(.*)$", env, re.M)
        assert m is not None and m.group(1).strip() == "", f"{key} must be empty in .env.example"
    assert "TRADING_MODE=simulation" in env and "LIVE_TRADING_ENABLED=false" in env
    ignore = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignore


def test_no_hardcoded_secrets_in_source() -> None:
    pattern = re.compile(r"(api_key|secret|password|token)\s*=\s*['\"][A-Za-z0-9_\-]{16,}['\"]", re.I)
    offenders = []
    for p in (ROOT / "backend").rglob("*.py"):
        if "tests" in p.parts:
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{p.name}:{i}")
    assert not offenders, offenders


def test_alerts_never_contain_secrets() -> None:
    from backend.core.logging import register_secrets
    register_secrets(["MY-BROKER-KEY-0123456789"])
    sent = []

    class Capture:
        name = "capture"

        def send(self, alert):  # type: ignore[no-untyped-def]
            sent.append(alert.text() + json.dumps(alert.data))

    am = AlertManager(async_external=False)
    am.add_channel(Capture())
    am.send(AlertType.EXECUTION_ERROR, AlertLevel.CRITICAL, "error api_key=abc123def",
            "failed with MY-BROKER-KEY-0123456789", {"raw": "token: zzz999"})
    assert sent and all("MY-BROKER-KEY" not in s and "abc123def" not in s and "zzz999" not in s for s in sent)


def test_alert_channel_failure_does_not_raise() -> None:
    class Broken:
        name = "broken"

        def send(self, alert):  # type: ignore[no-untyped-def]
            raise RuntimeError("smtp down")

    am = AlertManager(async_external=False)
    am.add_channel(Broken())
    assert am.send(AlertType.SYSTEM, AlertLevel.INFO, "x") is not None and am.failures


def test_alert_deduplication() -> None:
    am = AlertManager(async_external=False)
    assert am.send(AlertType.BROKER_DISCONNECTED, AlertLevel.CRITICAL, "down", dedup_key="d") is not None
    assert am.send(AlertType.BROKER_DISCONNECTED, AlertLevel.CRITICAL, "down", dedup_key="d") is None


def test_audit_chain_detects_tampering() -> None:
    a = AuditLog()
    for i in range(5):
        a.record("u", "act", {"i": i})
    assert a.verify() == (True, None)
    from dataclasses import replace
    a.entries[2] = replace(a.entries[2], details={"i": 99})
    assert a.verify() == (False, 3)
    b = AuditLog()
    b.record("u", "a")
    b.record("u", "b")
    del b.entries[0]
    assert not b.verify()[0]
