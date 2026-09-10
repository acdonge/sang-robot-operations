import type {
  AgentSnapshot,
  FleetRoute,
  RecordingInventory,
  RobotSource,
} from "./types";

export function formatNumber(value: number | null | undefined, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(value);
}

export function formatBytes(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  if (value < 1024) return `${formatNumber(value)} B`;
  if (value < 1024 ** 2) return `${formatNumber(value / 1024, 1)} KiB`;
  if (value < 1024 ** 3) return `${formatNumber(value / 1024 ** 2, 1)} MiB`;
  return `${formatNumber(value / 1024 ** 3, 2)} GiB`;
}

export function formatAge(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined) return "--";
  if (seconds < 1) return "now";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

export function formatLatency(milliseconds: number | null | undefined) {
  if (milliseconds === null || milliseconds === undefined) return "No data";
  if (milliseconds < 1000) return `${formatNumber(milliseconds)} ms`;
  return `${formatNumber(milliseconds / 1000, 1)} s`;
}

export function formatRate(rateHz: number | null, minimumRateHz: number | null = null) {
  if (rateHz === null || rateHz === undefined) return "-- Hz";
  const actual = formatNumber(rateHz, 1);
  if (minimumRateHz === null || minimumRateHz === undefined) return `${actual} Hz`;
  return `${actual} / ${formatNumber(minimumRateHz, 1)} Hz`;
}

export function formatClock(value: string | null | undefined) {
  if (!value) return "--:--:--";
  return new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    hour12: false,
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

export function formatSessionTime(value: string | null | undefined) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("en-US", {
    day: "2-digit",
    hour: "2-digit",
    hour12: false,
    minute: "2-digit",
    month: "short",
    second: "2-digit",
  }).format(new Date(value));
}

export function detailHash(agentId: string, robotId: string) {
  return `#robot/${encodeURIComponent(agentId)}/${encodeURIComponent(robotId)}`;
}

export function parseRoute(hash = window.location.hash): FleetRoute {
  const parts = hash.replace(/^#/, "").split("/");
  if (parts[0] !== "robot") return { view: "fleet" };
  try {
    return {
      agentId: parts[1] ? decodeURIComponent(parts[1]) : null,
      robotId: parts[2] ? decodeURIComponent(parts[2]) : null,
      view: "robot",
    };
  } catch {
    return { agentId: null, robotId: null, view: "robot" };
  }
}

export function agentNeedsAttention(agent: AgentSnapshot) {
  return agent.status !== "online" || agent.robots.some(
    (robot) => robot.fault || !robot.sources_fresh || robot.rates_ok === false,
  );
}

export function sourceHealth(source: RobotSource) {
  if (!source.fresh) return { color: "error", label: "Stale" } as const;
  if (source.rate_ok === false) return { color: "warning", label: "Low rate" } as const;
  return { color: "success", label: "Fresh" } as const;
}

export function recordingInventory(agent: AgentSnapshot | null | undefined): RecordingInventory {
  return agent?.recordings || {
    complete_count: 0,
    enabled: false,
    error_code: null,
    failed_count: 0,
    invalid_count: 0,
    partial_count: 0,
    recent: [],
    scanned_count: 0,
    state: "disabled",
    total_size_bytes: 0,
    truncated: false,
  };
}

export function statusColor(status: AgentSnapshot["status"]) {
  if (status === "online") return "success";
  if (status === "stale") return "warning";
  return "default";
}

export const EVENT_LABELS: Record<string, string> = {
  agent_offline: "Agent offline",
  agent_online: "Agent online",
  agent_recovered: "Agent recovered",
  agent_stale: "Agent stale",
  topic_rate_low: "Rate below minimum",
  topic_rate_recovered: "Rate recovered",
  topic_recovered: "Topic recovered",
  topic_stale: "Topic stale",
};
