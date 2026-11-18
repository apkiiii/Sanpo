#!/usr/bin/env python3
"""Walk-forward backtest + validation of all strategies, with Markdown/JSON reports.

Usage (verified data, files <DATA_DIR>/<SYMBOL>_M1.csv or _M5.csv, see data/historical/README.md):
    python scripts/run_backtest.py --data-dir data/historical --timeframe M15

Pipeline check without real data (results are labelled SYNTHETIC and can never validate a strategy):
    python scripts/run_backtest.py --synthetic

The validation table is written to --validation-out so that the runtime can use
the validated parameters (only strategies validated on real data can trade in DEMO/LIVE).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.agents.strategy_optimizer import StrategyOptimizer  # noqa: E402
from backend.backtesting.engine import BacktestConfig  # noqa: E402
from backend.backtesting.reports import save_report, walk_forward_markdown  # noqa: E402
from backend.backtesting.validation import assess_eligibility, walk_forward  # noqa: E402
from backend.core.instruments import MAJOR_PAIRS  # noqa: E402
from backend.core.types import Timeframe  # noqa: E402
from backend.data.market_data import CSVMarketDataProvider, validate_ohlc  # noqa: E402
from backend.data.synthetic import generate_ohlc  # noqa: E402
from backend.strategies import STRATEGIES  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data/historical")
    ap.add_argument("--symbols", default=",".join(MAJOR_PAIRS))
    ap.add_argument("--timeframe", default="H1", choices=[t.value for t in Timeframe])
    ap.add_argument("--strategies", default=",".join(STRATEGIES))
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--grid-limit", type=int, default=27)
    ap.add_argument("--synthetic", action="store_true", help="use generated prices (pipeline check only)")
    ap.add_argument("--synthetic-bars", type=int, default=12_000)
    ap.add_argument("--out", default="reports")
    ap.add_argument("--validation-out", default="var/strategy_validation.json")
    args = ap.parse_args()

    tf = Timeframe(args.timeframe)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    names = [s.strip() for s in args.strategies.split(",") if s.strip()]
    provider = CSVMarketDataProvider(args.data_dir)
    optimizer = StrategyOptimizer(tf, BacktestConfig(), n_folds=args.folds)
    results, notes = [], []
    for i, sym in enumerate(symbols):
        if args.synthetic:
            df = generate_ohlc(sym, datetime(2023, 1, 2, tzinfo=UTC), args.synthetic_bars, tf, seed=1000 + i)
            notes.append(f"{sym}: SYNTHETIC {len(df)} {tf.value} bars")
        else:
            try:
                df = provider.get_candles(sym, tf, 10**9)
            except Exception as exc:  # noqa: BLE001
                print(f"[skip] {sym}: {exc}")
                continue
            q = validate_ohlc(df, min_bars=2000)
            if not q.ok:
                print(f"[skip] {sym}: invalid data: {q.issues}")
                continue
            notes.append(f"{sym}: {df.attrs.get('source')} {len(df)} {tf.value} bars "
                         f"{df.index[0].date()} → {df.index[-1].date()}")
        for name in names:
            wf = walk_forward(sym, df, STRATEGIES[name], optimizer.config, args.folds, args.grid_limit)
            eligible, reasons = assess_eligibility(wf, optimizer.criteria)
            results.append((wf, eligible, reasons))
            m = wf.oos_metrics
            print(f"{sym:7} {name:16} trades={m['trades']:4d} PF={m['profit_factor']:.2f} "
                  f"expR={m['expectancy_r']:+.3f} DD={m['max_drawdown_pct']:.2f}% eligible={eligible}")
        if not args.synthetic:
            optimizer.validate(sym, df, names)
    if not results:
        print("No data processed. Provide verified CSV files or use --synthetic for a pipeline check.")
        return 1
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    kind = "synthetic" if args.synthetic else "historical"
    md = walk_forward_markdown(results, f"Backtest report ({kind}, {tf.value})", "; ".join(notes))
    data = [{"symbol": wf.symbol, "strategy": wf.strategy, "eligible": e, "reasons": r, "efficiency": wf.efficiency,
             "monte_carlo": wf.monte_carlo, "synthetic": wf.synthetic,
             "oos_metrics": {k: v for k, v in wf.oos_metrics.items()},
             "folds": [{"train": f.train, "test": f.test, "params": f.best_params, "oos": f.out_of_sample}
                       for f in wf.folds]} for wf, e, r in results]
    md_path, _ = save_report(md, {"generated_at": stamp, "data": notes, "results": data}, args.out,
                             f"backtest_{kind}_{tf.value}_{stamp}")
    if not args.synthetic:
        optimizer.save(args.validation_out)
        print(f"validation table written to {args.validation_out}")
    print(f"report written to {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
