import type { Dict, Position } from "../api";
import { fmt, money, time } from "../api";
import { Badge, Empty, Section } from "../components/ui";

export default function Positions({ open, closed }: { open: Position[]; closed: Dict[] }) {
  return (
    <div className="space-y-4">
      <Section title={`Positions ouvertes (${open.length})`}>
        {open.length ? (
          <div className="overflow-x-auto">
            <table>
              <thead>
                <tr>
                  <th>Paire</th><th>Sens</th><th>Volume</th><th>Entrée</th><th>Stop-loss</th><th>Take-profit</th>
                  <th>P/L</th><th>Risque initial</th><th>Stratégie</th><th>Statut</th>
                </tr>
              </thead>
              <tbody>
                {open.map((p) => (
                  <tr key={p.trade_id} title={`${p.entry_reason}\nValidé par : ${p.validated_by.join(", ")}`}>
                    <td className="font-mono">{p.symbol}</td>
                    <td><Badge value={p.direction} /></td>
                    <td className="tabular-nums">{fmt(p.units, 0)}</td>
                    <td className="tabular-nums">{p.entry_price}</td>
                    <td className="tabular-nums">{p.stop_loss ?? "AUCUN"}</td>
                    <td className="tabular-nums">{p.take_profit ?? "—"}</td>
                    <td className={`tabular-nums ${p.unrealized_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>{money(p.unrealized_pnl)}</td>
                    <td className="tabular-nums">{money(p.initial_risk)}</td>
                    <td>{p.strategy || "—"}</td>
                    <td>{p.managed ? <Badge value={p.status} /> : <Badge value="critical" />}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty text="Aucune position ouverte." />
        )}
      </Section>
      <Section title={`Trades clôturés (${closed.length})`}>
        {closed.length ? (
          <div className="overflow-x-auto">
            <table>
              <thead>
                <tr><th>Paire</th><th>Sens</th><th>Volume</th><th>Entrée</th><th>Sortie</th><th>Net</th><th>Commission</th><th>Swap</th><th>Motif</th><th>Clôture</th></tr>
              </thead>
              <tbody>
                {closed.map((t) => (
                  <tr key={String(t.trade_id)}>
                    <td className="font-mono">{String(t.symbol)}</td>
                    <td><Badge value={String(t.direction)} /></td>
                    <td className="tabular-nums">{fmt(Number(t.units), 0)}</td>
                    <td className="tabular-nums">{String(t.entry_price)}</td>
                    <td className="tabular-nums">{fmt(Number(t.exit_price), 5)}</td>
                    <td className={`tabular-nums ${Number(t.net_pnl) >= 0 ? "text-emerald-400" : "text-rose-400"}`}>{money(Number(t.net_pnl))}</td>
                    <td className="tabular-nums">{money(Number(t.commission))}</td>
                    <td className="tabular-nums">{money(Number(t.swap))}</td>
                    <td>{String(t.exit_reason)}</td>
                    <td className="text-xs">{time(String(t.exit_time))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty text="Aucun trade clôturé." />
        )}
      </Section>
    </div>
  );
}
