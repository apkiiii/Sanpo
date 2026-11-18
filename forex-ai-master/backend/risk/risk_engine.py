"""Risk engine — the rules behind BOT 5 (Risk Manager).

:meth:`RiskEngine.evaluate` runs *every* check and returns a
:class:`RiskDecision`. A trade is approved only if all checks pass. Approved
decisions carry a :class:`RiskApproval` signed with an HMAC key private to the
engine; the order manager refuses any order without a valid, unexpired
approval, so no other component (including the Master bot) can forge one.

The limits are a frozen :class:`RiskLimits` object, stored privately and exposed
read-only.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from backend.config.settings import RiskLimits, TradingMode
from backend.core.instruments import get_instrument, margin_required, price_move_value
from backend.core.types import AccountSnapshot, Direction, Quote, TradeProposal, utcnow
from backend.risk.position_sizing import size_position


@dataclass(frozen=True, slots=True)
class OpenRisk:
    """Risk carried by an open position (amount lost if its stop is hit)."""

    symbol: str
    direction: Direction
    units: float
    entry: float
    stop_loss: float | None
    risk_amount: float


@dataclass(slots=True)
class RiskState:
    """Everything the engine needs to evaluate a proposal. Built by the portfolio manager."""

    mode: TradingMode
    account: AccountSnapshot
    peak_equity: float
    day_start_equity: float
    week_start_equity: float
    daily_pnl: float  # realised + unrealised since day start (net)
    weekly_pnl: float
    open_positions: list[OpenRisk]
    quote: Quote | None
    mid_prices: dict[str, float]
    atr_spike_ratio: float | None = None
    correlations: dict[tuple[str, str], float] = field(default_factory=dict)
    consecutive_losses: int = 0
    kill_switch: bool = False
    safety_mode: bool = False
    session_target_reached: bool = False
    market_open: bool = True
    quote_max_age: timedelta = timedelta(seconds=30)
    now: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class RiskApproval:
    proposal_id: str
    symbol: str
    direction: str
    units: float
    stop_loss: float
    expires_at: datetime
    signature: str


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    proposal_id: str
    checks: dict[str, tuple[bool, str]] = field(default_factory=dict)
    units: float = 0.0
    risk_amount: float = 0.0
    risk_pct: float = 0.0
    approval: RiskApproval | None = None
    trigger_safety_mode: str | None = None

    @property
    def reasons(self) -> list[str]:
        return [f"{name}: {msg}" for name, (ok, msg) in self.checks.items() if not ok]


class RiskEngine:
    """Stateless rule evaluation with signed approvals."""

    APPROVAL_TTL = timedelta(seconds=60)

    def __init__(self, limits: RiskLimits, account_ccy: str = "USD") -> None:
        self.__limits = limits
        self.__key = secrets.token_bytes(32)
        self.account_ccy = account_ccy

    @property
    def limits(self) -> RiskLimits:
        """Read-only (frozen) view of the limits."""
        return self.__limits

    # ------------------------------------------------------------------ approval
    def _sign(self, proposal_id: str, symbol: str, direction: str, units: float, stop: float, exp: datetime) -> str:
        msg = f"{proposal_id}|{symbol}|{direction}|{units:.2f}|{stop:.6f}|{exp.isoformat()}".encode()
        return hmac.new(self.__key, msg, hashlib.sha256).hexdigest()

    def verify_approval(self, approval: RiskApproval, now: datetime | None = None) -> bool:
        """True if ``approval`` was issued by this engine and has not expired."""
        now = now or utcnow()
        if approval.expires_at < now:
            return False
        expected = self._sign(approval.proposal_id, approval.symbol, approval.direction, approval.units,
                              approval.stop_loss, approval.expires_at)
        return hmac.compare_digest(expected, approval.signature)

    # ------------------------------------------------------------------ helpers
    def _currency_exposure(self, positions: list[OpenRisk]) -> dict[str, float]:
        """Net risk per currency: long EURUSD adds +risk to EUR and -risk to USD."""
        exp: dict[str, float] = {}
        for p in positions:
            spec = get_instrument(p.symbol)
            exp[spec.base] = exp.get(spec.base, 0.0) + p.direction.sign * p.risk_amount
            exp[spec.quote] = exp.get(spec.quote, 0.0) - p.direction.sign * p.risk_amount
        return exp

    def _correlation(self, state: RiskState, a: str, b: str) -> float | None:
        return state.correlations.get((a, b), state.correlations.get((b, a)))

    # ------------------------------------------------------------------ main
    def evaluate(self, proposal: TradeProposal, state: RiskState) -> RiskDecision:
        lim = self.__limits
        s = proposal.setup
        spec = get_instrument(s.symbol)
        d = RiskDecision(False, proposal.proposal_id)
        c = d.checks
        equity, balance = state.account.equity, state.account.balance

        # --- global locks
        c["kill_switch"] = (not state.kill_switch, "kill switch active" if state.kill_switch else "off")
        c["safety_mode"] = (not state.safety_mode, "safety mode active" if state.safety_mode else "off")
        c["session_target"] = (not state.session_target_reached,
                               "session target reached: no new entries" if state.session_target_reached else "open")
        c["market_open"] = (state.market_open, "open" if state.market_open else "market closed")

        # --- stop-loss / take-profit sanity
        sl_ok = s.stop_loss > 0 and ((s.direction is Direction.LONG and s.stop_loss < s.entry) or
                                     (s.direction is Direction.SHORT and s.stop_loss > s.entry))
        c["stop_loss"] = (sl_ok, "valid" if sl_ok else "missing or wrong-side stop-loss")
        tp_ok = (s.direction is Direction.LONG and s.take_profit > s.entry) or \
                (s.direction is Direction.SHORT and s.take_profit < s.entry)
        c["take_profit"] = (tp_ok, "valid" if tp_ok else "take-profit on wrong side")
        stop_pips = spec.pips(s.risk_distance)
        c["min_stop"] = (stop_pips >= lim.min_stop_pips, f"{stop_pips:.1f} pips (min {lim.min_stop_pips})")
        rr = s.reward_risk
        c["reward_risk"] = (rr >= lim.min_reward_risk, f"{rr:.2f} (min {lim.min_reward_risk})")

        # --- execution conditions
        if state.quote is None:
            c["quote"] = (False, "no live quote")
        else:
            age = state.now - state.quote.time
            fresh = age <= state.quote_max_age
            c["quote"] = (fresh and state.quote.bid > 0 and state.quote.ask > state.quote.bid,
                          f"age {age.total_seconds():.0f}s" if fresh else "stale or invalid quote")
            spread_pips = spec.pips(state.quote.spread)
            c["spread"] = (spread_pips <= lim.max_spread_pips, f"{spread_pips:.2f} pips (max {lim.max_spread_pips})")
            ratio = state.quote.spread / s.risk_distance if s.risk_distance > 0 else 1.0
            c["spread_vs_stop"] = (ratio <= lim.max_spread_to_stop_ratio,
                                   f"{ratio:.0%} of stop (max {lim.max_spread_to_stop_ratio:.0%})")
            # Entry drift: the price must still be close to the planned entry.
            ref = state.quote.ask if s.direction is Direction.LONG else state.quote.bid
            drift = spec.pips(abs(ref - s.entry))
            c["entry_drift"] = (drift <= max(lim.max_slippage_pips, 0.25 * stop_pips),
                                f"{drift:.1f} pips from planned entry")
        if state.atr_spike_ratio is None:
            c["volatility"] = (False, "volatility unknown")
        else:
            c["volatility"] = (state.atr_spike_ratio <= lim.max_atr_spike_ratio,
                               f"ATR spike x{state.atr_spike_ratio:.2f} (max x{lim.max_atr_spike_ratio})")

        # --- loss limits (current and projected with this trade's risk)
        drawdown_pct = (state.peak_equity - equity) / state.peak_equity * 100 if state.peak_equity > 0 else 0.0
        if drawdown_pct >= lim.max_drawdown_pct:
            d.trigger_safety_mode = f"max drawdown reached ({drawdown_pct:.2f}%)"
        c["drawdown"] = (drawdown_pct < lim.max_drawdown_pct, f"{drawdown_pct:.2f}% (max {lim.max_drawdown_pct}%)")
        daily_loss_pct = -state.daily_pnl / state.day_start_equity * 100 if state.day_start_equity > 0 else 0.0
        c["daily_loss"] = (daily_loss_pct < lim.max_daily_loss_pct,
                           f"{daily_loss_pct:.2f}% (max {lim.max_daily_loss_pct}%)")
        weekly_loss_pct = -state.weekly_pnl / state.week_start_equity * 100 if state.week_start_equity > 0 else 0.0
        c["weekly_loss"] = (weekly_loss_pct < lim.max_weekly_loss_pct,
                            f"{weekly_loss_pct:.2f}% (max {lim.max_weekly_loss_pct}%)")

        # --- positions
        c["max_positions"] = (len(state.open_positions) < lim.max_open_positions,
                              f"{len(state.open_positions)} open (max {lim.max_open_positions})")
        dup = any(p.symbol == s.symbol for p in state.open_positions)
        c["duplicate_symbol"] = (not dup, "position already open on symbol" if dup else "none")

        # --- sizing (anti-martingale: exceptional risk never after a loss or in drawdown)
        exceptional_ok = (proposal.exceptional and state.consecutive_losses == 0 and state.daily_pnl >= 0
                          and drawdown_pct < lim.max_drawdown_pct / 2)
        risk_pct = lim.max_exceptional_risk_per_trade_pct if exceptional_ok else lim.max_risk_per_trade_pct
        try:
            sizing = size_position(balance, equity, risk_pct, s.entry, s.stop_loss, s.symbol, self.account_ccy,
                                   state.mid_prices)
        except Exception as exc:  # noqa: BLE001 - missing conversion price etc.
            c["sizing"] = (False, f"sizing failed: {exc}")
            return d
        c["sizing"] = (sizing.units > 0, sizing.reason or f"{sizing.units:.0f} units, {sizing.risk_pct:.3f}%")
        cap = min(balance, equity)
        new_risk_pct = sizing.risk_amount / cap * 100 if cap > 0 else 100.0
        c["risk_per_trade"] = (new_risk_pct <= risk_pct + 1e-9, f"{new_risk_pct:.3f}% (max {risk_pct}%)")

        open_risk = sum(p.risk_amount for p in state.open_positions)
        total_pct = (open_risk + sizing.risk_amount) / cap * 100 if cap > 0 else 100.0
        c["total_open_risk"] = (total_pct <= lim.max_total_open_risk_pct + 1e-9,
                                f"{total_pct:.3f}% (max {lim.max_total_open_risk_pct}%)")
        c["daily_loss_projected"] = (daily_loss_pct + total_pct <= lim.max_daily_loss_pct + 1e-9,
                                     f"worst case {daily_loss_pct + total_pct:.2f}% (max {lim.max_daily_loss_pct}%)")
        c["drawdown_projected"] = (drawdown_pct + total_pct <= lim.max_drawdown_pct + 1e-9,
                                   f"worst case {drawdown_pct + total_pct:.2f}% (max {lim.max_drawdown_pct}%)")

        new = OpenRisk(s.symbol, s.direction, sizing.units, s.entry, s.stop_loss, sizing.risk_amount)
        exposure = self._currency_exposure([*state.open_positions, new])
        worst_ccy = max(exposure.items(), key=lambda kv: abs(kv[1]), default=("-", 0.0))
        worst_pct = abs(worst_ccy[1]) / cap * 100 if cap > 0 else 100.0
        c["currency_exposure"] = (worst_pct <= lim.max_currency_exposure_pct + 1e-9,
                                  f"{worst_ccy[0]} {worst_pct:.3f}% (max {lim.max_currency_exposure_pct}%)")

        corr_issues = []
        for p in state.open_positions:
            rho = self._correlation(state, s.symbol, p.symbol)
            if rho is None:
                continue
            same_way = (rho > 0 and p.direction is s.direction) or (rho < 0 and p.direction is not s.direction)
            if abs(rho) >= lim.max_correlation and same_way:
                corr_issues.append(f"{p.symbol} rho={rho:+.2f}")
        c["correlation"] = (not corr_issues, ", ".join(corr_issues) or "ok")

        try:
            margin_new = margin_required(s.symbol, sizing.units, self.account_ccy, state.mid_prices)
            usage = (state.account.margin_used + margin_new) / equity * 100 if equity > 0 else 100.0
            c["margin"] = (usage <= lim.max_margin_usage_pct and margin_new <= state.account.free_margin,
                           f"{usage:.1f}% of equity (max {lim.max_margin_usage_pct}%)")
        except Exception as exc:  # noqa: BLE001
            c["margin"] = (False, f"margin check failed: {exc}")

        d.units, d.risk_amount, d.risk_pct = sizing.units, sizing.risk_amount, sizing.risk_pct
        d.approved = all(ok for ok, _ in c.values())
        if d.approved:
            exp = state.now + self.APPROVAL_TTL
            sig = self._sign(proposal.proposal_id, s.symbol, s.direction.value, sizing.units, s.stop_loss, exp)
            d.approval = RiskApproval(proposal.proposal_id, s.symbol, s.direction.value, sizing.units, s.stop_loss,
                                      exp, sig)
        return d

    def position_risk(self, symbol: str, direction: Direction, units: float, entry: float, stop: float | None,
                      mid_prices: dict[str, float]) -> float:
        """Amount lost if ``stop`` is hit (0 if the stop is at/after break-even, full notional if no stop)."""
        if stop is None:
            return float("inf")
        adverse = (entry - stop) * direction.sign
        if adverse <= 0:
            return 0.0
        return price_move_value(symbol, adverse, units, self.account_ccy, mid_prices)
