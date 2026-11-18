"""Kill switch and safety mode.

* **Kill switch**: manual or automatic emergency stop. Blocks all new entries,
  triggers cancellation of pending orders and the configured emergency close
  policy (executed by the trading engine), raises an alert and is audited.
  Deactivation requires an explicit operator action.
* **Safety mode**: automatic protective state (drawdown, broker disconnection,
  repeated errors, position anomaly). Blocks new entries but keeps monitoring.

This object holds state only; side effects are delegated to registered callbacks
so the logic is easy to test.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from backend.core.logging import get_logger
from backend.core.types import utcnow

log = get_logger(__name__)


@dataclass(slots=True)
class SafetyEvent:
    time: datetime
    kind: str  # kill_switch_on | kill_switch_off | safety_on | safety_off | error
    reason: str
    actor: str


@dataclass(slots=True)
class SafetyState:
    kill_switch: bool = False
    kill_switch_reason: str = ""
    safety_mode: bool = False
    safety_reasons: list[str] = field(default_factory=list)
    consecutive_errors: int = 0


class SafetyManager:
    def __init__(self, max_consecutive_errors: int = 5) -> None:
        self.state = SafetyState()
        self.max_consecutive_errors = max_consecutive_errors
        self.history: list[SafetyEvent] = []
        self._on_kill: list[Callable[[str, str], None]] = []
        self._on_safety: list[Callable[[str], None]] = []

    # -------------------------------------------------------------- callbacks
    def on_kill_switch(self, callback: Callable[[str, str], None]) -> None:
        self._on_kill.append(callback)

    def on_safety_mode(self, callback: Callable[[str], None]) -> None:
        self._on_safety.append(callback)

    # -------------------------------------------------------------- queries
    @property
    def entries_blocked(self) -> bool:
        return self.state.kill_switch or self.state.safety_mode

    def blocking_reasons(self) -> list[str]:
        out = []
        if self.state.kill_switch:
            out.append(f"kill switch: {self.state.kill_switch_reason}")
        out.extend(f"safety: {r}" for r in self.state.safety_reasons)
        return out

    # -------------------------------------------------------------- kill switch
    def activate_kill_switch(self, reason: str, actor: str = "system") -> None:
        already = self.state.kill_switch
        self.state.kill_switch = True
        self.state.kill_switch_reason = reason
        self.history.append(SafetyEvent(utcnow(), "kill_switch_on", reason, actor))
        log.critical("KILL SWITCH ACTIVATED", extra={"ctx": {"reason": reason, "actor": actor}})
        if not already:
            for cb in self._on_kill:
                cb(reason, actor)

    def deactivate_kill_switch(self, actor: str, confirmation: bool) -> None:
        """Explicit operator action required; never called automatically."""
        if not confirmation or not actor or actor == "system":
            raise PermissionError("kill switch can only be released by an operator with explicit confirmation")
        self.state.kill_switch = False
        self.state.kill_switch_reason = ""
        self.history.append(SafetyEvent(utcnow(), "kill_switch_off", "released", actor))

    # -------------------------------------------------------------- safety mode
    def enter_safety_mode(self, reason: str) -> None:
        if reason in self.state.safety_reasons:
            return
        self.state.safety_mode = True
        self.state.safety_reasons.append(reason)
        self.history.append(SafetyEvent(utcnow(), "safety_on", reason, "system"))
        log.error("safety mode", extra={"ctx": {"reason": reason}})
        for cb in self._on_safety:
            cb(reason)

    def clear_safety_reason(self, reason: str, actor: str) -> None:
        """Remove one reason (e.g. broker reconnected). Safety mode ends when no reason remains."""
        if reason in self.state.safety_reasons:
            self.state.safety_reasons.remove(reason)
            self.history.append(SafetyEvent(utcnow(), "safety_off", reason, actor))
        self.state.safety_mode = bool(self.state.safety_reasons)

    def reset_safety_mode(self, actor: str, confirmation: bool) -> None:
        if not confirmation or actor == "system":
            raise PermissionError("safety mode reset requires an operator confirmation")
        for r in list(self.state.safety_reasons):
            self.clear_safety_reason(r, actor)

    # -------------------------------------------------------------- errors
    def record_error(self, message: str) -> None:
        self.state.consecutive_errors += 1
        self.history.append(SafetyEvent(utcnow(), "error", message, "system"))
        if self.state.consecutive_errors >= self.max_consecutive_errors:
            self.enter_safety_mode(f"{self.state.consecutive_errors} consecutive errors")

    def record_success(self) -> None:
        self.state.consecutive_errors = 0

    # -------------------------------------------------------------- persistence
    def to_dict(self) -> dict[str, object]:
        return {"kill_switch": self.state.kill_switch, "kill_switch_reason": self.state.kill_switch_reason,
                "safety_mode": self.state.safety_mode, "safety_reasons": list(self.state.safety_reasons)}

    def restore(self, data: dict[str, object]) -> None:
        """Restore after restart. Protective states are restored, never cleared."""
        if data.get("kill_switch"):
            self.state.kill_switch = True
            self.state.kill_switch_reason = str(data.get("kill_switch_reason", "restored after restart"))
        for r in data.get("safety_reasons", []) or []:  # type: ignore[union-attr]
            if r not in self.state.safety_reasons:
                self.state.safety_reasons.append(str(r))
        self.state.safety_mode = bool(self.state.safety_reasons)
