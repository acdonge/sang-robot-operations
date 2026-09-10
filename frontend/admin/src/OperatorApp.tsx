import {
  App as AntApp,
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Flex,
  InputNumber,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  type TableColumnsType,
} from "antd";
import {
  CameraOutlined,
  CheckCircleOutlined,
  CloudUploadOutlined,
  DatabaseOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { useEffect, useState } from "react";
import { errorMessage, getJson, requestJson } from "./api";
import AppShell from "./components/AppShell";
import MjpegCameraPanel, { type PreviewCamera } from "./components/MjpegCameraPanel";
import PageHeader from "./components/PageHeader";
import type {
  CaptureJob,
  CaptureWorker,
  Configuration,
  RecordingSessionDetail,
  RunAssetObservation,
  RunFleetPayload,
  TaskRun,
} from "./operations-types";
import type { ConnectionState, TopicTelemetry } from "./types";
import { formatBytes, formatLatency, formatNumber, formatRate, formatSessionTime, sourceHealth } from "./utils";

const { Text, Title } = Typography;
const ACTIVE_CAPTURE_STATES = new Set(["PENDING", "CLAIMED", "RECORDING", "FINALIZING", "UPLOADING"]);
const STOPPABLE_CAPTURE_STATES = new Set(["PENDING", "CLAIMED", "RECORDING", "FINALIZING"]);

interface PendingCaptureRequest {
  durationS: number;
  requestId: string;
  workerId: string;
}

interface TelemetryRow extends TopicTelemetry {
  agentId: string;
  health: ReturnType<typeof sourceHealth>;
  kind: string;
  robotId: string;
  rowKey: string;
  sourceKey: string;
}

function runFromHash() {
  const match = window.location.hash.match(/^#run\/(.+)$/);
  if (!match) return "";
  try { return decodeURIComponent(match[1]); } catch { return ""; }
}

function socketUrl(runId: string) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/v1/task-runs/${encodeURIComponent(runId)}/stream`;
}

function captureProgress(job: CaptureJob | null) {
  if (!job) return 0;
  if (["COMPLETE", "FAILED", "CANCELLED"].includes(job.state)) return 100;
  if (job.state === "PENDING") return 4;
  if (job.state === "CLAIMED") return 8;
  if (job.state === "FINALIZING") return 90;
  if (job.state === "UPLOADING") {
    const expected = Number(job.recording?.expected_file_count || 0);
    const completed = Number(job.recording?.completed_file_count || 0);
    return expected ? 92 + Math.round(7 * completed / expected) : 92;
  }
  const started = Date.parse(job.started_at || "");
  if (!Number.isFinite(started)) return 12;
  const elapsed = Math.max(0, Date.now() - started) / 1000;
  return Math.min(88, Math.max(12, Math.round(88 * elapsed / job.duration_s)));
}

function captureStateNote(job: CaptureJob | null) {
  if (!job) return "Waiting for Task Run capture";
  const labels: Record<string, string> = {
    CANCELLED: "Cancelled before capture started",
    CLAIMED: "Edge worker is preparing the session",
    COMPLETE: "Central integrity verification complete",
    FAILED: "Capture failed · source evidence retained",
    FINALIZING: "Creating the MCAP index and manifest",
    PENDING: "Waiting for an Edge worker",
    RECORDING: "Recording ROS 2 observations with the fixed profile",
    UPLOADING: "Uploading to and verifying the Central Object Store",
  };
  return labels[job.state] || job.state;
}

function newRequestId() {
  if (crypto.randomUUID) return `ui-${crypto.randomUUID()}`;
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return `ui-${[...bytes].map((value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function captureRequestStorageKey(runId: string, workerId: string) {
  return `sang.capture.request.${runId}.${workerId}`;
}

function runStateColor(state: string) {
  if (state === "ACTIVE") return "processing";
  if (state === "READY" || state === "COMPLETE") return "success";
  if (state === "FAILED") return "error";
  return "default";
}

function observationColor(state: string) {
  if (state === "HEALTHY") return "success";
  if (state === "DEGRADED" || state === "UNINSTRUMENTED") return "warning";
  return "error";
}

export default function OperatorApp() {
  const { message, modal } = AntApp.useApp();
  const [runs, setRuns] = useState<TaskRun[]>([]);
  const [runId, setRunId] = useState(runFromHash());
  const [payload, setPayload] = useState<RunFleetPayload | null>(null);
  const [connection, setConnection] = useState<ConnectionState>({ detail: "Choose a Task Run", kind: "connecting", label: "Run not selected" });
  const [workerId, setWorkerId] = useState<string | null>(null);
  const [durationS, setDurationS] = useState(600);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [completionError, setCompletionError] = useState<string | null>(null);
  const [integrity, setIntegrity] = useState<RecordingSessionDetail | null>(null);

  async function loadRuns() {
    try {
      const configuration = await getJson<Configuration>("/api/v1/configuration");
      setRuns(configuration.task_runs);
      const requested = runFromHash();
      if (requested && configuration.task_runs.some((run) => run.run_id === requested)) setRunId(requested);
    } catch (error) {
      setConnection({ detail: errorMessage(error), kind: "error", label: "Central unavailable" });
    }
  }

  useEffect(() => {
    void loadRuns();
    function updateRun() { setRunId(runFromHash()); }
    window.addEventListener("hashchange", updateRun);
    return () => window.removeEventListener("hashchange", updateRun);
  }, []);

  useEffect(() => {
    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    setPayload(null); setWorkerId(null); setIntegrity(null); setActionError(null); setCompletionError(null);
    if (!runId) {
      setConnection({ detail: "Choose a Task Run", kind: "connecting", label: "Run not selected" });
      return;
    }

    function apply(next: RunFleetPayload) {
      if (!disposed && next.task_run?.run_id === runId) {
        setPayload(next);
        setConnection({ detail: "Run-scoped live contract", kind: "live", label: "Run live" });
      }
    }

    async function connect() {
      try {
        apply(await getJson<RunFleetPayload>(`/api/v1/task-runs/${encodeURIComponent(runId)}/fleet`));
      } catch (error) {
        if (!disposed) setConnection({ detail: errorMessage(error), kind: "error", label: "Run unavailable" });
        return;
      }
      if (disposed) return;
      socket = new WebSocket(socketUrl(runId));
      socket.addEventListener("message", (event) => {
        try { apply(JSON.parse(String(event.data)) as RunFleetPayload); }
        catch { setConnection({ detail: "Invalid live payload", kind: "error", label: "Stream error" }); }
      });
      socket.addEventListener("close", () => {
        if (disposed) return;
        setConnection({ detail: "Retrying live stream", kind: "connecting", label: "Reconnecting" });
        reconnectTimer = window.setTimeout(connect, 1600);
      });
      socket.addEventListener("error", () => socket?.close());
    }

    void connect();
    return () => {
      disposed = true;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      if (socket) { socket.onclose = null; socket.close(); }
    };
  }, [runId]);

  const currentJob = payload?.capture_jobs.find((job) => ACTIVE_CAPTURE_STATES.has(job.state)) || payload?.capture_jobs[0] || null;
  const activeJob = currentJob && ACTIVE_CAPTURE_STATES.has(currentJob.state) ? currentJob : null;
  const workers = payload?.capture_workers || [];
  const selectedWorker = workers.find((worker) => worker.worker_id === workerId) || null;

  useEffect(() => {
    if (!workers.some((worker) => worker.worker_id === workerId)) setWorkerId(workers.find((worker) => worker.available)?.worker_id || workers[0]?.worker_id || null);
  }, [payload, workerId]);

  useEffect(() => {
    if (!runId || !payload) return;
    for (const worker of payload.capture_workers) {
      const key = captureRequestStorageKey(runId, worker.worker_id);
      try {
        const pending = JSON.parse(sessionStorage.getItem(key) || "null") as PendingCaptureRequest | null;
        if (pending && payload.capture_jobs.some((job) => job.request_id === pending.requestId)) sessionStorage.removeItem(key);
      } catch { /* Invalid browser storage is safely ignored. */ }
    }
  }, [payload, runId]);

  useEffect(() => {
    let disposed = false;
    if (!currentJob?.recording) { setIntegrity(null); return; }
    const sessionId = currentJob.session_id;
    async function refreshIntegrity() {
      try {
        const next = await getJson<RecordingSessionDetail>(`/api/v1/recording-sessions/${encodeURIComponent(sessionId)}`);
        if (!disposed) setIntegrity(next);
      } catch { /* The live job summary remains visible while detail is staging. */ }
    }
    void refreshIntegrity();
    const timer = window.setInterval(() => void refreshIntegrity(), 3000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [currentJob?.session_id, currentJob?.state]);

  async function refreshPayload() {
    if (!runId) return;
    setPayload(await getJson<RunFleetPayload>(`/api/v1/task-runs/${encodeURIComponent(runId)}/fleet`));
  }

  function pendingRequest(worker: CaptureWorker) {
    const key = captureRequestStorageKey(runId, worker.worker_id);
    let stored: PendingCaptureRequest | null = null;
    try { stored = JSON.parse(sessionStorage.getItem(key) || "null") as PendingCaptureRequest | null; } catch { /* ignored */ }
    if (stored?.workerId === worker.worker_id && stored.durationS === durationS) return stored;
    const next = { durationS, requestId: newRequestId(), workerId: worker.worker_id };
    try { sessionStorage.setItem(key, JSON.stringify(next)); } catch { /* ignored */ }
    return next;
  }

  async function startCapture() {
    if (!selectedWorker || !runId || busy) return;
    const pending = pendingRequest(selectedWorker);
    setBusy(true); setActionError(null);
    try {
      await requestJson("POST", `/api/v1/task-runs/${encodeURIComponent(runId)}/capture-jobs`, {
        duration_s: durationS, request_id: pending.requestId, worker_id: selectedWorker.worker_id,
      });
      await refreshPayload();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally { setBusy(false); }
  }

  async function stopCapture() {
    if (!currentJob || busy || !STOPPABLE_CAPTURE_STATES.has(currentJob.state)) return;
    setBusy(true); setActionError(null);
    try {
      await requestJson("POST", `/api/v1/capture-jobs/${encodeURIComponent(currentJob.job_id)}/stop`, {});
      await refreshPayload();
    } catch (error) { setActionError(errorMessage(error)); }
    finally { setBusy(false); }
  }

  const completedSession = currentJob ? payload?.recording_sessions.find((session) => session.session_id === currentJob.session_id) || null : null;
  const qualityState = completedSession?.quality?.state || "PENDING";
  const completionEnabled = payload?.task_run.state === "ACTIVE" && !activeJob && currentJob?.state === "COMPLETE" && completedSession?.state === "COMPLETE" && qualityState === "PASS" && !busy;

  async function completeRun() {
    if (!payload || !completionEnabled) return;
    setBusy(true); setCompletionError(null);
    try {
      const completed = await requestJson<TaskRun>("POST", `/api/v1/task-runs/${encodeURIComponent(payload.task_run.run_id)}/complete-after-capture`, { expected_revision: payload.task_run.revision, state: "COMPLETE" });
      setRuns((current) => current.map((run) => run.run_id === completed.run_id ? completed : run));
      await refreshPayload();
    } catch (error) { setCompletionError(errorMessage(error)); }
    finally { setBusy(false); }
  }

  function confirmComplete() {
    if (!payload) return;
    modal.confirm({ content: "This releases every reserved asset after the quality-approved capture.", okText: "Complete Task Run", onOk: completeRun, title: `Complete ${payload.task_run.display_name}?` });
  }

  function selectRun(nextRunId: string) {
    const nextHash = nextRunId ? `#run/${encodeURIComponent(nextRunId)}` : "";
    if (window.location.hash === nextHash) setRunId(nextRunId);
    else window.location.hash = nextHash;
  }

  const cameras: PreviewCamera[] = (payload?.agents || []).flatMap((agent) => agent.robots.flatMap((robot) => robot.sources.filter((source) => source.kind === "camera").map((source) => ({ agentStatus: agent.status, id: `${agent.agent_id}/${robot.robot_id}/${source.key}`, label: source.key, robotId: robot.robot_id, source }))));
  const telemetryRows: TelemetryRow[] = (payload?.agents || []).flatMap((agent) => agent.robots.flatMap((robot) => robot.sources.flatMap((source) => source.topics.map((topic, index) => ({ ...topic, agentId: agent.agent_id, health: sourceHealth(source), kind: source.kind, robotId: robot.robot_id, rowKey: `${agent.agent_id}:${robot.robot_id}:${source.key}:${index}`, sourceKey: source.key })))));
  const healthyCount = (payload?.assets || []).filter((asset) => asset.state === "HEALTHY").length;
  const capturePercent = captureProgress(currentJob);
  const expectedFiles = Number(integrity?.expected_file_count || currentJob?.recording?.expected_file_count || 0);
  const completeFiles = Number(integrity?.completed_file_count || currentJob?.recording?.completed_file_count || 0);
  const mcapBytes = integrity?.artifacts.filter((artifact) => artifact.path.endsWith(".mcap")).reduce((total, artifact) => total + artifact.size_bytes, 0) || currentJob?.recording?.total_size_bytes || 0;
  const integrityVerified = integrity?.state === "COMPLETE" && expectedFiles > 0 && completeFiles === expectedFiles && integrity.artifacts.length === expectedFiles && integrity.artifacts.every((artifact) => /^[0-9a-f]{64}$/.test(artifact.sha256));

  const assetColumns: TableColumnsType<RunAssetObservation> = [
    { dataIndex: "role", key: "role", render: (value) => <Tag>{value}</Tag>, title: "Role", width: 150 },
    { key: "asset", render: (_, row) => <span className="table-identity"><strong>{row.asset.display_name}</strong><small>{row.asset.asset_type} · {row.asset.asset_id}</small></span>, title: "Assigned asset" },
    { dataIndex: "access_mode", key: "access", title: "Access", width: 120 },
    { dataIndex: "detail", ellipsis: true, key: "detail", title: "Live detail" },
    { dataIndex: "state", key: "state", render: (state: string) => <Tag color={observationColor(state)}>{state}</Tag>, title: "State", width: 130 },
  ];
  const telemetryColumns: TableColumnsType<TelemetryRow> = [
    { key: "source", render: (_, row) => <span className="table-identity"><strong>{row.sourceKey}</strong><small>{row.robotId}</small></span>, title: "Source", width: 160 },
    { dataIndex: "kind", key: "kind", render: (value) => <Tag>{String(value).toUpperCase()}</Tag>, title: "Kind", width: 90 },
    { dataIndex: "topic", ellipsis: true, key: "topic", render: (value) => <Text code>{value}</Text>, title: "ROS 2 topic" },
    { key: "rate", render: (_, row) => formatRate(row.rate_hz, row.minimum_rate_hz), title: "Rate", width: 120 },
    { dataIndex: "age_ms", key: "age", render: formatLatency, title: "Age", width: 90 },
    { dataIndex: "health", key: "health", render: (health: TelemetryRow["health"]) => <Tag color={health.color}>{health.label}</Tag>, title: "Health", width: 100 },
  ];

  let captureMessage = "An ACTIVE Task Run and an online Edge worker are required.";
  if (actionError) captureMessage = actionError;
  else if (busy) captureMessage = "Central is processing the capture request.";
  else if (!payload) captureMessage = "Select a Task Run.";
  else if (payload.task_run.state !== "ACTIVE") captureMessage = "Capture can only start for an ACTIVE Task Run.";
  else if (!selectedWorker) captureMessage = "No Edge worker covers every asset in this Task Run.";
  else if (!selectedWorker.available) captureMessage = "The Edge worker heartbeat is too old to start.";
  else if (activeJob) captureMessage = `${activeJob.state} · only one capture session is allowed per worker.`;
  else captureMessage = "The fixed read-only profile records robot joints and camera topics together.";
  const canStart = payload?.task_run.state === "ACTIVE" && selectedWorker?.available === true && !activeJob && !busy && durationS >= 5 && durationS <= 28800;

  return (
    <AppShell activeKey="operator" connection={connection} contextTitle="Operator station" scopeDetail="Run-scoped observation and fixed-profile capture" scopeTitle="Bounded operator access">
      <div className="page-stack operator-page">
        <PageHeader actions={<Select onChange={selectRun} options={runs.map((run) => ({ label: `${run.display_name} · ${run.state}`, value: run.run_id }))} placeholder="Select a Task Run" showSearch style={{ minWidth: 320 }} value={runId || undefined} />} description="Observe assigned equipment and create bounded MCAP recordings without actuator access." eyebrow="RUN-SCOPED LIVE CONTRACT" title={payload?.task_run.display_name || "Operator station"} />

        {!payload ? <Card><Empty description="Select a Task Run to load its assigned assets, capture worker, and live observations" /></Card> : <>
          <Card className="operator-run-card" extra={<Tag color={runStateColor(payload.task_run.state)}>{payload.task_run.state}</Tag>} title={<Space><RobotOutlined />{payload.task_run.display_name}</Space>}>
            <Descriptions column={{ xs: 1, sm: 2, lg: 4 }} items={[
              { key: "id", label: "Run ID", children: <Text code>{payload.task_run.run_id}</Text> },
              { key: "recipe", label: "Task definition", children: <Text code>{payload.task_run.task_definition_id}@{payload.task_run.task_version}</Text> },
              { key: "workcell", label: "Workcell", children: payload.task_run.workcell_id || "All assigned assets" },
              { key: "revision", label: "Revision", children: payload.task_run.revision },
            ]} />
          </Card>

          <Card className="summary-card" styles={{ body: { padding: 0 } }}><Row>
            <OperatorStat title="Assigned" value={payload.assets.length} />
            <OperatorStat title="Healthy" value={healthyCount} />
            <OperatorStat danger={payload.assets.length > healthyCount} title="Attention" value={payload.assets.length - healthyCount} />
            <OperatorStat title="Cameras" value={cameras.length} />
          </Row></Card>

          <Card extra={<Tag color={currentJob?.state === "FAILED" ? "error" : currentJob?.state === "COMPLETE" ? "success" : currentJob ? "processing" : "default"}>{currentJob?.state || "IDLE"}</Tag>} title={<Space><DatabaseOutlined />Observation capture</Space>}>
            <Row className="operator-capture-grid" gutter={[24, 24]}>
              <Col xs={24} xl={8}>
                <div className="capture-column">
                  <label className="operator-field"><Text type="secondary">EDGE WORKER</Text><Select disabled={Boolean(activeJob) || busy} onChange={setWorkerId} options={workers.map((worker) => ({ label: `${worker.worker_id} · ${worker.available ? "ONLINE" : "STALE"} · ${worker.profile_id}`, value: worker.worker_id }))} placeholder="No available workers" value={workerId || undefined} /></label>
                  <label className="operator-field"><Text type="secondary">DURATION / SECONDS</Text><InputNumber disabled={Boolean(activeJob) || busy} max={28800} min={5} onChange={(value) => { setActionError(null); setDurationS(Number(value || 0)); }} step={5} value={durationS} /></label>
                  <Flex gap={8}><Button disabled={!canStart} icon={<PlayCircleOutlined />} loading={busy} onClick={() => void startCapture()} type="primary">Start capture</Button><Button danger disabled={busy || !currentJob || !STOPPABLE_CAPTURE_STATES.has(currentJob.state)} icon={<PauseCircleOutlined />} onClick={() => void stopCapture()}>Stop capture</Button></Flex>
                  <Alert message={captureMessage} showIcon type={actionError ? "error" : "info"} />
                </div>
              </Col>
              <Col xs={24} xl={8}>
                <div className="capture-column capture-pipeline-ant">
                  <Flex justify="space-between"><div><Text type="secondary">PIPELINE STATE</Text><strong>{captureStateNote(currentJob)}</strong></div><strong>{capturePercent}%</strong></Flex>
                  <Progress percent={capturePercent} showInfo={false} status={currentJob?.state === "FAILED" ? "exception" : currentJob?.state === "COMPLETE" ? "success" : "active"} />
                  <Descriptions column={1} items={[
                    { key: "job", label: "Job", children: <Text code>{currentJob?.job_id || "--"}</Text> },
                    { key: "session", label: "Session", children: <Text code>{currentJob?.session_id || "--"}</Text> },
                    { key: "started", label: "Started", children: formatSessionTime(currentJob?.started_at || currentJob?.created_at) },
                  ]} size="small" />
                </div>
              </Col>
              <Col xs={24} xl={8}>
                <div className="capture-column integrity-column-ant">
                  <Flex justify="space-between"><Text type="secondary">CENTRAL OBJECT STORE</Text><Tag color={integrityVerified ? "success" : "default"}>{integrityVerified ? "VERIFIED / SHA-256" : integrity?.state || "AWAITING SESSION"}</Tag></Flex>
                  <Row gutter={12}><Col span={12}><Statistic title="MCAP" value={formatBytes(mcapBytes)} /></Col><Col span={12}><Statistic title="Files" value={`${completeFiles} / ${expectedFiles || "--"}`} /></Col></Row>
                  <div className="integrity-hash-ant"><Text type="secondary">MANIFEST SHA-256</Text><Text code>{integrity?.manifest_sha256 || currentJob?.recording?.manifest_sha256 || "--"}</Text></div>
                  <Flex align="center" justify="space-between"><Text>Quality gate</Text><Tag color={qualityState === "PASS" ? "success" : qualityState === "FAIL" ? "error" : "default"}>{qualityState}</Tag></Flex>
                  <Button block href={currentJob ? `/catalog.html?${new URLSearchParams({ run_id: currentJob.run_id, session_id: currentJob.session_id })}` : "/catalog.html"} icon={<CloudUploadOutlined />}>View integrity in Catalog</Button>
                  <Button block disabled={!completionEnabled} icon={<CheckCircleOutlined />} onClick={confirmComplete} type="primary">Complete Task Run</Button>
                  {completionError ? <Alert message={completionError} showIcon type="error" /> : <Text type="secondary">{completionEnabled ? "Quality PASS verified · completion releases reservations." : "A quality-approved completed session is required."}</Text>}
                </div>
              </Col>
            </Row>
          </Card>

          <Card title="Task assets"><Table columns={assetColumns} dataSource={payload.assets} pagination={false} rowKey={(row) => `${row.role}:${row.asset.asset_id}`} scroll={{ x: 850 }} /></Card>
          <Card title="ROS 2 observation telemetry"><Table columns={telemetryColumns} dataSource={telemetryRows} pagination={false} rowKey="rowKey" scroll={{ x: 900 }} size="small" /></Card>
          <Card title={<Space><CameraOutlined />Task cameras</Space>}><MjpegCameraPanel cameras={cameras} identity={`operator:${runId}`} /></Card>
          <Alert description="This app observes Task Runs and creates fixed-profile recording jobs. It never sends robot control, policy execution, welder, or wire-feeder commands." icon={<SafetyCertificateOutlined />} message="Monitoring boundary" showIcon type="success" />
        </>}
      </div>
    </AppShell>
  );
}

function OperatorStat({ danger = false, title, value }: { danger?: boolean; title: string; value: number }) {
  return <Col className={`summary-cell${danger ? " stat-danger" : ""}`} xs={12} lg={6}><Statistic prefix={<RobotOutlined />} title={title} value={value} /></Col>;
}
