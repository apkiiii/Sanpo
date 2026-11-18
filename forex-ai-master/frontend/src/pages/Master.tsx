import type { Decision } from "../api";
import { fmt, time } from "../api";
import { Badge, Empty, Section } from "../components/ui";

export default function Master({ accepted, rejected }: { accepted: Decision[]; rejected: Decision[] }) {
  return (
    <div className="space-y-4">
      <Section title={`Opportunités retenues (${accepted.length})`}>
        {accepted.length ? <DecisionList items={accepted} /> : <Empty text="Aucune opportunité retenue pour l'instant." />}
      </Section>
      <Section title={`Opportunités rejetées (${rejected.length})`}>
        {rejected.length ? <DecisionList items={rejected} /> : <Empty text="Aucun rejet enregistré." />}
      </Section>
    </div>
  );
}

function DecisionList({ items }: { items: Decision[] }) {
  return (
    <div className="space-y-2">
      {items.map((d) => (
        <details key={d.decision_id} className="rounded-lg border border-slate-800 p-2">
          <summary className="flex cursor-pointer flex-wrap items-center gap-2 text-sm">
            <span className="font-mono">{d.symbol}</span>
            <Badge value={d.status} />
            {d.setup && <Badge value={String(d.setup.direction)} />}
            {d.setup && <span className="text-slate-400">{String(d.setup.strategy)} · RR {String(d.setup.reward_risk)}</span>}
            <span className="ml-auto text-xs text-slate-500">{time(d.time)}</span>
          </summary>
          <div className="mt-2 grid gap-2 text-xs md:grid-cols-2">
            <div>
              <div className="label mb-1">Raisons</div>
              {d.reasons.map((r) => (
                <div key={r}>• {r}</div>
              ))}
              {d.consensus && (
                <div className="mt-2 text-slate-400">
                  Consensus {fmt(d.consensus.score)} · pour : {d.consensus.supporting.join(", ") || "—"} · contre : {d.consensus.opposing.join(", ") || "—"}
                </div>
              )}
            </div>
            {d.risk_checks && (
              <div>
                <div className="label mb-1">Validation du Risk Manager</div>
                {Object.entries(d.risk_checks).map(([k, c]) => (
                  <div key={k} className={c.ok ? "text-emerald-300" : "text-rose-300"}>
                    {c.ok ? "✓" : "✗"} {k} — {c.detail}
                  </div>
                ))}
              </div>
            )}
          </div>
        </details>
      ))}
    </div>
  );
}
