"""Persistence port used by the Master controller (implemented by the database repository)."""

from __future__ import annotations

from typing import Any, Protocol


class StateStore(Protocol):
    def save_state(self, key: str, value: dict[str, Any]) -> None: ...

    def load_state(self, key: str) -> dict[str, Any] | None: ...

    def save_position(self, data: dict[str, Any]) -> None: ...

    def load_open_positions(self) -> list[dict[str, Any]]: ...

    def save_decision(self, data: dict[str, Any]) -> None: ...

    def save_order(self, data: dict[str, Any]) -> None: ...

    def save_closed_trade(self, data: dict[str, Any]) -> None: ...

    def count_closed_trades(self, mode: str) -> int: ...


class InMemoryStateStore:
    """Simple store for tests and for running without a database."""

    def __init__(self) -> None:
        self.state: dict[str, dict[str, Any]] = {}
        self.positions: dict[str, dict[str, Any]] = {}
        self.decisions: list[dict[str, Any]] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.closed: list[dict[str, Any]] = []

    def save_state(self, key: str, value: dict[str, Any]) -> None:
        self.state[key] = value

    def load_state(self, key: str) -> dict[str, Any] | None:
        return self.state.get(key)

    def save_position(self, data: dict[str, Any]) -> None:
        self.positions[data["trade_id"]] = {**self.positions.get(data["trade_id"], {}), **data}

    def load_open_positions(self) -> list[dict[str, Any]]:
        return [p for p in self.positions.values() if p.get("status") == "open"]

    def save_decision(self, data: dict[str, Any]) -> None:
        self.decisions.append(data)

    def save_order(self, data: dict[str, Any]) -> None:
        self.orders[data["client_order_id"]] = data

    def save_closed_trade(self, data: dict[str, Any]) -> None:
        self.closed.append(data)

    def count_closed_trades(self, mode: str) -> int:
        return sum(1 for t in self.closed if t.get("mode") == mode)
