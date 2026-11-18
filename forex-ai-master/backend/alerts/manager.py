"""Alert system: dashboard, Telegram and e-mail channels.

* Messages are passed through :func:`redact` so secrets can never be sent.
* Identical alerts are de-duplicated within a cooldown window.
* A failing channel is logged and never interrupts trading.
* External channels are sent from a background thread pool.
"""

from __future__ import annotations

import smtplib
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from enum import StrEnum
from typing import Any, Protocol

import httpx

from backend.core.logging import get_logger, redact
from backend.core.types import new_id, utcnow

log = get_logger(__name__)


class AlertLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertType(StrEnum):
    TRADE_EXECUTED = "trade_executed"
    TRADE_CLOSED = "trade_closed"
    STOP_LOSS_HIT = "stop_loss_hit"
    TAKE_PROFIT_HIT = "take_profit_hit"
    SESSION_TARGET_REACHED = "session_target_reached"
    LOSS_LIMIT_REACHED = "loss_limit_reached"
    DRAWDOWN_EXCESSIVE = "drawdown_excessive"
    BROKER_DISCONNECTED = "broker_disconnected"
    API_UNAVAILABLE = "api_unavailable"
    EXECUTION_ERROR = "execution_error"
    POSITION_ANOMALY = "position_anomaly"
    SAFETY_MODE = "safety_mode"
    KILL_SWITCH = "kill_switch"
    SYSTEM = "system"


@dataclass(slots=True)
class Alert:
    type: AlertType
    level: AlertLevel
    title: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("ALR"))
    time: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type.value, "level": self.level.value, "title": self.title,
                "message": self.message, "data": self.data, "time": self.time.isoformat()}

    def text(self) -> str:
        return f"[{self.level.value.upper()}] {self.title}\n{self.message}"


class AlertChannel(Protocol):
    name: str

    def send(self, alert: Alert) -> None: ...


class DashboardChannel:
    """Keeps recent alerts in memory and notifies subscribers (WebSocket)."""

    name = "dashboard"

    def __init__(self, maxlen: int = 500) -> None:
        self.alerts: deque[Alert] = deque(maxlen=maxlen)
        self.subscribers: list[Callable[[Alert], None]] = []

    def send(self, alert: Alert) -> None:
        self.alerts.appendleft(alert)
        for cb in list(self.subscribers):
            try:
                cb(alert)
            except Exception:  # noqa: BLE001
                log.exception("dashboard subscriber failed")


class TelegramChannel:
    """Telegram Bot API (``sendMessage``). The token is never logged (URLs are redacted)."""

    name = "telegram"

    def __init__(self, token: str, chat_id: str, min_level: AlertLevel = AlertLevel.WARNING,
                 client: httpx.Client | None = None) -> None:
        self._token = token
        self.chat_id = chat_id
        self.min_level = min_level
        self._client = client or httpx.Client(timeout=5.0)

    def send(self, alert: Alert) -> None:
        if _level_rank(alert.level) < _level_rank(self.min_level):
            return
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        r = self._client.post(url, json={"chat_id": self.chat_id, "text": alert.text()[:4000]})
        if r.status_code != 200:
            raise RuntimeError(f"telegram HTTP {r.status_code}")


class EmailChannel:
    name = "email"

    def __init__(self, host: str, port: int, username: str, password: str, sender: str, recipient: str,
                 min_level: AlertLevel = AlertLevel.CRITICAL) -> None:
        self.host, self.port, self.username = host, port, username
        self._password = password
        self.sender, self.recipient, self.min_level = sender, recipient, min_level

    def send(self, alert: Alert) -> None:
        if _level_rank(alert.level) < _level_rank(self.min_level):
            return
        msg = EmailMessage()
        msg["Subject"] = f"[Forex AI Master] {alert.title}"
        msg["From"], msg["To"] = self.sender, self.recipient
        msg.set_content(alert.text())
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            smtp.starttls()
            if self.username:
                smtp.login(self.username, self._password)
            smtp.send_message(msg)


def _level_rank(level: AlertLevel) -> int:
    return {AlertLevel.INFO: 0, AlertLevel.WARNING: 1, AlertLevel.CRITICAL: 2}[level]


class AlertManager:
    def __init__(self, cooldown: timedelta = timedelta(minutes=5), async_external: bool = True) -> None:
        self.dashboard = DashboardChannel()
        self.external: list[AlertChannel] = []
        self.cooldown = cooldown
        self._last_sent: dict[str, datetime] = {}
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="alerts") if async_external else None
        self.failures: deque[str] = deque(maxlen=100)

    def add_channel(self, channel: AlertChannel) -> None:
        self.external.append(channel)

    def _deliver(self, channel: AlertChannel, alert: Alert) -> None:
        try:
            channel.send(alert)
        except Exception as exc:  # noqa: BLE001 - alert failures never stop trading
            msg = redact(f"{channel.name}: {exc}")
            self.failures.append(msg)
            log.warning("alert channel failed", extra={"ctx": {"error": msg}})

    def send(self, type_: AlertType, level: AlertLevel, title: str, message: str = "",
             data: dict[str, Any] | None = None, dedup_key: str | None = None) -> Alert | None:
        key = f"{type_.value}:{dedup_key or title}"
        now = utcnow()
        last = self._last_sent.get(key)
        if dedup_key is not None and last is not None and now - last < self.cooldown:
            return None
        self._last_sent[key] = now
        safe_data = {k: redact(str(v)) if isinstance(v, str) else v for k, v in (data or {}).items()}
        alert = Alert(type_, level, redact(title), redact(message), safe_data)
        self.dashboard.send(alert)
        for ch in self.external:
            if self._executor is not None:
                self._executor.submit(self._deliver, ch, alert)
            else:
                self._deliver(ch, alert)
        return alert

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return [a.to_dict() for a in list(self.dashboard.alerts)[:limit]]
