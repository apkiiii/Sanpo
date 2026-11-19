"""Runtime: wires every component from the settings and runs the autonomous loop.

* SIMULATION: replays verified historical data (or, explicitly, synthetic data)
  bar by bar through the simulated broker — no real order is ever sent.
* DEMO / LIVE: runs on wall-clock time against a broker connector (none is
  shipped yet: see docs/BROKER_SETUP.md).

The loop survives errors (they are counted by the safety manager), keeps
monitoring positions when entries are blocked, and publishes snapshots to the
dashboard subscribers.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from backend.alerts.manager import Alert, AlertLevel, AlertManager, AlertType, EmailChannel, TelegramChannel
from backend.backtesting.engine import DEFAULT_SPREADS
from backend.agents.strategy_optimizer import StrategyOptimizer
from backend.config.settings import Settings, TradingMode
from backend.core.errors import BrokerConnectionError, ConfigurationError
from backend.core.instruments import get_instrument
from backend.core.logging import get_logger, register_secrets
from backend.core.market_hours import is_market_open
from backend.core.types import Timeframe, utcnow
from backend.data.economic_calendar import CalendarProvider, FileCalendarProvider, NullCalendarProvider
from backend.data.market_data import BrokerMarketDataProvider, CSVMarketDataProvider, MarketDataProvider
from backend.data.synthetic import generate_ohlc
from backend.database.repository import Repository
from backend.master.master_controller import MasterController
from backend.security.audit import AuditLog
from backend.trading.broker_factory import create_broker
from backend.trading.broker_interface import BrokerInterface
from backend.trading.simulated_broker import SimulatedBroker

log = get_logger(__name__)
SYNTHETIC_START = datetime(2025, 1, 6, tzinfo=UTC)


def build_alerts(settings: Settings) -> AlertManager:
    alerts = AlertManager()
    if settings.telegram_bot_token.get_secret_value() and settings.telegram_chat_id:
        alerts.add_channel(TelegramChannel(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id))
    if settings.smtp_host and settings.alert_email_to:
        alerts.add_channel(EmailChannel(settings.smtp_host, settings.smtp_port, settings.smtp_username,
                                        settings.smtp_password.get_secret_value(), settings.alert_email_from,
                                        settings.alert_email_to))
    return alerts


class TradingRuntime:
    def __init__(self, settings: Settings, repo: Repository | None = None, broker: BrokerInterface | None = None,
                 data: MarketDataProvider | None = None, calendar: CalendarProvider | None = None) -> None:
        self.settings = settings
        register_secrets(settings.secret_values())
        self.repo = repo
        self.alerts = build_alerts(settings)
        self.audit = AuditLog(sink=repo.save_audit if repo else None)
        if repo is not None:
            self.audit.load(repo.load_audit())
            ok, bad = self.audit.verify()
            if not ok:
                raise ConfigurationError(f"audit log integrity check failed at entry {bad}")
        self.replay_cursor: datetime | None = None
        self._replay_end: datetime | None = None
        self.synthetic = False
        if settings.trading_mode is TradingMode.SIMULATION:
            data = data or self._simulation_data()
            self.broker = broker or create_broker(settings, data)
        else:
            self.broker = broker or create_broker(settings)
            data = data or BrokerMarketDataProvider(self.broker)
        self.data = data
        self.calendar = calendar or (FileCalendarProvider(settings.economic_calendar_path)
                                     if settings.economic_calendar_path else NullCalendarProvider())
        if repo is not None and isinstance(self.broker, SimulatedBroker):
            saved_broker = repo.load_state("sim_broker")
            if saved_broker:
                self.broker.import_state(saved_broker)  # demo account survives restarts
        optimizer = StrategyOptimizer(Timeframe(settings.entry_timeframe))
        loaded = optimizer.load(settings.strategy_validation_path)
        self.master = MasterController(settings, self.broker, data, self.calendar, self.alerts, self.audit,
                                       repo, optimizer)
        self.master.restore()
        self._listeners: set[asyncio.Queue[dict[str, Any]]] = set()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.cycles = 0
        self.step_seconds = settings.simulation_step_seconds
        self.running = settings.trading_mode is not TradingMode.SIMULATION or settings.simulation_autostart
        saved_ctl = repo.load_state("sim_control") if repo is not None else None
        if saved_ctl and settings.trading_mode is TradingMode.SIMULATION:
            self.running = bool(saved_ctl.get("running", self.running))
            self.step_seconds = float(saved_ctl.get("step_seconds", self.step_seconds))
        self.alerts.dashboard.subscribers.append(self._on_alert)
        self.audit.record("system", "startup", {"mode": settings.trading_mode.value, "broker": self.broker.name,
                                                "validated_strategies_loaded": loaded,
                                                "synthetic_data": self.synthetic})
        if self.synthetic:
            self.alerts.send(AlertType.SYSTEM, AlertLevel.WARNING, "SYNTHETIC DATA",
                             "Simulation runs on synthetic prices: results have no predictive value.")

    # ------------------------------------------------------------------ data
    def _simulation_data(self) -> CSVMarketDataProvider:
        provider = CSVMarketDataProvider(self.settings.historical_data_dir, DEFAULT_SPREADS)
        if self.settings.simulation_data_source == "synthetic":
            self.synthetic = True
            # aligned on midnight UTC so that bars fall on the normal M5/M15/H1 grid
            # fixed start so the generated market is identical at every launch (the demo can resume)
            for i, sym in enumerate(self.settings.symbols):
                provider.add_frame(sym, generate_ohlc(sym, SYNTHETIC_START, self.settings.synthetic_bars,
                                                      Timeframe.M5, seed=100 + i))
        else:
            missing = [s for s in self.settings.symbols if s not in provider.symbols()]
            if missing:
                raise ConfigurationError(
                    f"no historical data for {missing} in {self.settings.historical_data_dir}. Provide verified CSV "
                    "files (see data/historical/README.md) or set SIMULATION_DATA_SOURCE=synthetic for a pipeline demo.")
        starts = [provider.time_range(s)[0] for s in self.settings.symbols]
        ends = [provider.time_range(s)[1] for s in self.settings.symbols]
        begin = pd.Timestamp(self.settings.simulation_start, tz="UTC").to_pydatetime() if self.settings.simulation_start \
            else max(starts) + timedelta(days=60)
        if begin >= min(ends):
            raise ConfigurationError("not enough history for the simulation warm-up (need > 60 days)")
        saved = self.repo.load_state("replay") if self.repo is not None else None
        if saved and saved.get("source") == self.settings.simulation_data_source:
            resume = datetime.fromisoformat(saved["cursor"])
            if begin <= resume < min(ends):
                begin = resume  # continue where the previous run stopped
        self.replay_cursor = begin
        self._replay_end = min(ends)
        provider.cursor = begin
        return provider

    def _advance_replay(self) -> datetime | None:
        assert self.replay_cursor is not None
        nxt = self.replay_cursor + timedelta(minutes=5)
        while not is_market_open(nxt) and nxt < self._replay_end:
            nxt += timedelta(minutes=5)
        if nxt >= self._replay_end:
            return None
        self.replay_cursor = nxt
        assert isinstance(self.data, CSVMarketDataProvider) and isinstance(self.broker, SimulatedBroker)
        self.data.cursor = nxt
        self.broker.clock = nxt
        for sym in self.settings.symbols:
            half = DEFAULT_SPREADS.get(sym, 1.5) * get_instrument(sym).pip_size / 2
            # replay the bar's intrabar path so stops/targets hit by a wick are triggered
            for mid in self.data.bar_path(sym):
                self.broker.set_quote(sym, mid - half, mid + half, nxt)
        return nxt

    # ------------------------------------------------------------------ loop
    async def start(self) -> None:
        try:
            await asyncio.to_thread(self.broker.connect)
        except BrokerConnectionError as exc:
            self.alerts.send(AlertType.BROKER_DISCONNECTED, AlertLevel.CRITICAL, "Broker connection failed", str(exc))
        if self.replay_cursor is not None:
            self._advance_replay()
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="trading-loop")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self.audit.record("system", "shutdown", {"cycles": self.cycles})

    async def _loop(self) -> None:
        simulation = self.settings.trading_mode is TradingMode.SIMULATION
        while not self._stop.is_set():
            if simulation and not self.running:  # paused: the simulated market does not move
                await asyncio.sleep(0.25)
                continue
            if simulation:
                now = self.replay_cursor or utcnow()
            else:
                now = utcnow()
            await asyncio.to_thread(self.master.run_cycle, now)
            self.cycles += 1
            self.persist()
            await self._broadcast({"type": "snapshot", "data": self.snapshot()})
            if simulation:
                if self._advance_replay() is None:
                    self.alerts.send(AlertType.SYSTEM, AlertLevel.INFO, "Replay finished", "End of historical data.")
                    return
                await asyncio.sleep(self.step_seconds)
            else:
                await asyncio.sleep(self.settings.cycle_interval_seconds)

    def persist(self) -> None:
        """Save the demo account and replay position (SIMULATION only)."""
        if self.repo is None or not isinstance(self.broker, SimulatedBroker):
            return
        try:
            self.repo.save_state("sim_broker", self.broker.export_state())
            if self.replay_cursor is not None:
                self.repo.save_state("replay", {"cursor": self.replay_cursor.isoformat(),
                                                "source": self.settings.simulation_data_source})
        except Exception:  # noqa: BLE001
            log.exception("demo state persistence failed")

    # ------------------------------------------------------------------ simulation control
    MIN_STEP, MAX_STEP = 0.02, 10.0

    def set_running(self, running: bool, actor: str) -> dict[str, Any]:
        """Start or pause the simulated market (SIMULATION only). Positions stay protected by their stops."""
        if self.settings.trading_mode is not TradingMode.SIMULATION:
            raise ConfigurationError("the trading simulation can only be started or paused in SIMULATION mode")
        if self.replay_cursor is not None and self._replay_end and self.replay_cursor >= self._replay_end:
            raise ConfigurationError("the simulated market has reached the end of its data")
        self.running = running
        self.audit.record(actor, "simulation_started" if running else "simulation_paused",
                          {"replay_time": self.replay_cursor.isoformat() if self.replay_cursor else None})
        self._save_control()
        return self.simulation_status()

    def set_speed(self, step_seconds: float, actor: str) -> dict[str, Any]:
        if self.settings.trading_mode is not TradingMode.SIMULATION:
            raise ConfigurationError("speed applies to the SIMULATION mode only")
        if not self.MIN_STEP <= step_seconds <= self.MAX_STEP:
            raise ValueError(f"step must be between {self.MIN_STEP} and {self.MAX_STEP} seconds")
        self.step_seconds = step_seconds
        self.audit.record(actor, "simulation_speed", {"step_seconds": step_seconds})
        self._save_control()
        return self.simulation_status()

    def simulation_status(self) -> dict[str, Any]:
        return {"available": self.settings.trading_mode is TradingMode.SIMULATION, "running": self.running,
                "step_seconds": self.step_seconds,
                "replay_time": self.replay_cursor.isoformat() if self.replay_cursor else None}

    def _save_control(self) -> None:
        if self.repo is not None:
            self.repo.save_state("sim_control", {"running": self.running, "step_seconds": self.step_seconds})

    def cash_flow(self, amount: float, actor: str) -> dict[str, Any]:
        result = self.master.cash_flow(amount, actor)
        self.persist()
        return result

    # ------------------------------------------------------------------ dashboard
    def _on_alert(self, alert: Alert) -> None:
        if self.repo is not None:
            try:
                self.repo.save_alert(alert)
            except Exception:  # noqa: BLE001
                log.exception("alert persistence failed")
        for q in list(self._listeners):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait({"type": "alert", "data": alert.to_dict()})

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._listeners.discard(q)

    async def _broadcast(self, message: dict[str, Any]) -> None:
        for q in list(self._listeners):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(message)

    def snapshot(self) -> dict[str, Any]:
        st = self.master.status()
        st["synthetic_data"] = self.synthetic
        st["replay_time"] = self.replay_cursor.isoformat() if self.replay_cursor else None
        st["cycles"] = self.cycles
        st["simulation"] = self.simulation_status()
        from backend.version import APP_VERSION
        st["app_version"] = APP_VERSION
        st["cash_history"] = self.broker.cash_history()[-20:] if isinstance(self.broker, SimulatedBroker) else []
        st["decisions"] = [d.to_dict() for d in list(self.master.decisions)[:50]]
        return st
