"""LIVE mode guard.

LIVE trading is possible only if **all** of the following hold:

1. ``TRADING_MODE=live`` and ``LIVE_TRADING_ENABLED=true`` (both explicit);
2. ``LIVE_CONFIRMATION_PHRASE`` equals :data:`REQUIRED_PHRASE`;
3. the broker connector declares ``environment == "live"``;
4. a critical-test report exists, is recent, reports zero failures and was
   produced on the *current* source code (hash match);
5. at least ``LIVE_MIN_DEMO_TRADES`` closed trades were recorded in DEMO mode.

The system never switches from DEMO to LIVE by itself: the mode is read once at
start-up and there is no API to change it at runtime.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.config.settings import Settings, TradingMode

REQUIRED_PHRASE = "I UNDERSTAND THAT LIVE TRADING CAN LOSE REAL MONEY"
BACKEND_DIR = Path(__file__).resolve().parents[1]


def source_hash(root: Path = BACKEND_DIR) -> str:
    """SHA-256 over all backend Python sources except tests (stable ordering)."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        if "tests" in p.parts or "__pycache__" in p.parts:
            continue
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


@dataclass(slots=True)
class LiveCheck:
    allowed: bool
    failures: list[str] = field(default_factory=list)


def check_live_requirements(settings: Settings, broker_environment: str, demo_closed_trades: int,
                            now: datetime | None = None, report_path: Path | None = None) -> LiveCheck:
    now = now or datetime.now(UTC)
    f: list[str] = []
    if settings.trading_mode is not TradingMode.LIVE:
        f.append("TRADING_MODE is not 'live'")
    if not settings.live_trading_enabled:
        f.append("LIVE_TRADING_ENABLED is false")
    if settings.live_confirmation_phrase.strip() != REQUIRED_PHRASE:
        f.append("LIVE_CONFIRMATION_PHRASE does not match the required phrase")
    if broker_environment != "live":
        f.append(f"broker environment is '{broker_environment}', not 'live'")
    path = report_path or Path(settings.critical_tests_report_path)
    if not path.exists():
        f.append(f"critical test report missing ({path}); run scripts/run_critical_tests.py")
    else:
        try:
            rep = json.loads(path.read_text())
            ran_at = datetime.fromisoformat(rep["finished_at"])
            if rep.get("failed", 1) != 0 or rep.get("errors", 1) != 0 or rep.get("passed", 0) <= 0:
                f.append("critical tests did not all pass")
            if now - ran_at > timedelta(days=settings.critical_tests_max_age_days):
                f.append("critical test report is too old")
            if rep.get("source_hash") != source_hash():
                f.append("source code changed since the critical tests were run")
        except (KeyError, ValueError) as exc:
            f.append(f"critical test report unreadable: {exc}")
    if demo_closed_trades < settings.live_min_demo_trades:
        f.append(f"only {demo_closed_trades} closed DEMO trades, {settings.live_min_demo_trades} required")
    return LiveCheck(not f, f)
