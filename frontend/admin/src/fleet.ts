import { startTransition, useEffect, useState } from "react";
import type {
  AgentSnapshot,
  ConnectionState,
  FleetEvent,
  FleetSummary,
} from "./types";

const API = Object.freeze({
  agents: "/api/v1/agents",
  events: "/api/v1/events?limit=100",
  fleetSocket: "/api/v1/fleet",
  summary: "/api/v1/summary",
});

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (response.status === 401) {
    window.location.assign(`/login.html?next=${encodeURIComponent(window.location.href)}`);
    throw new Error("Authentication required");
  }
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json() as Promise<T>;
}

interface FleetPayload {
  agents: AgentSnapshot[];
  events: FleetEvent[];
  summary: FleetSummary;
  type?: string;
}

export function useFleetData() {
  const [agents, setAgents] = useState<AgentSnapshot[]>([]);
  const [events, setEvents] = useState<FleetEvent[]>([]);
  const [summary, setSummary] = useState<FleetSummary | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [connection, setConnection] = useState<ConnectionState>({
    detail: "Opening fleet stream",
    kind: "connecting",
    label: "Connecting",
  });

  function applyPayload(payload: FleetPayload) {
    startTransition(() => {
      setAgents(Array.isArray(payload.agents) ? payload.agents : []);
      setEvents(Array.isArray(payload.events) ? payload.events : []);
      setSummary(payload.summary || null);
    });
  }

  async function refresh() {
    setRefreshing(true);
    try {
      const [nextAgents, nextSummary, nextEvents] = await Promise.all([
        getJson<AgentSnapshot[]>(API.agents),
        getJson<FleetSummary>(API.summary),
        getJson<FleetEvent[]>(API.events),
      ]);
      applyPayload({ agents: nextAgents, events: nextEvents, summary: nextSummary });
      setConnection({
        detail: "Polling + fleet stream",
        kind: "live",
        label: "Central live",
      });
    } catch (error) {
      setConnection({
        detail: error instanceof Error ? error.message : "Unknown network error",
        kind: "error",
        label: "Central unavailable",
      });
    } finally {
      setRefreshing(false);
    }
  }

  useEffect(() => {
    let disposed = false;
    let reconnectTimer: number | undefined;
    let socket: WebSocket | null = null;

    function connect() {
      if (disposed) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(`${protocol}//${window.location.host}${API.fleetSocket}`);
      socket.addEventListener("open", () => {
        if (!disposed) setConnection({ detail: "Fleet stream connected", kind: "live", label: "Central live" });
      });
      socket.addEventListener("message", (event) => {
        if (disposed) return;
        const payload = JSON.parse(String(event.data)) as FleetPayload;
        if (payload.type === "fleet") applyPayload(payload);
      });
      socket.addEventListener("close", () => {
        if (disposed) return;
        setConnection({ detail: "Polling remains active", kind: "connecting", label: "Reconnecting" });
        reconnectTimer = window.setTimeout(connect, 2000);
      });
      socket.addEventListener("error", () => socket?.close());
    }

    void refresh();
    connect();
    const pollTimer = window.setInterval(() => void refresh(), 5000);
    return () => {
      disposed = true;
      window.clearInterval(pollTimer);
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, []);

  return { agents, connection, events, refresh, refreshing, summary };
}
