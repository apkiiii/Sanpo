import { useState } from "react";
import type { Snapshot } from "../api";
import { api, fmt, money, time } from "../api";
import { Badge, Progress, Section, Stat } from "../components/ui";

export default function Overview({ s, isAdmin, onChange }: { s: Snapshot; isAdmin: boolean; onChange: () => void }) {
  const p = s.portfolio;
  const openRisk = s.open_positions.reduce((a, x) => a + (x.initial_risk || 0), 0);
  const sess = p.session;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Balance" value={money(p.balance)} />
        <Stat label="Equity" value={money(p.equity)} hint={`Non réalisé ${money(p.unrealized_pnl)}`} />
        <Stat label="Profit net (total)" value={money(p.all_time.net_realized)} tone={p.all_time.net_realized >= 0 ? "good" : "bad"} />
        <Stat label="Drawdown" value={`${fmt(p.drawdown_pct)} %`} hint={`max ${s.risk_limits.max_drawdown_pct} %`} tone={p.drawdown_pct > s.risk_limits.max_drawdown_pct / 2 ? "warn" : undefined} />
        <Stat label="Profit session" value={money(sess.net_realized)} tone={sess.net_realized >= 0 ? "good" : "bad"} hint={`depuis ${time(sess.start)}`} />
        <Stat label="Risque ouvert" value={money(openRisk)} hint={`${fmt((openRisk / Math.max(1, p.balance)) * 100)} % du capital`} />
        <Stat label="Positions ouvertes" value={s.open_positions.length} hint={`max ${s.risk_limits.max_open_positions}`} />
        <Stat label="Trades (session / jour)" value={`${sess.trades} / ${p.day.trades}`} hint={`${p.consecutive_losses} perte(s) consécutive(s)`} />
      </div>

      {s.mode === "simulation" && <DemoAccount s={s} isAdmin={isAdmin} onChange={onChange} />}

      <Section title={`Objectif de session (${p.target.enabled ? "activé" : "désactivé"})`} right={p.target.reached ? <Badge value="executed" /> : null}>
        <div className="mb-2 flex justify-between text-sm">
          <span>{money(sess.net_realized)} réalisés nets</span>
          <span className="text-slate-400">cible {money(p.target.target)}</span>
        </div>
        <Progress value={p.target.progress} />
        <p className="mt-2 text-xs text-slate-400">
          Cible de performance, jamais une garantie. Elle ne modifie ni la taille des positions ni le risque.
          {p.target.reached && " Objectif atteint : nouvelles entrées suspendues pour la session."}
        </p>
      </Section>

      <div className="grid gap-3 md:grid-cols-3">
        <Section title="Session">
          <PeriodTable p={sess} />
        </Section>
        <Section title="Journée">
          <PeriodTable p={p.day} />
        </Section>
        <Section title="Semaine">
          <PeriodTable p={p.week} />
        </Section>
      </div>

      <Section title="Dernier cycle">
        {s.last_cycle ? (
          <div className="space-y-1 text-sm">
            <div>
              {time(s.last_cycle.time)} — entrées {s.last_cycle.entries_allowed ? <Badge value="ok" /> : <Badge value="block" />}
            </div>
            {s.last_cycle.blocking_reasons.map((r) => (
              <div key={r} className="text-amber-300">• {r}</div>
            ))}
            {s.last_cycle.errors.map((r) => (
              <div key={r} className="text-rose-400">• {r}</div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-slate-500">Aucun cycle exécuté.</p>
        )}
      </Section>
    </div>
  );
}

function PeriodTable({ p }: { p: Snapshot["portfolio"]["session"] }) {
  const rows: [string, string][] = [
    ["Profit brut", money(p.gross_pnl)],
    ["Commissions", money(p.commissions)],
    ["Swaps", money(p.swaps)],
    ["Coût slippage", money(p.slippage_cost)],
    ["Gains réalisés", money(p.realized_profit)],
    ["Pertes réalisées", money(p.realized_loss)],
    ["Net réalisé", money(p.net_realized)],
    ["Trades (G/P)", `${p.trades} (${p.wins}/${p.losses})`],
  ];
  return (
    <table>
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k}>
            <td className="text-slate-400">{k}</td>
            <td className="text-right tabular-nums">{v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DemoAccount({ s, isAdmin, onChange }: { s: Snapshot; isAdmin: boolean; onChange: () => void }) {
  const [amount, setAmount] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const value = Number(amount.replace(",", "."));
  const valid = Number.isFinite(value) && value > 0;
  const p = s.portfolio;

  async function move(kind: "deposit" | "withdraw") {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<{ balance: number }>(`/api/account/${kind}`, { method: "POST", body: JSON.stringify({ amount: value }) });
      setMsg({ ok: true, text: `${kind === "deposit" ? "Dépôt" : "Retrait"} de ${money(value)} effectué. Nouvelle balance : ${money(r.balance)}.` });
      setAmount("");
      onChange();
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="Compte démo (argent fictif)">
      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-2">
          <div className="flex gap-2">
            <input
              id="demo-amount"
              inputMode="decimal"
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
              placeholder="Montant en $ (ex. 40)"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
            />
          </div>
          <div className="flex gap-2">
            <button className="btn flex-1 bg-emerald-600 hover:bg-emerald-500" disabled={!isAdmin || !valid || busy} onClick={() => move("deposit")}>Déposer</button>
            <button className="btn flex-1 bg-slate-700 hover:bg-slate-600" disabled={!isAdmin || !valid || busy} onClick={() => move("withdraw")}>Retirer</button>
          </div>
          {msg && <p className={`text-sm ${msg.ok ? "text-emerald-300" : "text-rose-400"}`}>{msg.text}</p>}
          <p className="text-xs text-slate-400">
            Argent virtuel uniquement. Un dépôt ou un retrait ne compte ni comme un gain ni comme une perte, et ne déclenche aucune limite.
            Le retrait est limité à la marge libre si des positions sont ouvertes.
          </p>
        </div>
        <table>
          <tbody>
            <tr><td className="text-slate-400">Capital de départ</td><td className="text-right tabular-nums">{money(p.initial_balance)}</td></tr>
            <tr><td className="text-slate-400">Dépôts − retraits</td><td className="text-right tabular-nums">{money(p.net_deposits)}</td></tr>
            <tr><td className="text-slate-400">Balance actuelle</td><td className="text-right tabular-nums">{money(p.balance)}</td></tr>
            {s.cash_history.slice().reverse().slice(0, 5).map((c) => (
              <tr key={c.time + c.amount}>
                <td className="text-xs text-slate-500">{c.amount > 0 ? "Dépôt" : "Retrait"} · {time(c.time)}</td>
                <td className={`text-right text-xs tabular-nums ${c.amount > 0 ? "text-emerald-400" : "text-rose-400"}`}>{fmt(c.amount)} $</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
