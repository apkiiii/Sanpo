import { useEffect, useState } from "react";
import { api } from "../api";

interface UpdateStatus { current: string; latest: string | null; available: boolean; supported: boolean; error?: string }

export default function UpdateBanner({ isAdmin }: { isAdmin: boolean }) {
  const [st, setSt] = useState<UpdateStatus | null>(null);
  const [state, setState] = useState<"idle" | "installing" | "error">("idle");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    const load = () => api<UpdateStatus>("/api/app/update").then(setSt).catch(() => undefined);
    load();
    const t = setInterval(load, 3_600_000);
    return () => clearInterval(t);
  }, []);

  if (!st?.supported || !st.available) return null;
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-xl border border-sky-500/50 bg-sky-500/10 p-3 text-sm">
      <div className="flex-1">
        <b>Mise à jour disponible : version {st.latest}</b> (installée : {st.current}).
        <div className="text-xs text-slate-400">
          {state === "installing" ? "Téléchargement et installation en cours… le logiciel va se fermer puis se rouvrir tout seul." : "Votre compte démo et vos réglages sont conservés."}
        </div>
        {msg && <div className="text-xs text-rose-400">{msg}</div>}
      </div>
      <button
        id="update-install"
        className="btn bg-sky-600 hover:bg-sky-500"
        disabled={!isAdmin || state === "installing"}
        onClick={async () => {
          setState("installing");
          setMsg("");
          try {
            await api("/api/app/update", { method: "POST" });
          } catch (e) {
            setState("error");
            setMsg((e as Error).message);
          }
        }}
      >
        {state === "installing" ? "Installation…" : "Installer la mise à jour"}
      </button>
    </div>
  );
}
