"""Backtest reports (Markdown + JSON).

Every report states the data source. Reports produced on synthetic data carry a
prominent warning: they validate the *software*, not a strategy.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from backend.backtesting.validation import WalkForwardResult

DISCLAIMER = ("Past (simulated) performance is not a promise of future results. Costs, slippage and execution "
              "are modelled approximately.")
SYNTHETIC_BANNER = ("> **WARNING — SYNTHETIC DATA.** These results were produced on randomly generated prices. "
                    "They only demonstrate that the backtesting pipeline works. They have **no predictive value** "
                    "and must not be used to select or activate a strategy.")


def _fmt(v: Any, digits: int = 2) -> str:
    if isinstance(v, float):
        if math.isinf(v):
            return "∞"
        return f"{v:,.{digits}f}"
    return str(v)


def _table(rows: list[dict[str, Any]], cols: list[str]) -> str:
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(_fmt(r.get(c, "")) for c in cols) + " |")
    return "\n".join(out)


def walk_forward_markdown(results: list[tuple[WalkForwardResult, bool, list[str]]], title: str,
                          data_note: str) -> str:
    synthetic = any(wf.synthetic for wf, _, _ in results)
    lines = [f"# {title}", ""]
    if synthetic:
        lines += [SYNTHETIC_BANNER, ""]
    lines += [f"**Data:** {data_note}", "", f"*{DISCLAIMER}*", "", "## Summary (out-of-sample only)", ""]
    rows = []
    for wf, eligible, reasons in results:
        m = wf.oos_metrics
        rows.append({"strategy": wf.strategy, "symbol": wf.symbol, "trades": m["trades"],
                     "net_profit": m["net_profit"], "profit_factor": m["profit_factor"],
                     "win_rate_%": m["win_rate"] * 100, "expectancy_R": m["expectancy_r"],
                     "max_dd_%": m["max_drawdown_pct"], "sharpe": m["sharpe"], "sortino": m["sortino"],
                     "wf_efficiency": wf.efficiency, "mc_dd_p95_%": wf.monte_carlo.get("max_dd_p95", float("nan")),
                     "eligible": "yes" if eligible else "no"})
    lines.append(_table(rows, list(rows[0].keys()) if rows else ["strategy"]))
    lines += ["", "## Details", ""]
    for wf, eligible, reasons in results:
        m = wf.oos_metrics
        lines += [f"### {wf.strategy} — {wf.symbol}", "",
                  f"- Eligible: **{'yes' if eligible else 'no'}**" + ("" if eligible else f" ({'; '.join(reasons)})"),
                  f"- Out-of-sample trades: {m['trades']}, net {_fmt(m['net_profit'])}, "
                  f"commissions {_fmt(m['total_commission'])}, swaps {_fmt(m['total_swap'])}, "
                  f"slippage cost {_fmt(m['total_slippage_cost'])}",
                  f"- Average duration: {_fmt(m['avg_duration_hours'])} h; return {_fmt(m['return_pct'])} %",
                  f"- Monte Carlo ({wf.monte_carlo.get('runs', 0)} runs): return p5/p50/p95 = "
                  f"{_fmt(wf.monte_carlo.get('return_p5', float('nan')))} / "
                  f"{_fmt(wf.monte_carlo.get('return_p50', float('nan')))} / "
                  f"{_fmt(wf.monte_carlo.get('return_p95', float('nan')))} %, "
                  f"P(drawdown limit) = {_fmt(wf.monte_carlo.get('prob_dd_limit', float('nan')))}", "",
                  "| fold | train | test | params | IS score | OOS trades | OOS PF | OOS exp. R | plateau |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for f in wf.folds:
            lines.append(f"| {f.fold} | {f.train[0][:10]}→{f.train[1][:10]} | {f.test[0][:10]}→{f.test[1][:10]} | "
                         f"{f.best_params} | {_fmt(f.in_sample_score)} | {f.out_of_sample['trades']} | "
                         f"{_fmt(f.out_of_sample['profit_factor'])} | {_fmt(f.out_of_sample['expectancy_r'], 3)} | "
                         f"{f.plateau_ratio:.0%} |")
        for label, key in (("pair", "by_symbol"), ("session", "by_session")):
            groups = m.get(key, {})
            if groups:
                lines += ["", f"Performance by {label}:", "",
                          _table([{label: k, **v} for k, v in groups.items()],
                                 [label, "trades", "net_profit", "profit_factor", "win_rate", "expectancy_r"])]
        lines.append("")
    return "\n".join(lines)


def save_report(markdown: str, data: dict[str, Any], out_dir: str | Path, name: str) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md, js = out / f"{name}.md", out / f"{name}.json"
    md.write_text(markdown, encoding="utf-8")
    js.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return md, js
