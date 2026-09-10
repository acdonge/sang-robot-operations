import type { AgentSnapshot, RobotSource } from "./types";

export interface ProcessingNode {
  agent_ids: string[];
  display_name: string;
  lifecycle_state: string;
  node_id: string;
  revision: number;
}

export interface AssetBinding {
  agent_id: string;
  robot_id: string;
  source_key: string | null;
}

export interface Asset {
  asset_id: string;
  asset_type: string;
  binding: AssetBinding | null;
  capabilities: string[];
  display_name: string;
  lifecycle_state: string;
  node_id: string | null;
  revision: number;
  runtime_kind: string;
}

export interface Workcell {
  asset_ids: string[];
  description: string | null;
  display_name: string;
  lifecycle_state: string;
  revision: number;
  workcell_id: string;
}

export interface TaskRole {
  access_mode: string;
  asset_type: string;
  role: string;
}

export interface TaskDefinition {
  description: string | null;
  display_name: string;
  roles: TaskRole[];
  task_definition_id: string;
  version: number;
}

export interface WorkflowDraftRole {
  access_mode: "exclusive" | "shared_read";
  asset_type: string;
  cardinality: "1";
  requires: string[];
  role: string;
}

export interface WorkflowStep {
  capability?: string;
  depends_on: string[];
  kind: string;
  parameters?: Record<string, unknown>;
  role?: string;
  step_id: string;
  timeout_s?: number;
  [key: string]: unknown;
}

export interface WorkflowDraft {
  created_at: string;
  description: string | null;
  display_name: string;
  draft_id: string;
  published_at: string | null;
  revision: number;
  roles: WorkflowDraftRole[];
  source_task_definition_id: string | null;
  source_task_version: number | null;
  source_workflow_id: string | null;
  source_workflow_version: number | null;
  state: "DRAFT" | "PUBLISHED";
  steps: WorkflowStep[];
  task_definition_id: string;
  task_version: number;
  updated_at: string;
  workflow_id: string;
  workflow_version: number;
}

export interface WorkflowCompatibility {
  adapter_id: string;
  issues: string[];
  profile_sha256: string;
  runnable: boolean;
}

export interface WorkflowPublication {
  description: string | null;
  display_name: string;
  published_at: string;
  roles: TaskRole[];
  simulation_compatibility: WorkflowCompatibility[];
  source_draft_id: string;
  task_definition_id: string;
  task_version: number;
  workflow: {
    roles: Array<Record<string, unknown>>;
    schema_version: number;
    steps: WorkflowStep[];
    version: number;
    workflow_id: string;
  };
  workflow_id: string;
  workflow_sha256: string;
  workflow_version: number;
}

export interface WorkflowValidation {
  draft_id: string;
  issues: Array<{ location: string[]; message: string; type: string }>;
  valid: boolean;
  workflow_sha256?: string;
}

export interface SkillParameterProperty {
  type: "string" | "number" | "integer" | "boolean";
  title: string;
  description: string | null;
  default: unknown;
  const: unknown;
  enum: unknown[] | null;
  minimum: number | null;
  maximum: number | null;
  multipleOf: number | null;
  unit: string | null;
  readOnly: boolean;
}

export interface SkillDefinition {
  capability: string;
  category: string;
  description: string;
  execution_modes: Array<"SIMULATION" | "SHADOW" | "PHYSICAL">;
  parameter_schema: {
    type: "object";
    properties: Record<string, SkillParameterProperty>;
    required: string[];
    additionalProperties: false;
  };
  title: string;
}

export interface RobotActionDefinition {
  access_mode: "exclusive" | "shared_read";
  action_ref: string;
  asset_type: string;
  capability: string;
  default_timeout_s: number;
  description: string;
  editable_parameters: string[];
  execution_mode: "SIMULATION" | "SHADOW" | "PHYSICAL";
  fixed_parameters: Record<string, unknown>;
  robot_label: string;
  robot_type: string;
  role_template: string;
  script: {
    entrypoint: string;
    runtime: "UNITY_CSHARP" | "ROS2_PYTHON";
    script_ref: string;
    sha256: string;
  };
  step_id_template: string;
  title: string;
}

export type ActionValidationState = "PENDING" | "PASS" | "FAIL";

export interface ActionValidationReport {
  adapter_id?: string | null;
  adapter_profile_sha256?: string | null;
  central_executed_source?: false;
  checks: Array<{ check: string; state: "PASS"; value?: unknown }>;
  detail: string;
  evidence_run_id?: string | null;
  physical_output_connected?: false;
  source_sha256: string;
  state: ActionValidationState;
  validated_at: string;
}

