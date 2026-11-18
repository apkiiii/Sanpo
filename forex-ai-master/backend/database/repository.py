"""Repository implementing the :class:`StateStore` port with SQLAlchemy."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.alerts.manager import Alert
from backend.core.logging import get_logger
from backend.database.models import (
    AlertRecord,
    AuditRecord,
    Base,
    ClosedTradeRecord,
    DecisionRecord,
    OrderRecord,
    PositionRecord,
    SystemState,
)
from backend.security.audit import AuditEntry

log = get_logger(__name__)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        from pathlib import Path

        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True)


class Repository:
    def __init__(self, url: str, create_schema: bool = False) -> None:
        self.engine = make_engine(url)
        if create_schema:
            Base.metadata.create_all(self.engine)
        self._session = sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> Session:
        return self._session()

    # --- StateStore ---------------------------------------------------------
    def save_state(self, key: str, value: dict[str, Any]) -> None:
        with self.session() as s, s.begin():
            s.merge(SystemState(key=key, value=value))

    def load_state(self, key: str) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(SystemState, key)
            return dict(row.value) if row else None

    def save_position(self, data: dict[str, Any]) -> None:
        with self.session() as s, s.begin():
            row = s.get(PositionRecord, data["trade_id"])
            merged = {**(row.data if row else {}), **data}
            s.merge(PositionRecord(trade_id=data["trade_id"], symbol=merged["symbol"],
                                   direction=merged.get("direction", ""), status=merged.get("status", "open"),
                                   mode=merged.get("mode", ""), data=merged))

    def load_open_positions(self) -> list[dict[str, Any]]:
        with self.session() as s:
            return [dict(r.data) for r in s.scalars(select(PositionRecord).where(PositionRecord.status == "open"))]

    def save_decision(self, data: dict[str, Any]) -> None:
        with self.session() as s, s.begin():
            s.merge(DecisionRecord(decision_id=data["decision_id"], symbol=data["symbol"], status=data["status"],
                                   time=datetime.fromisoformat(data["time"]), data=data))

    def save_order(self, data: dict[str, Any]) -> None:
        with self.session() as s, s.begin():
            s.merge(OrderRecord(client_order_id=data["client_order_id"], symbol=data["symbol"],
                                status=data["status"], mode=data.get("mode", ""), data=data))

    def save_closed_trade(self, data: dict[str, Any]) -> None:
        with self.session() as s, s.begin():
            s.merge(ClosedTradeRecord(trade_id=data["trade_id"], symbol=data["symbol"], mode=data.get("mode", ""),
                                      net_pnl=float(data["net_pnl"]), exit_reason=data.get("exit_reason", ""),
                                      exit_time=datetime.fromisoformat(data["exit_time"]), data=data))
            pos = s.get(PositionRecord, data["trade_id"])
            if pos is not None:
                pos.status = "closed"
                pos.data = {**pos.data, "status": "closed", "exit_reason": data.get("exit_reason")}

    def count_closed_trades(self, mode: str) -> int:
        with self.session() as s:
            return int(s.scalar(select(func.count()).select_from(ClosedTradeRecord)
                                .where(ClosedTradeRecord.mode == mode)) or 0)

    # --- queries for the API ------------------------------------------------
    def closed_trades(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.scalars(select(ClosedTradeRecord).order_by(ClosedTradeRecord.exit_time.desc()).limit(limit))
            return [dict(r.data) for r in rows]

    def decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.scalars(select(DecisionRecord).order_by(DecisionRecord.time.desc()).limit(limit))
            return [dict(r.data) for r in rows]

    # --- audit & alerts -----------------------------------------------------
    def save_audit(self, e: AuditEntry) -> None:
        with self.session() as s, s.begin():
            s.add(AuditRecord(seq=e.seq, time=e.time, actor=e.actor, action=e.action, details=e.details,
                              prev_hash=e.prev_hash, hash=e.hash))

    def load_audit(self) -> list[AuditEntry]:
        with self.session() as s:
            rows = s.scalars(select(AuditRecord).order_by(AuditRecord.seq))
            return [AuditEntry(r.seq, r.time if r.time.tzinfo else r.time.replace(tzinfo=UTC),
                               r.actor, r.action, dict(r.details), r.prev_hash, r.hash) for r in rows]

    def save_alert(self, a: Alert) -> None:
        with self.session() as s, s.begin():
            s.merge(AlertRecord(id=a.id, time=a.time, type=a.type.value, level=a.level.value, title=a.title,
                                message=a.message))
