import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentView, Alert, Decision, Dict, Snapshot } from "./api";
import { api, getToken, login, openFeed, setToken, setupAccount, time } from "./api";
import { Badge } from "./components/ui";
import Bots from "./pages/Bots";
import Master from "./pages/Master";
import Overview from "./pages/Overview";
import Positions from "./pages/Positions";
import Security from "./pages/Security";
import Strategies from "./pages/Strategies";

const TABS = ["Vue générale", "7 bots", "Bot maître", "Positions", "Stratégies", "Sécurité"] as const;
type Tab = (typeof TABS)[number];

export default function App() {
  const [authed, setAuthed] = useState(!!getToken());
  return authed ? <Dashboard onLogout={() => { setToken(null); setAuthed(false); }} /> : <Login onDone={() => setAuthed(true)} />;
}

function Login({ onDone }: { onDone: () => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [setup, setSetup] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api<{ login_enabled: boolean; setup_available: boolean }>("/api/auth/status")
      .then((s) => setSetup(s.setup_available))
      .catch(() => undefined);
  }, []);
  const tooShort = setup && password.length < 12;
  const mismatch = setup && confirm.length > 0 && confirm !== password;
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <form
        className="card w-full max-w-sm space-y-3"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError("");
          try {
            if (setup) await setupAccount(username, password);
            else await login(username, password);
            onDone();
          } catch (err) {
            setError((err as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <h1 className="text-lg font-semibold">Forex AI Master</h1>
        {setup && <p className="text-sm text-slate-400">Premier lancement : choisissez votre identifiant et un mot de passe (12 caractères minimum). Il protège l'accès au logiciel sur cet ordinateur.</p>}
        <input id="username" className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="Utilisateur" />
        <input id="password" className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2" type="password" autoComplete={setup ? "new-password" : "current-password"} value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Mot de passe" />
        {setup && <input id="confirm" className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder="Confirmer le mot de passe" />}
        {tooShort && password.length > 0 && <p className="text-xs text-amber-300">Encore {12 - password.length} caractère(s).</p>}
        {mismatch && <p className="text-xs text-amber-300">Les deux mots de passe sont différents.</p>}
        {error && <p className="text-sm text-rose-400">{error}</p>}
        <button className="btn w-full bg-sky-600 hover:bg-sky-500" disabled={busy || !password || tooShort || (setup && confirm !== password)}>
          {setup ? "Créer mon accès" : "Se connecter"}
        </button>
      </form>
    </div>
  );
}

function Dashboard({ onLogout }: { onLogout: () => void }) {
  const [tab, setTab] = useState<Tab>("Vue générale");
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [agents, setAgents] = useState<AgentView[]>([]);
  const [master, setMaster] = useState<{ accepted: Decision[]; rejected: Decision[] }>({ accepted: [], rejected: [] });
  const [positions, setPositions] = useState<{ open: Snapshot["open_positions"]; closed: Dict[] }>({ open: [], closed: [] });
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [strategies, setStrategies] = useState<Dict[]>([]);
  const [role, setRole] = useState("viewer");
  const [live, setLive] = useState(false);
  const [error, setError] = useState("");
  const wsRef = useRef<WebSocket | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, a, m, p, al, st, me] = await Promise.all([
        api<Snapshot>("/api/overview"),
        api<AgentView[]>("/api/agents"),
        api<{ accepted: Decision[]; rejected: Decision[] }>("/api/master"),
        api<{ open: Snapshot["open_positions"]; closed: Dict[] }>("/api/positions"),
        api<Alert[]>("/api/alerts"),
        api<Dict[]>("/api/strategies"),
        api<{ role: string }>("/api/me"),
      ]);
      setSnap(s); setAgents(a); setMaster(m); setPositions(p); setAlerts(al); setStrategies(st); setRole(me.role);
      setError("");
    } catch (e) {
      if (!getToken()) onLogout();
      setError((e as Error).message);
    }
  }, [onLogout]);

  useEffect(() => {
    refresh();
    const poll = setInterval(refresh, 10_000);
    let retry: ReturnType<typeof setTimeout>;
    const connect = () => {
      wsRef.current = openFeed(
        (msg) => {
          setLive(true);
          if (msg.type === "snapshot") setSnap(msg.data as Snapshot);
          if (msg.type === "alert") setAlerts((prev) => [msg.data as Alert, ...prev].slice(0, 200));
        },
        () => {
          setLive(false);
          retry = setTimeout(connect, 5000);
        },
      );
    };
    connect();
    return () => {
      clearInterval(poll);
      clearTimeout(retry);
      if (wsRef.current) { wsRef.current.onclose = null; wsRef.current.close(); }
    };
  }, [refresh]);

  const critical = alerts.filter((a) => a.level === "critical").length;
  return (
    <div className="mx-auto max-w-7xl space-y-4 p-4">
      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">Forex AI Master</h1>
        {snap && <Badge value={snap.mode === "live" ? "critical" : snap.mode === "demo" ? "warning" : "ok"} />}
        {snap && <span className="text-sm font-semibold uppercase">{snap.mode}</span>}
        <span className={`text-xs ${live ? "text-emerald-400" : "text-slate-500"}`}>● {live ? "temps réel" : "hors ligne"}</span>
        {snap?.replay_time && <span className="text-xs text-slate-400">rejeu : {time(snap.replay_time)}</span>}
        <button className="btn ml-auto bg-slate-800 hover:bg-slate-700" onClick={onLogout}>Déconnexion</button>
      </header>

      {snap?.synthetic_data && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-200">
          DONNÉES SYNTHÉTIQUES — démonstration du pipeline uniquement. Aucun résultat affiché n'a de valeur prédictive.
        </div>
      )}
      {snap && (snap.safety.kill_switch || snap.safety.safety_mode) && (
        <div className="rounded-lg border border-rose-500/40 bg-rose-500/10 p-3 text-sm text-rose-200">
          Nouvelles entrées bloquées : {[snap.safety.kill_switch && `kill switch (${snap.safety.kill_switch_reason})`, ...snap.safety.safety_reasons].filter(Boolean).join(" · ")}
        </div>
      )}
      {error && <div className="text-sm text-rose-400">{error}</div>}

      <nav className="flex flex-wrap gap-1">
        {TABS.map((t) => (
          <button key={t} className={`btn ${tab === t ? "bg-sky-600" : "bg-slate-800 hover:bg-slate-700"}`} onClick={() => setTab(t)}>
            {t}{t === "Sécurité" && critical > 0 ? ` (${critical})` : ""}
          </button>
        ))}
      </nav>

      {!snap ? (
        <p className="text-slate-400">Chargement…</p>
      ) : tab === "Vue générale" ? (
        <Overview s={snap} isAdmin={role === "admin"} onChange={refresh} />
      ) : tab === "7 bots" ? (
        <Bots agents={agents} />
      ) : tab === "Bot maître" ? (
        <Master accepted={master.accepted} rejected={master.rejected} />
      ) : tab === "Positions" ? (
        <Positions open={snap.open_positions} closed={positions.closed} />
      ) : tab === "Stratégies" ? (
        <Strategies rows={strategies} />
      ) : (
        <Security s={snap} alerts={alerts} isAdmin={role === "admin"} onChange={refresh} />
      )}
    </div>
  );
}
