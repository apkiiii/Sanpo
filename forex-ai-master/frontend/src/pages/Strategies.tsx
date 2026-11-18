import type { Dict } from "../api";
import { fmt, time } from "../api";
import { Badge, Empty, Section } from "../components/ui";

export default function Strategies({ rows }: { rows: Dict[] }) {
  return (
    <Section title="Validation des stratégies (walk-forward, hors échantillon)">
      {rows.length ? (
        <div className="overflow-x-auto">
          <table>
            <thead>
              <tr><th>Stratégie</th><th>Paire</th><th>Éligible</th><th>Trades OOS</th><th>PF</th><th>Espérance (R)</th><th>DD max %</th><th>Efficacité WF</th><th>Données</th><th>Validée le</th><th>Motifs</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.strategy}-${r.symbol}`}>
                  <td>{String(r.strategy)}</td>
                  <td className="font-mono">{String(r.symbol)}</td>
                  <td>{r.eligible ? <Badge value="ok" /> : <Badge value="block" />}</td>
                  <td className="tabular-nums">{String(r.trades)}</td>
                  <td className="tabular-nums">{fmt(Number(r.profit_factor))}</td>
                  <td className="tabular-nums">{fmt(Number(r.expectancy_r), 3)}</td>
                  <td className="tabular-nums">{fmt(Number(r.max_drawdown_pct))}</td>
                  <td className="tabular-nums">{fmt(Number(r.efficiency))}</td>
                  <td>{r.synthetic ? <Badge value="warning" /> : "réelles"}</td>
                  <td className="text-xs">{time(String(r.validated_at))}</td>
                  <td className="text-xs text-slate-400">{(r.reasons as string[]).join("; ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty text="Aucune stratégie validée. Lancez scripts/run_backtest.py sur des données historiques vérifiées." />
      )}
      <p className="mt-2 text-xs text-slate-500">Des résultats historiques ne sont jamais une promesse de profit futur.</p>
    </Section>
  );
}
