import type { ReactNode } from "react";

export function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: "good" | "bad" | "warn" }) {
  const color = tone === "good" ? "text-emerald-400" : tone === "bad" ? "text-rose-400" : tone === "warn" ? "text-amber-300" : "text-slate-100";
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${color}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-slate-400">{hint}</div>}
    </div>
  );
}

const BADGE: Record<string, string> = {
  ok: "bg-emerald-500/15 text-emerald-300",
  long: "bg-emerald-500/15 text-emerald-300",
  executed: "bg-emerald-500/15 text-emerald-300",
  short: "bg-rose-500/15 text-rose-300",
  block: "bg-rose-500/15 text-rose-300",
  error: "bg-rose-500/15 text-rose-300",
  critical: "bg-rose-500/15 text-rose-300",
  rejected_risk: "bg-rose-500/15 text-rose-300",
  execution_failed: "bg-rose-500/15 text-rose-300",
  warning: "bg-amber-500/15 text-amber-300",
  insufficient_data: "bg-amber-500/15 text-amber-300",
  data_unavailable: "bg-amber-500/15 text-amber-300",
  rejected_master: "bg-amber-500/15 text-amber-300",
};

export function Badge({ value }: { value: string }) {
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${BADGE[value] ?? "bg-slate-700/50 text-slate-300"}`}>{value}</span>;
}

export function Section({ title, children, right }: { title: string; children: ReactNode; right?: ReactNode }) {
  return (
    <section className="card">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-slate-200">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}

export function Empty({ text }: { text: string }) {
  return <p className="py-4 text-center text-sm text-slate-500">{text}</p>;
}

export function Progress({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  return (
    <div className="h-2 w-full overflow-hidden rounded bg-slate-800">
      <div className="h-full bg-sky-500 transition-all" style={{ width: `${pct}%` }} />
    </div>
  );
}