export interface ActionLibraryEntry {
  access_mode: "exclusive" | "shared_read";
  action_id: string;
  action_ref: string;
  asset_type: string;
  capability: string;
  created_at: string | null;
  created_by: string;
  default_timeout_s: number;
  description: string;
  draft_id: string | null;
  editable_parameters: string[];
  execution_mode: "SIMULATION";
  fixed_parameters: Record<string, unknown>;
  origin: "BUILTIN" | "LIBRARY";
  published_at: string | null;
  revision: number;
  robot_label: string;
  robot_type: string;
  role_template: string;
  script: {
    entrypoint: string;
    file_name?: string;
    runtime: "UNITY_CSHARP" | "ROS2_PYTHON";
    script_ref?: string;
    sha256: string;
    size_bytes?: number;
  };
  simulation_validation: ActionValidationReport | null;
  simulation_validation_state: ActionValidationState;
  source_validation: ActionValidationReport | null;
  source_validation_state: ActionValidationState;
  state: "DRAFT" | "PUBLISHED";
  step_id_template: string;
  target_version: number;
  title: string;
  updated_at: string | null;
  validated_at: string | null;
}

export interface SimulationAdapterStatus {
  adapter_id: string;
  detail: string;
  execution_runtime?: "isaac";
  profile_sha256: string;
  ready: boolean;
  scenario_id: string;
  updated_at: string | null;
  worker_id: string | null;
}

export interface CommissioningCandidate {
  candidate_id: string;
  created_at: string;
  evidence_run_ids: string[];
  profile_id: string;
  profile_version: number;
  state: "SIMULATION_APPROVED";
  task_definition_id: string;
  task_version: number;
  workflow_id: string;
  workflow_sha256: string;
  workflow_version: number;
}

export interface SimulationValidationProfile {
  adapter_id: string;
  adapter_profile_sha256: string;
  commissioning_candidate: CommissioningCandidate | null;
  created_at: string;
  created_by: string;
  description: string | null;
  display_name: string;
  max_duration_s: number;
  passing_run_count: number;
  profile_id: string;
  qualified: boolean;
  require_all_steps_succeeded: true;
  require_output_isolated: true;
  require_reset_acknowledged: boolean;
  required_successful_runs: number;
  scenario_id: string;
  task_definition_id: string;
  task_version: number;
  version: number;
}

export interface SimulationValidationCheck {
  code: string;
  detail: string;
  passed: boolean;
}

export interface SimulationValidationReport {
  checks: SimulationValidationCheck[];
  evaluated_at: string | null;
  profile_id: string;
  profile_version: number;
  run_id: string;
  state: "PENDING" | "AWAITING_RESET" | "PASS" | "FAIL";
}

export interface UnityRuntimeStatus {
  configured: boolean;
  heartbeat: {
    age_s: number;
    lifecycle_state: string;
    observed_phase: string;
    ready: boolean;
    updated_at: string;
  } | null;
  last_exit_code: number | null;
  physical_output_connected: boolean;
  pid: number | null;
  preview: {
    age_s: number;
    captured_at: string;
    height: number;
    sequence: number;
    target_rate_hz: number;
    width: number;
  } | null;
  preview_available: boolean;
  ready: boolean;
  running: boolean;
  scenario_id: string | null;
  schema_version: 1;
  started_at: string | null;
}

export interface WorkflowSimulationRun {
  adapter_id: string;
  created_at: string;
  detail: string;
  display_name: string;
  finished_at: string | null;
  profile_sha256: string;
  progress: {
    cell_reason?: string | null;
    cell_state?: string | null;
    lifecycle_state?: "INITIALIZING" | "READY" | "RUNNING" | "HELD" | "ABORTED" | "COMPLETE" | "FAILED" | "RESETTING";
    steps: Array<{ state: string; step_id: string; updated_at?: string }>;
    unity_phase?: string;
    isaac_phase?: string;
  };
  controls: WorkflowSimulationControl[];
  reconciliation_required: boolean;
  result: Record<string, unknown> | null;
  run_id: string;
  scenario_id: string;
  started_at: string | null;
  state: "QUEUED" | "RUNNING" | "COMPLETE" | "FAILED" | "INTERRUPTED";
  task_definition_id: string;
  task_version: number;
  updated_at: string;
  validation: SimulationValidationReport | null;
  worker_id: string | null;
  workflow_id: string;
  workflow_sha256: string;
  workflow_version: number;
}

export interface WorkflowSimulationControl {
  action: "HOLD" | "RESUME" | "ABORT" | "RESET";
  control_id: string;
  created_at: string;
  detail: string;
  finished_at: string | null;
  run_id: string;
  state: "REQUESTED" | "CLAIMED" | "ACKNOWLEDGED" | "REJECTED";
  updated_at: string;
  worker_id: string | null;
}

export interface TaskAssignment {
  access_mode: string;
  asset?: Asset;
  asset_id: string;
  reserved?: boolean;
  role: string;
}

