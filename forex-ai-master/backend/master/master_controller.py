"""FOREX MASTER AI — central controller supervising the 7 agents.

One call to :meth:`MasterController.run_cycle` performs, in order:

1. broker connectivity check (safety mode on disconnection);
2. reconciliation of broker vs local positions (anomalies -> safety mode);
3. account / P&L update, drawdown and loss-limit monitoring;
4. management of open positions (break-even, trailing, invalidation) — always,
   even when entries are blocked;
5. analysis of every symbol by the agents;
6. if entries are allowed: consensus, ranking, Risk Manager review and execution
   of approved proposals only.

The Master cannot change risk limits (it only holds a read-only view) and cannot
place an order without a signed approval from the Risk Manager.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from backend.agents.base import MarketContext
from backend.agents.execution_monitor import ExecutionMonitor
from backend.agents.macro_analyst import MacroAnalyst
from backend.agents.market_analyst import MarketAnalyst
from backend.agents.price_action_analyst import PriceActionAnalyst
from backend.agents.risk_manager import RiskManager, atr_spike_ratio
from backend.agents.strategy_optimizer import StrategyOptimizer
from backend.agents.technical_analyst import TechnicalAnalyst
from backend.alerts.manager import AlertLevel, AlertManager, AlertType
from backend.analysis.indicators import atr
from backend.config.settings import EmergencyClosePolicy, Settings, TargetReachedPolicy, TradingMode
from backend.core.errors import BrokerConnectionError, ConfigurationError, ForexAIError, LiveModeNotAllowedError
from backend.core.instruments import configure_trade_sizes
from backend.core.logging import get_logger
from backend.core.market_hours import in_trading_session, is_market_open
from backend.core.types import AgentReport, ClosedTrade, Direction, OrderStatus, Quote, Timeframe, TradeProposal, utcnow
from backend.data.economic_calendar import CalendarProvider
from backend.data.market_data import MarketDataProvider, drop_incomplete_last_bar, returns_correlation
from backend.master.consensus_engine import ConsensusEngine
from backend.master.decision_engine import Decision, DecisionEngine
from backend.master.state_store import InMemoryStateStore, StateStore
from backend.risk.risk_engine import OpenRisk, RiskState
from backend.risk.safety_manager import SafetyManager
from backend.security.audit import AuditLog
from backend.security.live_guard import check_live_requirements
from backend.trading.broker_interface import BrokerInterface
from backend.trading.order_manager import ManagedOrder
from backend.trading.portfolio_manager import PortfolioManager, SessionTarget
from backend.trading.position_manager import ManagedPosition, ManagementPolicy

log = get_logger(__name__)

EXPECTED_BROKER_ENV = {TradingMode.SIMULATION: "simulation", TradingMode.DEMO: "practice", TradingMode.LIVE: "live"}


@dataclass(slots=True)
class CycleReport:
    time: datetime
    entries_allowed: bool
    blocking_reasons: list[str] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    management_actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class MasterController:
    def __init__(self, settings: Settings, broker: BrokerInterface, data: MarketDataProvider,
                 calendar: CalendarProvider, alerts: AlertManager | None = None, audit: AuditLog | None = None,
                 store: StateStore | None = None, optimizer: StrategyOptimizer | None = None,
                 policy: ManagementPolicy | None = None) -> None:
        self.settings = settings
        self._mode = settings.trading_mode
        self.broker = broker
        self.data = data
        self.alerts = alerts or AlertManager(async_external=False)
        self.audit = audit or AuditLog()
        self.store: StateStore = store or InMemoryStateStore()
        self._check_mode()
        configure_trade_sizes(settings.min_trade_units, settings.trade_units_step)

        self.entry_tf = Timeframe(settings.entry_timeframe)
        self.timeframes = [Timeframe(t) for t in settings.timeframes]
        limits = settings.risk_limits()
        self.safety = SafetyManager(settings.max_consecutive_errors)
        self.risk_manager = RiskManager(limits, self.safety, settings.account_currency, self.entry_tf)
        self.market_analyst = MarketAnalyst()
        self.technical_analyst = TechnicalAnalyst(self.entry_tf)
        self.price_action = PriceActionAnalyst(self.entry_tf)
        self.macro = MacroAnalyst(calendar, settings.news_blackout_minutes_before, settings.news_blackout_minutes_after,
                                  settings.block_when_calendar_unavailable)
        self.optimizer = optimizer or StrategyOptimizer(self.entry_tf)
        self._cycle_now: datetime | None = None
        self.execution = ExecutionMonitor(broker, self._verify_approval, self._entries_hard_blocked,
                                          limits.max_spread_pips, limits.max_slippage_pips, policy,
                                          settings.account_currency, self._on_order_event, self._on_position_event)
        self.portfolio = PortfolioManager(settings.initial_balance, settings.session_start_utc, settings.session_end_utc,
                                          SessionTarget(settings.target_session_profit, settings.target_enabled))
        allow_missing_macro = self._mode is TradingMode.SIMULATION and not settings.block_when_calendar_unavailable
        self.decision_engine = DecisionEngine(ConsensusEngine(allow_missing_macro=allow_missing_macro), self._mode,
                                              settings.live_approved_strategies)
        self.decisions: deque[Decision] = deque(maxlen=500)
        self.latest_reports: dict[str, dict[str, AgentReport]] = {}
        self.last_cycle: CycleReport | None = None
        self.trade_only_in_session = settings.trade_only_in_session
        self.safety.on_kill_switch(self._on_kill_switch)
        self.safety.on_safety_mode(self._on_safety_mode)

    # ================================================================ setup
    @property
    def mode(self) -> TradingMode:
        """Read-only: the mode can only be changed by restarting with a new configuration."""
        return self._mode

    def _check_mode(self) -> None:
        expected = EXPECTED_BROKER_ENV[self._mode]
        if self.broker.environment != expected:
            raise ConfigurationError(f"mode {self._mode.value} requires a '{expected}' broker, "
                                     f"got '{self.broker.environment}'")
        if self._mode is TradingMode.LIVE:
            check = check_live_requirements(self.settings, self.broker.environment,
                                            self.store.count_closed_trades(TradingMode.DEMO.value))
            if not check.allowed:
                raise LiveModeNotAllowedError("; ".join(check.failures))

    @property
    def agents(self) -> dict[str, Any]:
        return {a.name: a for a in (self.market_analyst, self.technical_analyst, self.price_action, self.macro,
                                    self.risk_manager, self.optimizer, self.execution)}

    def restore(self) -> None:
        """Restore persisted state after a restart, then reconcile with the broker."""
        saf = self.store.load_state("safety")
        if saf:
            self.safety.restore(saf)
        pf = self.store.load_state("portfolio")
        if pf:
            self.portfolio.restore(pf)
        for d in self.store.load_open_positions():
            self.execution.positions.positions[d["trade_id"]] = ManagedPosition(
                d["trade_id"], d["symbol"], Direction(d["direction"]), d["units"], d["entry_price"], d.get("stop_loss"),
                d.get("take_profit"), d.get("initial_stop"), d.get("initial_risk", 0.0),
                d.get("potential_profit", 0.0), d.get("reward_risk", 0.0), datetime.fromisoformat(d["entry_time"]),
                entry_reason=d.get("entry_reason", ""), strategy=d.get("strategy", ""),
                validated_by=tuple(d.get("validated_by", [])), invalidation=d.get("invalidation"),
                breakeven_done=d.get("breakeven_done", False))
        self.audit.record("system", "restart_restore", {"open_positions": len(self.execution.positions.positions),
                                                        "safety": self.safety.to_dict()})

    # ================================================================ events
    def _verify_approval(self, approval: Any) -> bool:
        """Approvals are checked against the cycle clock (simulated time in replays, UTC otherwise)."""
        return self.risk_manager.verify_approval(approval, self._cycle_now or utcnow())

    def _entries_hard_blocked(self) -> bool:
        return self.safety.entries_blocked

    def _on_order_event(self, kind: str, order: ManagedOrder) -> None:
        data = {"client_order_id": order.client_order_id, "proposal_id": order.proposal.proposal_id,
                "symbol": order.request.symbol, "side": order.request.side.value, "units": order.request.units,
                "stop_loss": order.request.stop_loss, "take_profit": order.request.take_profit,
                "status": order.status.value, "attempts": order.attempts, "error": order.error,
                "fill_price": order.result.fill_price if order.result else None,
                "position_id": order.result.position_id if order.result else None,
                "slippage_pips": order.slippage_pips, "mode": self._mode.value}
        self.store.save_order(data)
        self.audit.record("execution_monitor", f"order_{kind}", data)
        if kind == "filled":
            self.alerts.send(AlertType.TRADE_EXECUTED, AlertLevel.INFO, f"Trade executed {order.request.symbol}",
                             f"{order.request.side.value} {order.request.units:.0f} @ {data['fill_price']}", data)
            if order.slippage_pips is not None and order.slippage_pips > self.risk_manager.limits.max_slippage_pips:
                self.alerts.send(AlertType.EXECUTION_ERROR, AlertLevel.WARNING, "High slippage",
                                 f"{order.slippage_pips:.1f} pips on {order.request.symbol}", data)
        elif kind in ("rejected", "unresolved", "anomaly"):
            self.alerts.send(AlertType.EXECUTION_ERROR, AlertLevel.WARNING if kind == "rejected" else AlertLevel.CRITICAL,
                             f"Order {kind}: {order.request.symbol}", order.error, data)
            if kind in ("unresolved", "anomaly"):
                self.safety.enter_safety_mode(f"order {order.client_order_id} {kind}")

    def _on_position_event(self, kind: str, data: dict[str, Any]) -> None:
        payload = {**data, "mode": self._mode.value}
        if "trade_id" in data and kind in ("position_opened", "position_closed"):
            self.store.save_position(payload)
        self.audit.record("execution_monitor", kind, payload)

    def _on_kill_switch(self, reason: str, actor: str) -> None:
        """Kill switch actions: cancel pending, apply emergency policy, keep monitoring, alert, audit."""
        actions: list[str] = []
        try:
            actions.append(f"cancelled {self.broker.cancel_pending_orders()} pending order(s)")
            if self.settings.emergency_close_policy is EmergencyClosePolicy.CLOSE_ALL:
                errs = self.execution.positions.close_all("kill_switch")
                actions.append("close_all requested" + (f" with errors: {errs}" if errs else ""))
            else:
                actions.append("positions kept with their stop-losses (policy keep_with_stops)")
        except ForexAIError as exc:
            actions.append(f"kill switch action failed: {exc}")
        self.alerts.send(AlertType.KILL_SWITCH, AlertLevel.CRITICAL, "KILL SWITCH ACTIVATED",
                         f"{reason} (by {actor}). " + "; ".join(actions))
        self.audit.record(actor, "kill_switch_activated", {"reason": reason, "actions": actions})
        self.store.save_state("safety", self.safety.to_dict())

    def _on_safety_mode(self, reason: str) -> None:
        self.alerts.send(AlertType.SAFETY_MODE, AlertLevel.CRITICAL, "Safety mode activated", reason,
                         dedup_key=reason)
        self.audit.record("system", "safety_mode", {"reason": reason})
        self.store.save_state("safety", self.safety.to_dict())

    def _on_closed_trade(self, trade: ClosedTrade, now: datetime) -> None:
        reached = self.portfolio.record_trade(trade, now)
        data = {"trade_id": trade.trade_id, "symbol": trade.symbol, "net_pnl": round(trade.net_pnl, 2),
                "gross_pnl": round(trade.gross_pnl, 2), "commission": round(trade.commission, 2),
                "swap": round(trade.swap, 2), "exit_reason": trade.exit_reason, "exit_price": trade.exit_price,
                "entry_price": trade.entry_price, "units": trade.units, "direction": trade.direction.value,
                "entry_time": trade.entry_time.isoformat(), "exit_time": trade.exit_time.isoformat(),
                "mode": self._mode.value}
        self.store.save_closed_trade(data)
        type_ = {"stop_loss": AlertType.STOP_LOSS_HIT, "take_profit": AlertType.TAKE_PROFIT_HIT}.get(
            trade.exit_reason, AlertType.TRADE_CLOSED)
        self.alerts.send(type_, AlertLevel.INFO, f"Trade closed {trade.symbol} ({trade.exit_reason})",
                         f"net {trade.net_pnl:+.2f} {self.settings.account_currency}", data)
        if reached:
            self._on_target_reached(now)

    def _on_target_reached(self, now: datetime) -> None:
        snap = self.portfolio.snapshot()
        net = snap["session"]["net_realized"]
        policy = self.settings.target_reached_policy
        actions: list[str] = []
        if policy is TargetReachedPolicy.BREAKEVEN:
            quotes = self._quotes([p.symbol for p in self.execution.positions.open_positions()])
            actions = [f"break-even {t}" for t in self.execution.positions.move_all_to_breakeven(quotes)]
        elif policy is TargetReachedPolicy.CLOSE_ALL:
            errs = self.execution.positions.close_all("session_target")
            actions = ["close_all requested"] + errs
        self.alerts.send(AlertType.SESSION_TARGET_REACHED, AlertLevel.INFO, "Session target reached",
                         f"net realised {net:.2f} >= {self.portfolio.target.target:.2f}. New entries suspended "
                         f"for this session. Policy: {policy.value}.", {"net_realized": net, "actions": actions})
        self.audit.record("master", "session_target_reached", {"net_realized": net, "policy": policy.value,
                                                               "actions": actions})

    # ================================================================ helpers
    def _quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out = {}
        for s in set(symbols):
            try:
                out[s] = self.broker.get_quote(s)
            except BrokerConnectionError:
                pass
        return out

    def _frames(self, symbol: str, now: datetime) -> dict[Timeframe, pd.DataFrame]:
        frames = {}
        for tf in self.timeframes:
            try:
                df = self.data.get_candles(symbol, tf, 300)
                frames[tf] = drop_incomplete_last_bar(df, tf, now)
            except ForexAIError as exc:
                log.warning("candles unavailable", extra={"ctx": {"symbol": symbol, "tf": tf.value, "err": str(exc)}})
        return frames

    def _risk_state(self, symbol: str, frames: dict[Timeframe, pd.DataFrame], quote: Quote | None,
                    mids: dict[str, float], correlations: dict[tuple[str, str], float], now: datetime,
                    entries_reasons: list[str]) -> RiskState:
        acct = self.broker.get_account()
        open_risk = []
        for p in self.execution.positions.open_positions():
            r = self.risk_manager.engine.position_risk(p.symbol, p.direction, p.units, p.entry_price, p.stop_loss, mids)
            open_risk.append(OpenRisk(p.symbol, p.direction, p.units, p.entry_price, p.stop_loss, r))
        entry = frames.get(self.entry_tf)
        return RiskState(
            mode=self._mode, account=acct, peak_equity=self.portfolio.peak_equity,
            day_start_equity=self.portfolio.day.start_equity if self.portfolio.day else acct.equity,
            week_start_equity=self.portfolio.week.start_equity if self.portfolio.week else acct.equity,
            daily_pnl=self.portfolio.daily_pnl(), weekly_pnl=self.portfolio.weekly_pnl(), open_positions=open_risk,
            quote=quote, mid_prices=mids, atr_spike_ratio=atr_spike_ratio(entry) if entry is not None else None,
            correlations=correlations, consecutive_losses=self.portfolio.consecutive_losses,
            session_target_reached=self.portfolio.target.reached, market_open=is_market_open(now), now=now)

    def _monitor_limits(self) -> None:
        lim = self.risk_manager.limits
        if self.portfolio.drawdown_pct >= lim.max_drawdown_pct:
            self.alerts.send(AlertType.DRAWDOWN_EXCESSIVE, AlertLevel.CRITICAL, "Maximum drawdown reached",
                             f"{self.portfolio.drawdown_pct:.2f}%", dedup_key="max_dd")
            self.safety.enter_safety_mode("maximum drawdown reached")
        day = self.portfolio.day
        if day and day.start_equity > 0 and -self.portfolio.daily_pnl() / day.start_equity * 100 >= lim.max_daily_loss_pct:
            self.alerts.send(AlertType.LOSS_LIMIT_REACHED, AlertLevel.WARNING, "Daily loss limit reached",
                             "No new entries until the next trading day.", dedup_key=f"daily:{day.start.date()}")
        week = self.portfolio.week
        if week and week.start_equity > 0 and -self.portfolio.weekly_pnl() / week.start_equity * 100 >= lim.max_weekly_loss_pct:
            self.alerts.send(AlertType.LOSS_LIMIT_REACHED, AlertLevel.WARNING, "Weekly loss limit reached",
                             "No new entries until next week.", dedup_key=f"weekly:{week.start.date()}")

    # ================================================================ cycle
    def run_cycle(self, now: datetime | None = None) -> CycleReport:
        now = now or utcnow()
        self._cycle_now = now
        report = CycleReport(now, False)
        try:
            self._cycle(now, report)
            self.safety.record_success()
        except BrokerConnectionError as exc:
            report.errors.append(f"broker: {exc}")
            self.alerts.send(AlertType.API_UNAVAILABLE, AlertLevel.CRITICAL, "Broker API unavailable", str(exc),
                             dedup_key="api")
            self.safety.record_error(str(exc))
        except Exception as exc:  # noqa: BLE001 - the loop must survive and report
            log.exception("cycle failure")
            report.errors.append(f"{type(exc).__name__}: {exc}")
            self.safety.record_error(str(exc))
        self.store.save_state("safety", self.safety.to_dict())
        self.store.save_state("portfolio", self.portfolio.to_state())
        self.last_cycle = report
        return report

    def _cycle(self, now: datetime, report: CycleReport) -> None:
        # 1. connectivity
        if not self.broker.is_connected():
            try:
                self.broker.connect()
            except BrokerConnectionError:
                self.alerts.send(AlertType.BROKER_DISCONNECTED, AlertLevel.CRITICAL, "Broker disconnected",
                                 "Reconnection failed. New entries blocked.", dedup_key="disconnect")
                self.safety.enter_safety_mode("broker disconnected")
                report.blocking_reasons.append("broker disconnected")
                return
        if "broker disconnected" in self.safety.state.safety_reasons:
            self.safety.clear_safety_reason("broker disconnected", "system")
            self.alerts.send(AlertType.SYSTEM, AlertLevel.INFO, "Broker reconnected", "")

        # 2. reconciliation
        rep = self.execution.reconcile()
        for t in rep.closed_trades:
            self._on_closed_trade(t, now)
        if rep.unknown_at_broker:
            ids = [p.position_id for p in rep.unknown_at_broker]
            self.alerts.send(AlertType.POSITION_ANOMALY, AlertLevel.CRITICAL, "Unknown position at broker",
                             f"{ids} not opened by this system. Entries blocked until reviewed.", {"ids": ids})
            self.safety.enter_safety_mode(f"unknown broker position(s) {ids}")
            for bp in rep.unknown_at_broker:
                if bp.stop_loss is None:
                    self.alerts.send(AlertType.POSITION_ANOMALY, AlertLevel.CRITICAL, "Position without stop-loss",
                                     f"{bp.position_id} {bp.symbol} has no stop-loss.", dedup_key=bp.position_id)
        for m in rep.mismatches:
            self.alerts.send(AlertType.POSITION_ANOMALY, AlertLevel.WARNING, "Position mismatch", m, dedup_key=m)
        for m in rep.missing_at_broker:
            self.alerts.send(AlertType.POSITION_ANOMALY, AlertLevel.WARNING, "Position closed without record",
                             m, dedup_key=m)

        # 3. account and limits
        acct = self.broker.get_account()
        positions = self.broker.get_positions()
        for ev in self.portfolio.update_account(acct, [p.unrealized_pnl for p in positions], now):
            self.audit.record("system", ev, {"equity": acct.equity})
        self._monitor_limits()

        # 4. position management (always active)
        open_syms = [p.symbol for p in self.execution.positions.open_positions()]
        if open_syms:
            quotes = self._quotes(open_syms)
            atrs, closes = {}, {}
            for s in set(open_syms):
                try:
                    df = drop_incomplete_last_bar(self.data.get_candles(s, self.entry_tf, 60), self.entry_tf, now)
                    atrs[s] = float(atr(df).iloc[-1])
                    closes[s] = float(df["close"].iloc[-1])
                except (ForexAIError, IndexError):
                    pass
            report.management_actions = self.execution.positions.manage(quotes, atrs, closes)

        # 5. entry gating
        reasons = self.safety.blocking_reasons()
        if self.portfolio.target.reached:
            reasons.append("session target reached")
        if not is_market_open(now):
            reasons.append("market closed")
        if self.trade_only_in_session and not in_trading_session(now, self.settings.session_start_utc,
                                                                 self.settings.session_end_utc):
            reasons.append("outside configured trading session")
        report.blocking_reasons = reasons
        report.entries_allowed = not reasons

        # 6. analysis
        all_items: list[tuple[Decision, TradeProposal | None]] = []
        frames_by_symbol: dict[str, dict[Timeframe, pd.DataFrame]] = {}
        quotes = self._quotes(self.settings.symbols)
        mids = {s: q.mid for s, q in quotes.items()}
        allow_syn = self._mode is TradingMode.SIMULATION
        for symbol in self.settings.symbols:
            frames = self._frames(symbol, now)
            frames_by_symbol[symbol] = frames
            ctx = MarketContext(symbol, now, frames, quotes.get(symbol), {"allow_synthetic": allow_syn})
            reports = {name: agent.analyze(ctx) for name, agent in self.agents.items()}
            self.latest_reports[symbol] = reports
            if not report.entries_allowed:
                continue
            entry = frames.get(self.entry_tf)
            validated = self.optimizer.eligible_strategies(symbol, allow_synthetic=allow_syn)
            setups = self.optimizer.strategy_setups(symbol, entry, allow_syn) if entry is not None and validated else []
            regime = (reports["market_analyst"].details.get("timeframes", {}).get(self.entry_tf.value, {})
                      .get("regime"))
            scores = {r.strategy: max(0.0, min(1.0, (r.oos_metrics.get("expectancy_r") or 0) * 2)) for r in validated}
            all_items += self.decision_engine.evaluate_symbol(symbol, reports, setups, {r.strategy for r in validated},
                                                              regime, now, scores)

        if not report.entries_allowed:
            return
        for d, _ in all_items:
            if d.status != "proposed":
                self._record(d, report)
        free = self.risk_manager.limits.max_open_positions - len(self.execution.positions.open_positions())
        chosen = self.decision_engine.rank(all_items, max(0, free))
        for d, _ in all_items:
            if d.status == "skipped" and d.setup is not None:
                self._record(d, report)

        # 7. risk review and execution
        h1 = {s: f.get(Timeframe.H1) for s, f in frames_by_symbol.items() if f.get(Timeframe.H1) is not None}
        corr = returns_correlation(h1)
        correlations = {(a, b): float(corr.loc[a, b]) for a in corr.index for b in corr.columns if a != b} \
            if not corr.empty else {}
        for d, proposal in chosen:
            sym = proposal.setup.symbol
            state = self._risk_state(sym, frames_by_symbol[sym], quotes.get(sym), mids, correlations, now, reasons)
            decision = self.risk_manager.review(proposal, state)
            d.risk_checks = {k: {"ok": ok, "detail": msg} for k, (ok, msg) in decision.checks.items()}
            if not decision.approved:
                d.status, d.reasons = "rejected_risk", decision.reasons
                self._record(d, report)
                continue
            try:
                order = self.execution.execute(proposal, decision, mids)
                d.order_id = order.client_order_id
                ok = order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
                d.status = "executed" if ok else "execution_failed"
                d.reasons = [f"order {order.status.value}"] + ([order.error] if order.error else [])
            except ForexAIError as exc:
                d.status, d.reasons = "execution_failed", [str(exc)]
                self.alerts.send(AlertType.EXECUTION_ERROR, AlertLevel.WARNING, f"Execution failed {sym}", str(exc))
            self._record(d, report)

    def _record(self, d: Decision, report: CycleReport) -> None:
        self.decisions.appendleft(d)
        report.decisions.append(d)
        self.store.save_decision({**d.to_dict(), "mode": self._mode.value})
        if d.status in ("executed", "rejected_risk", "execution_failed"):
            self.audit.record("master", f"decision_{d.status}", {"decision_id": d.decision_id, "symbol": d.symbol,
                                                                  "reasons": d.reasons})

    # ================================================================ views
    def status(self) -> dict[str, Any]:
        return {
            "mode": self._mode.value,
            "broker": {"name": self.broker.name, "environment": self.broker.environment,
                       "connected": self.broker.is_connected()},
            "safety": self.safety.to_dict(),
            "portfolio": self.portfolio.snapshot(),
            "risk_limits": self.risk_manager.limits.model_dump(),
            "open_positions": [p.to_dict() for p in self.execution.positions.open_positions()],
            "last_cycle": None if self.last_cycle is None else {
                "time": self.last_cycle.time.isoformat(), "entries_allowed": self.last_cycle.entries_allowed,
                "blocking_reasons": self.last_cycle.blocking_reasons, "errors": self.last_cycle.errors,
                "management_actions": self.last_cycle.management_actions},
        }

    def agents_view(self) -> list[dict[str, Any]]:
        out = []
        for name, agent in self.agents.items():
            per_symbol = {}
            for sym, reps in self.latest_reports.items():
                r = reps.get(name)
                if r is not None:
                    per_symbol[sym] = {"status": r.status.value, "signal": r.signal.value,
                                       "confidence": r.confidence, "summary": r.summary, "reasons": list(r.reasons),
                                       "time": r.timestamp.isoformat()}
            h = agent.health
            out.append({"name": name, "runs": h.runs, "errors": h.errors, "last_error": h.last_error,
                        "last_run": h.last_run.isoformat() if h.last_run else None,
                        "last_duration_ms": round(h.last_duration_ms, 1), "symbols": per_symbol})
        return out
