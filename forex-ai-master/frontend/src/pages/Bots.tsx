import type { AgentView } from "../api";
import { fmt, time } from "../api";
import { Badge, Empty, Section } from "../components/ui";

const LABELS: Record<string, string> = {
  market_analyst: "BOT 1 — Market Analyst",
  technical_analyst: "BOT 2 — Technical Analyst",
  price_action_analyst: "BOT 3 — Price Action Analyst",
  macro_analyst: "BOT 4 — News & Macro Analyst",
  risk_manager: "BOT 5 — Risk Manager (veto)",
  strategy_optimizer: "BOT 6 — Strategy Optimizer",
  execution_monitor: "BOT 7 — Execution & Trade Monitor",
};

export default function Bots({ agents }: { agents: AgentView[] }) {
  if (!agents.length) return <Empty text="Aucune donnée d'agent." />;
  return (
    <div className="grid gap-3 lg:grid-cols-2">
      {agents.map((a) => (
        <Section
          key={a.name}
          title={LABELS[a.name] ?? a.name}
          right={<span className="text-xs text-slate-400">{a.runs} exécutions · {a.errors} erreurs · {fmt(a.last_duration_ms, 0)} ms</span>}
        >
          {a.last_error && <p className="mb-2 text-xs text-rose-400">Dernière erreur : {a.last_error}</p>}
          <table>
            <thead>
              <tr>
                <th>Paire</th>
                <th>Statut</th>
                <th>Signal</th>
                <th>Conf.</th>
                <th>Analyse</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(a.symbols).map(([sym, r]) => (
                <tr key={sym} title={r.reasons.join("\n")}>
                  <td className="font-mono">{sym}</td>
                  <td><Badge value={r.status} /></td>
                  <td><Badge value={r.signal} /></td>
                  <td className="tabular-nums">{fmt(r.confidence)}</td>
                  <td className="text-xs text-slate-300">{r.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-xs text-slate-500">Dernière mise à jour : {time(a.last_run)}</p>
        </Section>
      ))}
    </div>
  );
}
