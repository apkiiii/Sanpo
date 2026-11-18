import { useState } from "react";
import type { Alert, Snapshot } from "../api";
import { api, time } from "../api";
import { Badge, Empty, Section } from "../components/ui";

export default function Security({ s, alerts, isAdmin, onChange }: { s: Snapshot; alerts: Alert[]; isAdmin: boolean; onChange: () => void }) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function act(path: string, body: unknown) {
    setBusy(true);
    setError("");
    try {
      await api(path, { method: "POST", body: JSON.stringify(body) });
      onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-3">
        <Section title="Kill switch" right={s.safety.kill_switch ? <Badge value="block" /> : <Badge value="ok" />}>
          {s.safety.kill_switch ? (
            <>
              <p className="mb-3 text-sm text-rose-300">Actif : {s.safety.kill_switch_reason}</p>
              <button
                className="btn bg-slate-700 hover:bg-slate-600"
                disabled={!isAdmin || busy}
                onClick={() => window.confirm("Réactiver les nouvelles entrées ?") && act("/api/killswitch/release", { confirm: true })}
              >
                Libérer le kill switch
              </button>
            </>
          ) : (
            <>
              <input
                className="mb-2 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
                placeholder="Motif (obligatoire)"
                value={reason}
                maxLength={200}
                onChange={(e) => setReason(e.target.value)}
              />
              <button
                className="btn w-full bg-rose-600 hover:bg-rose-500"
                disabled={!isAdmin || busy || reason.trim().length < 3}
                onClick={() => act("/api/killswitch/activate", { reason })}
              >
                ACTIVER LE KILL SWITCH
              </button>
            </>
          )}
          {!isAdmin && <p className="mt-2 text-xs text-slate-500">Rôle administrateur requis.</p>}
          {error && <p className="mt-2 text-xs text-rose-400">{error}</p>}
        </Section>

        <Section title="Mode sécurité" right={s.safety.safety_mode ? <Badge value="block" /> : <Badge value="ok" />}>
          {s.safety.safety_reasons.length ? (
            <>
              {s.safety.safety_reasons.map((r) => (
                <div key={r} className="text-sm text-amber-300">• {r}</div>
              ))}
              <button
                className="btn mt-3 bg-slate-700 hover:bg-slate-600"
                disabled={!isAdmin || busy}
                onClick={() => window.confirm("Les causes ont-elles été vérifiées ?") && act("/api/safety/reset", { confirm: true })}
              >
                Réinitialiser après vérification
              </button>
            </>
          ) : (
            <p className="text-sm text-slate-400">Inactif.</p>
          )}
        </Section>

        <Section title="Broker">
          <div className="space-y-1 text-sm">
            <div>{s.broker.name} ({s.broker.environment})</div>
            <div>{s.broker.connected ? <Badge value="ok" /> : <Badge value="block" />} {s.broker.connected ? "connecté" : "DÉCONNECTÉ"}</div>
            <div className="text-xs text-slate-400">Mode : {s.mode.toUpperCase()}</div>
          </div>
        </Section>
      </div>

      <Section title="Limites de risque (lecture seule)">
        <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm md:grid-cols-4">
          {Object.entries(s.risk_limits).map(([k, v]) => (
            <div key={k} className="flex justify-between border-b border-slate-800/60 py-1">
              <span className="text-slate-400">{k}</span>
              <span className="tabular-nums">{v}</span>
            </div>
          ))}
        </div>
        <p className="mt-2 text-xs text-slate-500">Modifiables uniquement par configuration (.env) et redémarrage.</p>
      </Section>

      <Section title={`Alertes (${alerts.length})`}>
        {alerts.length ? (
          <div className="max-h-[480px] space-y-1 overflow-y-auto">
            {alerts.map((a) => (
              <div key={a.id} className="flex gap-2 border-b border-slate-800/60 py-1 text-sm">
                <Badge value={a.level} />
                <div className="flex-1">
                  <div className="font-medium">{a.title}</div>
                  {a.message && <div className="text-xs text-slate-400">{a.message}</div>}
                </div>
                <span className="text-xs text-slate-500">{time(a.time)}</span>
              </div>
            ))}
          </div>
        ) : (
          <Empty text="Aucune alerte." />
        )}
      </Section>
    </div>
  );
}