export interface TaskRun {
  assignments: TaskAssignment[];
  display_name: string;
  revision: number;
  run_id: string;
  state: string;
  task_definition_id: string;
  task_version: number;
  workcell_id: string | null;
}

export interface AgentRegistration {
  agent_id: string;
  display_name: string;
}

export interface Configuration {
  agent_registrations?: AgentRegistration[];
  assets: Asset[];
  audit: unknown[];
  processing_nodes: ProcessingNode[];
  task_definitions: TaskDefinition[];
  task_runs: TaskRun[];
  workcells: Workcell[];
}

export interface DiscoveryRobot {
  agent_id: string;
  agent_name: string;
  model: string;
  node_id: string | null;
  robot_id: string;
  runtime_kind: string;
  status: string;
}

export interface DiscoveryCamera {
  agent_id: string;
  agent_name: string;
  fresh: boolean;
  node_id: string | null;
  preview_url: string | null;
  rate_ok: boolean | null;
  robot_id: string;
  runtime_kind: string;
  source_key: string;
  status: string;
}

export interface Discovery {
  cameras: DiscoveryCamera[];
  robots: DiscoveryRobot[];
}

export interface QualitySummary {
  analyzed_at?: string;
  clock_state?: string;
  failed_check_count?: number;
  report_sha256?: string;
  state: string;
  warning_check_count?: number;
}

export interface RecordingSessionSummary {
  agent_id: string;
  completed_file_count: number;
  expected_file_count: number;
  manifest_sha256: string;
  profile_id: string;
  quality: QualitySummary | null;
  robot_id: string;
  run_id: string | null;
  session_id: string;
  started_at: string;
  state: string;
  total_size_bytes: number;
}

export interface RecordingTopic {
  asset_id: string;
  key: string;
  message_count: number;
  name: string;
  type: string;
}

export interface RecordingArtifact {
  download_url: string | null;
  path: string;
  sha256: string;
  size_bytes: number;
  state: string;
}

export interface RecordingSessionDetail extends RecordingSessionSummary {
  artifacts: RecordingArtifact[];
  source_time_range: { end_ns: number | string; start_ns: number | string };
  topics: RecordingTopic[];
}

export interface QualityStream {
  configured: { height: number | null; minimum_rate_hz: number | null; width: number | null };
  key: string;
  measured: {
    height: number | null;
    max_gap_ms: number | null;
    message_count: number;
    p95_gap_ms: number | null;
    p95_joint_skew_ms: number | null;
    rate_hz: number | null;
    timestamp_regressions: number;
    width: number | null;
  };
  state: string;
}

export interface QualityDetail {
  available: boolean;
  clock_state: string;
  report_url: string;
  state: string;
  streams: QualityStream[];
  summary: { failed_check_count: number; warning_check_count: number };
}

export interface DatasetDerivation {
  camera_topic_keys: string[];
  derivation_id: string;
  download_url: string;
  error: string | null;
  fps: number;
  frame_count: number | null;
  output_sha256: string | null;
  output_size_bytes: number | null;
  requested_end_offset_ms: number;
  requested_start_offset_ms: number;
  session_id: string;
  state: string;
  target_end_ns: number | string;
  target_start_ns: number | string;
}

export interface TimeStatus {
  central: {
    error_code: string | null;
    state: string;
    system_offset_ms: number | null;
  };
}

export interface PlaybackCamera {
  asset_id: string;
  key: string;
  message_count: number;
  topic: string;
}

export interface PlaybackInfo {
  cameras: PlaybackCamera[];
  duration_ms: number;
  frame_step_ms: number;
  max_camera_views: number;
  session_id: string;
}

export interface CaptureWorker {
  agent_id: string;
  available: boolean;
  profile_id: string;
  robot_id: string;
  worker_id: string;
}

export interface CaptureJob {
  created_at: string;
  duration_s: number;
  error: string | null;
  job_id: string;
  recording: {
    completed_file_count: number;
    expected_file_count: number;
    manifest_sha256: string;
    state: string;
    total_size_bytes: number;
  } | null;
  request_id: string;
  run_id: string;
  session_id: string;
  started_at: string | null;
  state: string;
  worker_id: string;
}

export interface RunAssetObservation {
  access_mode: string;
  asset: Asset;
  detail: string;
  role: string;
  state: string;
}

export interface RunFleetPayload {
  agents: AgentSnapshot[];
  assets: RunAssetObservation[];
  capture_jobs: CaptureJob[];
  capture_workers: CaptureWorker[];
  generated_at: string;
  recording_sessions: RecordingSessionSummary[];
  task_run: TaskRun;
}

export interface CameraChoice {
  agent: AgentSnapshot;
  id: string;
  robotId: string;
  source: RobotSource;
}
