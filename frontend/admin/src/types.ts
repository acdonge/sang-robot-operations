export type AgentStatus = "online" | "stale" | "offline";

export interface TopicTelemetry {
  age_ms: number | null;
  fresh: boolean;
  minimum_rate_hz: number | null;
  rate_hz: number | null;
  rate_ok: boolean | null;
  seq: number;
  topic: string;
}

export interface RobotSource {
  fresh: boolean;
  freshness_ms: number;
  key: string;
  kind: "state" | "camera" | "action" | string;
  preview_url: string | null;
  rate_ok: boolean | null;
  required: boolean;
  topics: TopicTelemetry[];
  upstream_preview_available?: boolean;
}

export interface RecordingProgress {
  active: boolean;
  current_instruction: string | null;
  elapsed_s: number;
  episode_index: number;
  episodes_total: number;
  frames: number;
}

export interface RobotSnapshot {
  fault: string | null;
  fsm_state: string;
  model: string;
  phase: string;
  rates_ok: boolean | null;
  recording: RecordingProgress;
  robot_id: string;
  runtime_kind: string | null;
  sources: RobotSource[];
  sources_fresh: boolean;
}

export interface RecordingSession {
  finished_at: string | null;
  message_count: number | null;
  profile_id: string | null;
  robot_id: string | null;
  run_id: string | null;
  session_id: string;
  started_at: string | null;
  state: string;
  task_definition_id: string | null;
  task_definition_version: number | null;
  topic_count: number | null;
  total_size_bytes: number | null;
  transport_messages_lost: number | null;
}

export interface RecordingInventory {
  complete_count: number;
  enabled: boolean;
  error_code: string | null;
  failed_count: number;
  invalid_count: number;
  partial_count: number;
  recent: RecordingSession[];
  scanned_count: number;
  state: string;
  total_size_bytes: number;
  truncated: boolean;
}

export interface AgentStorage {
  cpu_pct: number | null;
  disk_free_gb: number | null;
  ram_free_gb: number | null;
}

export interface AgentSnapshot {
  agent_id: string;
  agent_name: string;
  age_s: number;
  clock_skew_s: number;
  event_counts: Record<string, number>;
  recordings: RecordingInventory;
  robots: RobotSnapshot[];
  status: AgentStatus;
  storage: AgentStorage;
  sw_version: string;
}

export interface FleetSummary {
  agents: {
    needs_attention: number;
    offline: number;
    online: number;
    stale: number;
    total: number;
  };
  generated_at: string;
  recordings?: {
    complete: number;
    failed: number;
    invalid: number;
    partial: number;
    total: number;
    total_size_bytes: number;
  };
  robots: {
    faulted: number;
    recording: number;
    total: number;
  };
  storage: {
    disk_free_gb_min: number | null;
  };
  sync?: {
    supported: boolean;
  };
}

export interface FleetEvent {
  agent_id: string;
  event_type: string;
  id: number | string;
  message: string;
  occurred_at: string;
  robot_id: string | null;
  severity: "info" | "warning" | "critical" | string;
  source_key: string | null;
  topic: string | null;
}

export type FleetRoute =
  | { view: "fleet" }
  | { view: "robot"; agentId: string | null; robotId: string | null };

export type ConnectionKind = "live" | "connecting" | "error";

export interface ConnectionState {
  detail: string;
  kind: ConnectionKind;
  label: string;
}
