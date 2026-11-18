#!/usr/bin/env python3
"""Compte démo fictif : entraîne tout le système (7 bots + Bot Maître + Risk Manager + exécution)
sur plusieurs jours de marché simulé, avec une somme fictive, puis écrit un bilan.

Aucun ordre réel n'est jamais envoyé : le broker est le SimulatedBroker.

Exemples :
    python scripts/run_demo.py                          # 100 000 $ fictifs, 10 jours, prix synthétiques
    python scripts/run_demo.py --balance 10000 --days 20
    python scripts/run_demo.py --data csv --data-dir data/historical --days 60   # données historiques réelles
"""

from __future__ import annotations

import argparse
import collections
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.config.settings import Settings  # noqa: E402
from backend.core.logging import configure_logging  # noqa: E402
from backend.core.market_hours import is_market_open  # noqa: E402
from backend.runtime import TradingRuntime  # noqa: E402


def fmt(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--balance", type=float, default=100_000.0, help="somme fictive de départ (USD)")
    ap.add_argument("--days", type=int, default=10, help="jours de trading simulés")
    ap.add_argument("--symbols", default="EURUSD,GBPUSD,USDJPY,USDCHF,AUDUSD,USDCAD,NZDUSD")
    ap.add_argument("--data", choices=["synthetic", "csv"], default="synthetic")
    ap.add_argument("--data-dir", default="data/historical")
    ap.add_argument("--target", type=float, default=1000.0, help="objectif de session (0 = désactivé)")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--snapshot", default="", help="écrit aussi l'état complet du système en JSON (pour une vue HTML)")
    args = ap.parse_args()
    configure_logging("ERROR")

    settings = Settings(
        _env_file=None, trading_mode="simulation", initial_balance=args.balance,
        symbols=args.symbols, timeframes=["M15", "H1", "H4"], entry_timeframe="M15",
        simulation_data_source=args.data, historical_data_dir=args.data_dir, simulation_step_seconds=0,
        target_session_profit=args.target, target_enabled=args.target > 0,
        # Pas de calendrier économique disponible en démo : explicitement autorisé en SIMULATION seulement.
        block_when_calendar_unavailable=False,
        strategy_validation_path=str(ROOT / "var" / "strategy_validation.json"),
    )
    rt = TradingRuntime(settings)
    m = rt.master
    rt.broker.connect()
    start = rt.replay_cursor
    assert start is not None
    end = start + timedelta(days=args.days * 7 / 5)  # jours calendaires couvrant N jours ouvrés
    print(f"Compte démo fictif : {fmt(args.balance)} $ | {len(settings.symbols)} paires | "
          f"{args.days} jours | données {args.data.upper()}")
    t0, cycles, equity_curve, equity_times = time.time(), 0, [], []
    now = rt._advance_replay()
    while now is not None and now < end:
        if now.minute % 15 == 0 and is_market_open(now):  # analyse à chaque clôture M15
            m.run_cycle(now)
            cycles += 1
            equity_curve.append(m.portfolio.equity)
            equity_times.append(now.isoformat())
            if cycles % 96 == 0:
                print(f"  {now:%Y-%m-%d %H:%M}  equity {fmt(m.portfolio.equity)} $  "
                      f"trades {m.portfolio.all_time.trades}  ({time.time() - t0:.0f}s)")
        now = rt._advance_replay()
    m.run_cycle(rt.replay_cursor)

    # ------------------------------------------------------------------ bilan
    store = m.store
    trades = store.closed  # type: ignore[attr-defined]
    opened = {p["trade_id"]: p for p in store.positions.values()}  # type: ignore[attr-defined]
    pnl = [t["net_pnl"] for t in trades]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x <= 0]
    peak, max_dd = args.balance, 0.0
    for e in equity_curve:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100)
    risks = [p.get("initial_risk", 0) / args.balance * 100 for p in opened.values() if p.get("initial_risk")]
    statuses = collections.Counter(d["status"] for d in store.decisions)  # type: ignore[attr-defined]
    reasons = collections.Counter(r.split(":")[0] for d in store.decisions  # type: ignore[attr-defined]
                                  if d["status"] in ("rejected_master", "rejected_risk") for r in d["reasons"])
    targets = sum(1 for e in m.audit.entries if e.action == "session_target_reached")
    alerts = collections.Counter(a["type"] for a in m.alerts.recent(500))
    pf = sum(wins) / -sum(losses) if losses and sum(losses) < 0 else float("inf") if wins else 0.0
    acct = rt.broker.get_account()

    lines = [f"# Bilan du compte démo fictif — {datetime.now(UTC):%Y-%m-%d %H:%M} UTC", ""]
    if rt.synthetic:
        lines += ["> **DONNÉES SYNTHÉTIQUES** : prix générés aléatoirement. Ce bilan montre que le système fonctionne "
                  "(décisions, risque, exécution) ; il ne dit **rien** de la rentabilité sur le vrai marché.", ""]
    lines += [
        "## Paramètres", "",
        f"- Somme fictive de départ : **{fmt(args.balance)} $**",
        f"- Période simulée : {start:%Y-%m-%d} → {rt.replay_cursor:%Y-%m-%d} ({args.days} jours ouvrés, {cycles} cycles M15)",
        f"- Paires : {', '.join(settings.symbols)}",
        f"- Objectif de session : {fmt(args.target) + ' $' if args.target > 0 else 'désactivé'}",
        f"- Limites : {settings.risk_max_risk_per_trade_pct} %/trade, {settings.risk_max_daily_loss_pct} %/jour, "
        f"drawdown {settings.risk_max_drawdown_pct} %, {settings.risk_max_open_positions} positions max", "",
        "## Résultats", "",
        "| Indicateur | Valeur |", "|---|---|",
        f"| Balance finale | {fmt(acct.balance)} $ |",
        f"| Equity finale | {fmt(acct.equity)} $ |",
        f"| Profit net réalisé | {fmt(sum(pnl))} $ ({sum(pnl) / args.balance * 100:+.2f} %) |",
        f"| Trades clôturés | {len(pnl)} (gagnants {len(wins)}, perdants {len(losses)}) |",
        f"| Taux de réussite | {len(wins) / len(pnl) * 100 if pnl else 0:.1f} % |",
        f"| Profit factor | {pf:.2f} |",
        f"| Meilleur / pire trade | {fmt(max(pnl, default=0))} $ / {fmt(min(pnl, default=0))} $ |",
        f"| Drawdown maximal | {max_dd:.2f} % (limite {settings.risk_max_drawdown_pct} %) |",
        f"| Risque initial max par trade | {max(risks, default=0):.3f} % (limite {settings.risk_max_risk_per_trade_pct} %) |",
        f"| Positions encore ouvertes | {len(m.execution.positions.open_positions())} |",
        f"| Sessions où l'objectif a été atteint | {targets} |",
        f"| Kill switch / mode sécurité en fin de run | {m.safety.state.kill_switch} / {m.safety.state.safety_mode} |", "",
        "## Décisions du Bot Maître", "",
        "| Statut | Nombre |", "|---|---|",
        *[f"| {k} | {v} |" for k, v in statuses.most_common()], "",
        "Principaux motifs de refus :", "",
        *[f"- {k} ({v})" for k, v in reasons.most_common(8)], "",
        "## Alertes", "", *[f"- {k} : {v}" for k, v in alerts.most_common()], "",
        "## Trades", "",
        "| Paire | Sens | Volume | Entrée | Sortie | Net | Motif | Ouverture | Clôture |",
        "|---|---|---|---|---|---|---|---|---|",
        *[f"| {t['symbol']} | {t['direction']} | {t['units']:,.0f} | {t['entry_price']:.5f} | {t['exit_price']:.5f} | "
          f"{fmt(t['net_pnl'])} | {t['exit_reason']} | {t['entry_time'][:16]} | {t['exit_time'][:16]} |" for t in trades],
    ]
    out = ROOT / args.out
    out.mkdir(exist_ok=True)
    path = out / f"demo_{datetime.now(UTC):%Y%m%d_%H%M%S}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    if args.snapshot:
        import json
        snap = {
            "generated_at": datetime.now(UTC).isoformat(), "synthetic": rt.synthetic, "balance_start": args.balance,
            "period": [start.isoformat(), rt.replay_cursor.isoformat()],
            "status": rt.snapshot(), "agents": m.agents_view(),
            "decisions": [d.to_dict() for d in list(m.decisions)[:150]],
            "decision_counts": dict(statuses), "rejection_reasons": dict(reasons.most_common(10)),
            "risk_rejections": dict(m.risk_manager.rejection_counts),
            "closed_trades": trades, "alerts": m.alerts.recent(200),
            "equity": {"t": equity_times[::4], "v": [round(v, 2) for v in equity_curve[::4]]},
            "audit": m.audit.tail(40), "audit_valid": m.audit.verify()[0],
        }
        Path(args.snapshot).write_text(json.dumps(snap, default=str), encoding="utf-8")
        print(f"Snapshot : {args.snapshot}")
    print(f"\nBalance {fmt(args.balance)} $ -> {fmt(acct.balance)} $ | {len(pnl)} trades | "
          f"DD max {max_dd:.2f} % | risque max/trade {max(risks, default=0):.3f} %")
    print(f"Bilan : {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
