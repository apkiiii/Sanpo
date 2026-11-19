import { useState } from "react";
import type { SimulationStatus } from "../api";
import { api, time } from "../api";

const SPEEDS: { label: string; step: number }[] = [
  { label: "Lente", step: 2 },
  { label: "Normale", step: 1 },
  { label: "Rapide", step: 0.2 },
  { label: "Très rapide", step: 0.05 },
];

export default function SimulationControl({ sim, isAdmin, onChange }: { sim: SimulationStatus; isAdmin: boolean; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!sim.available) return null;

  async function call(path: string, init: RequestInit) {
    setBusy(true);
    setError("");
    try {
      await api(path, init);
      onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const current = SPEEDS.reduce((a, b) => (Math.abs(b.step - sim.step_seconds) < Math.abs(a.step - sim.step_seconds) ? b : a));
  return (
    <div className={`flex flex-wrap items-center gap-3 rounded-xl border p-3 ${sim.running ? "border-emerald-500/40 bg-emerald-500/10" : "border-slate-700 bg-slate-900/70"}`}>
      <button
        id="sim-toggle"
        className={`btn min-w-[220px] text-base font-semibold ${sim.running ? "bg-amber-600 hover:bg-amber-500" : "bg-emerald-600 hover:bg-emerald-500"}`}
        disabled={!isAdmin || busy}
        onClick={() => call(sim.running ? "/api/simulation/pause" : "/api/simulation/start", { method: "POST" })}
      >
        {sim.running ? "⏸  Mettre en pause" : "▶  Démarrer la simulation de trading"}
      </button>
      <div className="text-sm">
        <div className="font-medium">{sim.running ? "Simulation en marche : les bots analysent et tradent le compte démo" : "Simulation en pause : le marché est arrêté"}</div>
        <div className="text-xs text-slate-400">Heure du marché simulé : {time(sim.replay_time)}</div>
      </div>
      <label className="ml-auto flex items-center gap-2 text-sm text-slate-300" htmlFor="sim-speed">
        Vitesse
        <select
          id="sim-speed"
          className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5"
          value={current.step}
          disabled={!isAdmin || busy}
          onChange={(e) => call("/api/simulation/speed", { method: "PUT", body: JSON.stringify({ step_seconds: Number(e.target.value) }) })}
        >
          {SPEEDS.map((s) => (
            <option key={s.step} value={s.step}>
              {s.label} ({s.step < 1 ? `${Math.round(1 / s.step)} bougies/s` : `1 bougie / ${s.step} s`})
            </option>
          ))}
        </select>
      </label>
      {error && <p className="w-full text-sm text-rose-400">{error}</p>}
      {!isAdmin && <p className="w-full text-xs text-slate-500">Rôle administrateur requis.</p>}
    </div>
  );
}
