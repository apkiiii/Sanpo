// Typed client for the Forex AI Master API. The token lives in sessionStorage only.

export type Dict = Record<string, unknown>;

export interface Period {
  start: string;
  start_equity: number;
  gross_pnl: number;
  commissions: number;
  swaps: number;
  slippage_cost: number;
  realized_profit: number;
  realized_loss: number;
  net_realized: number;
  trades: number;
  wins: number;
  losses: number;
}

export interface Position {
  trade_id: string;
  symbol: string;
  direction: "long" | "short";
  units: number;
  entry_price: number;
  stop_loss: number | null;
  take_profit: number | null;
  initial_risk: number;
  reward_risk: number;
  entry_time: string;
  status: string;
  strategy: string;
  validated_by: string[];
  unrealized_pnl: number;
  managed: boolean;
  entry_reason: string;
}

export interface Decision {
  decision_id: string;
  symbol: string;
  time: string;
  status: string;
  reasons: string[];
  quality: number;
  setup: Dict | null;
  consensus: { supporting: string[]; opposing: string[]; vetoes: string[]; score: number } | null;
  risk_checks: Record<string, { ok: boolean; detail: string }> | null;
}

export interface SimulationStatus {
  available: boolean;
  running: boolean;
  step_seconds: number;
  replay_time: string | null;
}

export interface Snapshot {
  mode: string;
  simulation?: SimulationStatus;
  app_version?: string;
  cash_history: { time: string; amount: number }[];
  synthetic_data: boolean;
  replay_time: string | null;
  cycles: number;
  broker: { name: string; environment: string; connected: boolean };
  safety: { kill_switch: boolean; kill_switch_reason: string; safety_mode: boolean; safety_reasons: string[] };
  risk_limits: Record<string, number>;
  open_positions: Position[];
  portfolio: {
    balance: number;
    equity: number;
    peak_equity: number;
    drawdown: number;
    drawdown_pct: number;
    unrealized_pnl: number;
    consecutive_losses: number;
    initial_balance: number;
    net_deposits: number;
    session: Period;
    day: Period;
    week: Period;
    all_time: Period;
    target: { target: number; enabled: boolean; reached: boolean; reached_at: string | null; progress: number };
  };
  last_cycle: { time: string; entries_allowed: boolean; blocking_reasons: string[]; errors: string[] } | null;
  decisions: Decision[];
}

export interface AgentView {
  name: string;
  runs: number;
  errors: number;
  last_error: string;
  last_run: string | null;
  last_duration_ms: number;
  symbols: Record<string, { status: string; signal: string; confidence: number; summary: string; reasons: string[]; time: string }>;
}

export interface Alert {
  id: string;
  type: string;
  level: "info" | "warning" | "critical";
  title: string;
  message: string;
  time: string;
}

const TOKEN_KEY = "fam_token";

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: session only in memory */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const res = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers ?? {}),
    },
  });
  if (res.status === 401) setToken(null);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export async function login(username: string, password: string): Promise<void> {
  const r = await api<{ access_token: string }>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  setToken(r.access_token);
}

export async function setupAccount(username: string, password: string): Promise<void> {
  const r = await api<{ access_token: string }>("/api/auth/setup", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  setToken(r.access_token);
}

export function openFeed(onMessage: (msg: { type: string; data: unknown }) => void, onClose: () => void): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => ws.send(JSON.stringify({ token: getToken() }));
  ws.onmessage = (e) => onMessage(JSON.parse(e.data));
  ws.onclose = onClose;
  return ws;
}

export const fmt = (v: number | null | undefined, d = 2): string =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : v.toLocaleString("fr-FR", { minimumFractionDigits: d, maximumFractionDigits: d });

export const money = (v: number | null | undefined): string => (v === null || v === undefined ? "—" : `${fmt(v)} $`);

export const time = (iso: string | null | undefined): string => (iso ? new Date(iso).toLocaleString("fr-FR") : "—");
