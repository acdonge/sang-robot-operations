import {
  App as AntApp,
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Divider,
  Empty,
  Flex,
  List,
  Progress,
  Row,
  Select,
  Slider,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  type TableColumnsType,
} from "antd";
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseOutlined,
  DatabaseOutlined,
  DownloadOutlined,
  FileSearchOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  StepBackwardOutlined,
} from "@ant-design/icons";
import { useEffect, useRef, useState } from "react";
import { errorMessage, getJson } from "./api";
import AppShell from "./components/AppShell";
import PageHeader from "./components/PageHeader";
import type {
  Configuration,
  DatasetDerivation,
  PlaybackCamera,
  PlaybackInfo,
  QualityDetail,
  RecordingSessionDetail,
  RecordingSessionSummary,
  TimeStatus,
} from "./operations-types";
import type { ConnectionState } from "./types";
import { formatBytes, formatNumber, formatSessionTime } from "./utils";

const { Text, Title } = Typography;

interface Filters {
  agentId: string;
  runId: string;
  state: string;
}

interface PlaybackFrame {
  offsetMs: number | null;
  skewMs: number | null;
  sourceDeltaMs: number | null;
  url: string;
}

interface PlaybackFrameRequest {
  bucketMs: number;
  epoch: number;
  generation: number;
  selected: string[];
  sessionId: string;
}

interface LoadedPlaybackFrame {
  cameraKey: string;
  frame: PlaybackFrame;
}

function stateColor(value: string | null | undefined) {
  if (value === "COMPLETE" || value === "PASS" || value === "LOCKED") return "success";
  if (value === "STAGING" || value === "RUNNING" || value === "WARNING") return "warning";
  if (value === "FAIL" || value === "FAILED") return "error";
  return "default";
}

function formatDuration(milliseconds: number) {
  const totalTenths = Math.max(0, Math.round(milliseconds / 100));
  const minutes = Math.floor(totalTenths / 600);
  const seconds = Math.floor((totalTenths % 600) / 10);
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${totalTenths % 10}`;
}

function waitForRetry(delayMs: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("Playback request aborted", "AbortError"));
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export default function CatalogApp() {
  const initialQuery = new URLSearchParams(window.location.search);
  const { message } = AntApp.useApp();
  const [filters, setFilters] = useState<Filters>({ agentId: "", runId: initialQuery.get("run_id") || "", state: "" });
  const [configuration, setConfiguration] = useState<Configuration | null>(null);
  const [sessions, setSessions] = useState<RecordingSessionSummary[]>([]);
  const [derivations, setDerivations] = useState<DatasetDerivation[]>([]);
  const [timeStatus, setTimeStatus] = useState<TimeStatus | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(initialQuery.get("session_id"));
  const [detail, setDetail] = useState<RecordingSessionDetail | null>(null);
  const [quality, setQuality] = useState<QualityDetail | null>(null);
  const [playbackSessionId, setPlaybackSessionId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [connection, setConnection] = useState<ConnectionState>({ detail: "Loading recording index", kind: "connecting", label: "Connecting" });

  async function load(nextFilters = filters) {
    setLoading(true);
    setConnection({ detail: "Synchronizing metadata", kind: "connecting", label: "Synchronizing" });
    try {
      const sessionParameters = new URLSearchParams({ limit: "500" });
      if (nextFilters.runId) sessionParameters.set("run_id", nextFilters.runId);
      if (nextFilters.agentId) sessionParameters.set("agent_id", nextFilters.agentId);
      if (nextFilters.state) sessionParameters.set("state", nextFilters.state);
      const derivationParameters = new URLSearchParams({ limit: "500" });
      if (nextFilters.runId) derivationParameters.set("run_id", nextFilters.runId);
      const [nextConfiguration, nextSessions, nextDerivations, nextTimeStatus] = await Promise.all([
        getJson<Configuration>("/api/v1/configuration"),
        getJson<RecordingSessionSummary[]>(`/api/v1/recording-sessions?${sessionParameters}`),
        getJson<DatasetDerivation[]>(`/api/v1/dataset-derivations?${derivationParameters}`),
        getJson<TimeStatus>("/api/v1/time-status"),
      ]);
      setConfiguration(nextConfiguration);
      setSessions(nextSessions);
      setDerivations(nextDerivations);
      setTimeStatus(nextTimeStatus);
      if (selectedId && !nextSessions.some((session) => session.session_id === selectedId)) {
        setSelectedId(null); setDetail(null); setQuality(null); setPlaybackSessionId(null);
      }
      setConnection({ detail: "Central object catalog", kind: "live", label: "Catalog live" });
    } catch (error) {
      setConnection({ detail: errorMessage(error), kind: "error", label: "Catalog unavailable" });
      void message.error(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }

  async function selectSession(sessionId: string) {
    setSelectedId(sessionId);
    if (playbackSessionId && playbackSessionId !== sessionId) setPlaybackSessionId(null);
    try {
      const encoded = encodeURIComponent(sessionId);
      const [nextDetail, nextQuality] = await Promise.all([
        getJson<RecordingSessionDetail>(`/api/v1/recording-sessions/${encoded}`),
        getJson<QualityDetail>(`/api/v1/recording-sessions/${encoded}/quality`),
      ]);
      setDetail(nextDetail);
      setQuality(nextQuality);
    } catch (error) {
      setConnection({ detail: errorMessage(error), kind: "error", label: "Detail unavailable" });
    }
  }

  useEffect(() => {
    void load(filters).then(() => {
      const requested = initialQuery.get("session_id");
      if (requested) void selectSession(requested);
    });
  }, []);

  const completeCount = sessions.filter((session) => session.state === "COMPLETE").length;
  const stagingCount = sessions.filter((session) => session.state === "STAGING").length;
  const totalSize = sessions.reduce((total, session) => total + Number(session.total_size_bytes || 0), 0);
  const derivedCount = derivations.filter((item) => item.state === "COMPLETE").length;
  const clock = timeStatus?.central;

  const sessionColumns: TableColumnsType<RecordingSessionSummary> = [
    { dataIndex: "session_id", key: "session", render: (value: string, row) => <span className="table-identity"><strong>{value}</strong><small>{row.run_id || "Unscoped capture"}</small></span>, title: "Session / Task", width: 300 },
    { key: "source", render: (_, row) => <span className="table-identity"><strong>{row.robot_id}</strong><small>{row.agent_id}</small></span>, title: "Source", width: 200 },
    { dataIndex: "started_at", key: "started", render: formatSessionTime, title: "Started", width: 150 },
    { dataIndex: "total_size_bytes", key: "size", render: formatBytes, title: "Size", width: 110 },
    { key: "state", render: (_, row) => <Space size={4} direction="vertical"><Tag color={stateColor(row.state)}>{row.state}</Tag><Tag color={stateColor(row.quality?.state)}>Q {row.quality?.state || "UNKNOWN"}</Tag></Space>, title: "Storage / Quality", width: 150 },
  ];

  const derivationColumns: TableColumnsType<DatasetDerivation> = [
    { dataIndex: "derivation_id", key: "id", render: (value: string, row) => <span className="table-identity"><strong>{value}</strong><small>{row.session_id}</small></span>, title: "Dataset / Source", width: 300 },
    { key: "segment", render: (_, row) => <span className="table-identity"><strong>{formatNumber(row.requested_start_offset_ms / 1000, 1)}–{formatNumber(row.requested_end_offset_ms / 1000, 1)} s</strong><small>{formatNumber((Number(BigInt(String(row.target_end_ns)) - BigInt(String(row.target_start_ns))) / 1e9), 1)} s · {row.fps} Hz · {formatNumber(row.frame_count)} frames</small></span>, title: "Segment", width: 230 },
    { key: "features", render: (_, row) => <span className="table-identity"><strong>state + {row.camera_topic_keys.length} cameras</strong><small>observation only · no action</small></span>, title: "Features", width: 190 },
    { key: "output", render: (_, row) => row.state === "COMPLETE" ? <Space direction="vertical" size={2}><Tag color="success">COMPLETE</Tag><Text>{formatBytes(row.output_size_bytes)}</Text><Button href={row.download_url} icon={<DownloadOutlined />} size="small" type="link">Download tar</Button></Space> : <Space direction="vertical" size={2}><Tag color={stateColor(row.state)}>{row.state}</Tag><Text type="secondary">{row.error || "Waiting for output"}</Text></Space>, title: "Output", width: 150 },
  ];

  return (
    <AppShell activeKey="catalog" connection={connection} contextTitle="Data catalog" scopeDetail="Immutable MCAP and derived observations" scopeTitle="Observation storage">
      <div className="page-stack catalog-page">
        <PageHeader actions={<Button icon={<ReloadOutlined spin={loading} />} loading={loading} onClick={() => void load()}>Refresh</Button>} description="Find selected robot and camera recordings, inspect integrity, and replay synchronized views." eyebrow="ADMIN / IMMUTABLE OBSERVATIONS" title="Data catalog" />

        <Card className="summary-card" styles={{ body: { padding: 0 } }}>
          <Row>
            <CatalogStat title="Sessions" value={sessions.length} />
            <CatalogStat title="Complete" value={completeCount} />
            <CatalogStat title="Staging" value={stagingCount} />
            <CatalogStat title="Declared size" value={formatBytes(totalSize)} />
            <CatalogStat title="LeRobot derived" value={derivedCount} />
            <Col className="summary-cell" xs={12} lg={4}><Statistic prefix={<ClockCircleOutlined />} suffix={<Text type="secondary">{clock?.system_offset_ms == null ? "" : `${formatNumber(clock.system_offset_ms, 3)} ms`}</Text>} title="Central clock" value={(clock?.state || "unknown").toUpperCase()} /></Col>
          </Row>
        </Card>

        <Card className="catalog-filter-card">
          <Flex align="end" gap={12} wrap>
            <FilterSelect label="Task Run" onChange={(runId) => setFilters((current) => ({ ...current, runId }))} options={(configuration?.task_runs || []).map((run) => ({ label: `${run.display_name} · ${run.run_id}`, value: run.run_id }))} placeholder="All runs" value={filters.runId} />
            <FilterSelect label="Agent" onChange={(agentId) => setFilters((current) => ({ ...current, agentId }))} options={(configuration?.agent_registrations || []).map((agent) => ({ label: `${agent.display_name} · ${agent.agent_id}`, value: agent.agent_id }))} placeholder="All agents" value={filters.agentId} />
            <FilterSelect label="State" onChange={(state) => setFilters((current) => ({ ...current, state }))} options={[{ label: "COMPLETE", value: "COMPLETE" }, { label: "STAGING", value: "STAGING" }]} placeholder="All states" value={filters.state} />
            <Button icon={<FileSearchOutlined />} loading={loading} onClick={() => void load(filters)} type="primary">Apply filters</Button>
          </Flex>
        </Card>

        <div className="catalog-workspace-ant">
          <Card className="catalog-session-card" extra={<Text type="secondary">{sessions.length} results</Text>} title="Recording sessions">
            <Table columns={sessionColumns} dataSource={sessions} locale={{ emptyText: "No centrally stored sessions match these filters" }} onRow={(session) => ({ onClick: () => void selectSession(session.session_id) })} pagination={{ hideOnSinglePage: true, pageSize: 12 }} rowClassName={(session) => session.session_id === selectedId ? "selected-catalog-row" : ""} rowKey="session_id" scroll={{ x: 900 }} size="small" />
          </Card>
          <SessionInspector detail={detail} onPlayback={() => detail && setPlaybackSessionId(detail.session_id)} quality={quality} />
        </div>

        {playbackSessionId ? <PlaybackPanel onClose={() => setPlaybackSessionId(null)} sessionId={playbackSessionId} /> : null}

        <Card extra={<Tag>{derivations.length} artifacts</Tag>} title={<Space><DatabaseOutlined />LeRobot datasets</Space>}>
          <Alert description="Source MCAP files remain unchanged. Current derivatives contain observation features only and no action feature." message="Reproducible training derivatives" showIcon type="info" />
          <Table columns={derivationColumns} dataSource={derivations} locale={{ emptyText: "No derived LeRobot datasets have been created" }} pagination={{ hideOnSinglePage: true, pageSize: 8 }} rowKey="derivation_id" scroll={{ x: 900 }} />
        </Card>
      </div>
    </AppShell>
  );
}

function CatalogStat({ title, value }: { title: string; value: number | string }) {
  return <Col className="summary-cell" xs={12} lg={4}><Statistic prefix={<DatabaseOutlined />} title={title} value={value} /></Col>;
}

function FilterSelect({ label, onChange, options, placeholder, value }: {
  label: string;
  onChange: (value: string) => void;
  options: Array<{ label: string; value: string }>;
  placeholder: string;
  value: string;
}) {
  return <label className="filter-field"><Text type="secondary">{label}</Text><Select allowClear onChange={(next) => onChange(next || "")} options={options} placeholder={placeholder} showSearch value={value || undefined} /></label>;
}

function SessionInspector({ detail, onPlayback, quality }: { detail: RecordingSessionDetail | null; onPlayback: () => void; quality: QualityDetail | null }) {
  if (!detail) return <Card className="catalog-inspector"><Empty description="Select a session to inspect topics, objects, and SHA-256" image={Empty.PRESENTED_IMAGE_SIMPLE} /></Card>;
  const cameras = detail.topics.filter((topic) => topic.type === "sensor_msgs/msg/CompressedImage" && topic.message_count > 0);
  return (
    <Card className="catalog-inspector" extra={<Tag color={stateColor(detail.state)}>{detail.state}</Tag>} title="Session provenance">
      <Title className="session-detail-title" level={4}>{detail.session_id}</Title>
      <Descriptions column={1} items={[
        { key: "source", label: "Agent / Robot", children: <Text code>{detail.agent_id} / {detail.robot_id}</Text> },
        { key: "run", label: "Task Run", children: <Text code>{detail.run_id || "unscoped"}</Text> },
        { key: "profile", label: "Capture profile", children: <Text code>{detail.profile_id}</Text> },
        { key: "time", label: "Source time", children: <Text code>{String(detail.source_time_range.start_ns)} → {String(detail.source_time_range.end_ns)}</Text> },
        { key: "hash", label: "Manifest SHA-256", children: <Text className="break-code" code>{detail.manifest_sha256}</Text> },
      ]} size="small" />
      <Divider />
      <QualityPanel quality={quality} />
      <Divider titlePlacement="start">Topics · {detail.topics.length}</Divider>
      <List dataSource={detail.topics} renderItem={(topic) => <List.Item><List.Item.Meta description={`${topic.asset_id} · ${formatNumber(topic.message_count)} messages`} title={topic.key} /><Text className="list-topic" code>{topic.name}</Text></List.Item>} size="small" />
      <Divider titlePlacement="start">Objects · {detail.artifacts.length}</Divider>
      <List dataSource={detail.artifacts} renderItem={(artifact) => <List.Item actions={artifact.download_url ? [<Button href={artifact.download_url} icon={<DownloadOutlined />} key="download" size="small" type="link">Download</Button>] : []}><List.Item.Meta description={<><Text>{formatBytes(artifact.size_bytes)} · {artifact.state}</Text><Text className="artifact-hash" code>{artifact.sha256}</Text></>} title={artifact.path} /></List.Item>} size="small" />
      {detail.state === "COMPLETE" && cameras.length ? <Button block icon={<PlayCircleOutlined />} onClick={onPlayback} type="primary">Open recording · {cameras.length} cameras</Button> : null}
    </Card>
  );
}

function QualityPanel({ quality }: { quality: QualityDetail | null }) {
  if (!quality?.available) return <Alert message="No quality report is available yet" showIcon type="info" />;
  return (
    <div className="quality-panel-ant">
      <Flex align="center" justify="space-between"><strong>Observation quality</strong><Tag color={stateColor(quality.state)}>{quality.state}</Tag></Flex>
      <Text type="secondary">Clock {quality.clock_state} · {quality.summary.failed_check_count} failed · {quality.summary.warning_check_count} warnings</Text>
      <List dataSource={quality.streams} renderItem={(stream) => <List.Item extra={<Tag color={stateColor(stream.state)}>{stream.state}</Tag>}><List.Item.Meta description={`${formatNumber(stream.measured.rate_hz, 2)} / ≥${formatNumber(stream.configured.minimum_rate_hz, 1)} Hz · gap p95 ${formatNumber(stream.measured.p95_gap_ms, 1)} ms`} title={stream.key} /></List.Item>} size="small" />
      <Button href={quality.report_url} size="small" type="link">Immutable JSON report</Button>
    </div>
  );
}

function PlaybackPanel({ onClose, sessionId }: { onClose: () => void; sessionId: string }) {
  const { message } = AntApp.useApp();
  const [info, setInfo] = useState<PlaybackInfo | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [offsetMs, setOffsetMs] = useState(0);
  const [scrubOffsetMs, setScrubOffsetMs] = useState<number | null>(null);
  const [seekRevision, setSeekRevision] = useState(0);
  const [speed, setSpeed] = useState(1);
  const [playing, setPlaying] = useState(false);
  const [status, setStatus] = useState("Checking MCAP index");
  const [frames, setFrames] = useState<Record<string, PlaybackFrame>>({});
  const framesRef = useRef<Record<string, PlaybackFrame>>({});
  const panelRef = useRef<HTMLDivElement>(null);
  const generationRef = useRef(0);
  const frameEpochRef = useRef(0);
  const pendingFrameRequestRef = useRef<PlaybackFrameRequest | null>(null);
  const frameWorkerRunningRef = useRef(false);
  const activeFrameControllersRef = useRef<AbortController[]>([]);
  const scrubbingRef = useRef(false);

  function invalidateFrameRequests() {
    frameEpochRef.current += 1;
    pendingFrameRequestRef.current = null;
    activeFrameControllersRef.current.forEach((controller) => controller.abort());
    activeFrameControllersRef.current = [];
  }

  async function fetchPlaybackFrame(request: PlaybackFrameRequest, cameraKey: string, controller: AbortController): Promise<LoadedPlaybackFrame> {
    const parameters = new URLSearchParams({ offset_ms: String(request.bucketMs), topic_key: cameraKey });
    const url = `/api/v1/recording-sessions/${encodeURIComponent(request.sessionId)}/playback/frame?${parameters}`;
    let response: Response | null = null;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      response = await fetch(url, {
        cache: "no-store", headers: { Accept: "image/jpeg" }, signal: controller.signal,
      });
      if (response.status !== 429 || attempt === 1) break;
      await response.body?.cancel();
      await waitForRetry(150, controller.signal);
    }
    if (!response?.ok) {
      const payload = await response?.json().catch(() => ({})) as { error?: string } | undefined;
      throw new Error(payload?.error || `${cameraKey}: HTTP ${response?.status || "unknown"}`);
    }
    const frameOffset = Number(response.headers.get("X-SANG-Frame-Offset-Ms"));
    const sourceOffset = Number(response.headers.get("X-SANG-Source-Offset-Ms"));
    const skew = Number(response.headers.get("X-SANG-Skew-Ms"));
    return {
      cameraKey,
      frame: {
        offsetMs: Number.isFinite(frameOffset) ? frameOffset : null,
        skewMs: Number.isFinite(skew) ? skew : null,
        sourceDeltaMs: Number.isFinite(sourceOffset) ? sourceOffset - request.bucketMs : null,
        url: URL.createObjectURL(await response.blob()),
      },
    };
  }

  async function drainFrameRequests() {
    if (frameWorkerRunningRef.current) return;
    frameWorkerRunningRef.current = true;
    try {
      while (pendingFrameRequestRef.current) {
        const request = pendingFrameRequestRef.current;
        pendingFrameRequestRef.current = null;
        if (request.generation !== generationRef.current || request.epoch !== frameEpochRef.current) continue;

        const controllers = request.selected.map(() => new AbortController());
        activeFrameControllersRef.current = controllers;
        setStatus(`Synchronizing ${request.selected.length} cameras`);
        const results = await Promise.allSettled(request.selected.map((cameraKey, index) => (
          fetchPlaybackFrame(request, cameraKey, controllers[index])
        )));
        activeFrameControllersRef.current = [];

        const loaded = results.flatMap((result) => result.status === "fulfilled" ? [result.value] : []);
        const currentRequest = request.generation === generationRef.current && request.epoch === frameEpochRef.current;
        if (!currentRequest) {
          loaded.forEach(({ frame }) => URL.revokeObjectURL(frame.url));
          continue;
        }
        const failures = results.filter((result) => result.status === "rejected" && (result.reason as { name?: string })?.name !== "AbortError");
        if (failures.length) {
          loaded.forEach(({ frame }) => URL.revokeObjectURL(frame.url));
          setStatus(`${failures.length} frames could not be read`);
          continue;
        }

        setFrames((current) => {
          const next = { ...current };
          loaded.forEach(({ cameraKey, frame }) => {
            if (next[cameraKey]?.url) URL.revokeObjectURL(next[cameraKey].url);
            next[cameraKey] = frame;
          });
          return next;
        });
        setStatus(pendingFrameRequestRef.current ? "Catching up to playback" : `${request.selected.length} cameras synchronized`);
      }
    } finally {
      activeFrameControllersRef.current = [];
      frameWorkerRunningRef.current = false;
      if (pendingFrameRequestRef.current) void drainFrameRequests();
    }
  }

  useEffect(() => {
    let disposed = false;
    invalidateFrameRequests();
    generationRef.current += 1;
    const generation = generationRef.current;
    setPlaying(false);
    setInfo(null);
    setSelected([]);
    setOffsetMs(0);
    setScrubOffsetMs(null);
    scrubbingRef.current = false;
    setStatus("Checking MCAP index");
    setFrames((current) => {
      Object.values(current).forEach((frame) => URL.revokeObjectURL(frame.url));
      return {};
    });
    void getJson<PlaybackInfo>(`/api/v1/recording-sessions/${encodeURIComponent(sessionId)}/playback`).then((next) => {
      if (disposed || generation !== generationRef.current) return;
      setInfo(next);
      setSelected(next.cameras.slice(0, next.max_camera_views).map((camera) => camera.key));
      setStatus("MCAP index ready");
      window.setTimeout(() => panelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
    }).catch((error) => {
      if (!disposed && generation === generationRef.current) setStatus(errorMessage(error));
    });
    return () => { disposed = true; generationRef.current += 1; invalidateFrameRequests(); };
  }, [sessionId]);

  const bucketMs = info ? Math.min(info.duration_ms, Math.round(offsetMs / info.frame_step_ms) * info.frame_step_ms) : 0;
  const selectedSignature = selected.join("|");

  useEffect(() => {
    if (!info) return;
    if (!selected.length) {
      pendingFrameRequestRef.current = null;
      setStatus("Select one or more camera views");
      return;
    }
    pendingFrameRequestRef.current = {
      bucketMs,
      epoch: frameEpochRef.current,
      generation: generationRef.current,
      selected: [...selected],
      sessionId,
    };
    void drainFrameRequests();
  }, [bucketMs, info, seekRevision, selectedSignature, sessionId]);

  useEffect(() => {
    if (!playing || !info) return;
    const durationMs = info.duration_ms;
    let animationFrame = 0;
    let previous = performance.now();
    function tick(now: number) {
      const elapsed = now - previous;
      previous = now;
      setOffsetMs((current) => {
        const next = Math.min(durationMs, current + elapsed * speed);
        if (next >= durationMs) window.setTimeout(() => setPlaying(false), 0);
        return next;
      });
      animationFrame = requestAnimationFrame(tick);
    }
    animationFrame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(animationFrame);
  }, [info, playing, speed]);

  useEffect(() => { framesRef.current = frames; }, [frames]);
  useEffect(() => () => {
    Object.values(framesRef.current).forEach((frame) => URL.revokeObjectURL(frame.url));
  }, []);

  function selectCameras(values: Array<string | number>) {
    if (!info) return;
    const next = values.map(String);
    if (next.length > info.max_camera_views) {
      void message.warning(`You can view up to ${info.max_camera_views} cameras at once.`);
      return;
    }
    setPlaying(false);
    invalidateFrameRequests();
    setSelected(next);
    setFrames((current) => {
      Object.entries(current).forEach(([key, frame]) => { if (!next.includes(key)) URL.revokeObjectURL(frame.url); });
      return Object.fromEntries(Object.entries(current).filter(([key]) => next.includes(key)));
    });
  }

  function beginScrub(value: number) {
    setPlaying(false);
    if (!scrubbingRef.current) {
      scrubbingRef.current = true;
      invalidateFrameRequests();
    }
    setScrubOffsetMs(value);
    setStatus("Release timeline to seek");
  }

  function finishScrub(value: number) {
    invalidateFrameRequests();
    scrubbingRef.current = false;
    setScrubOffsetMs(null);
    setOffsetMs(value);
    setSeekRevision((current) => current + 1);
  }

  function restartPlayback() {
    setPlaying(false);
    finishScrub(0);
  }

  const selectedCameras = info?.cameras.filter((camera) => selected.includes(camera.key)) || [];
  const displayedOffsetMs = scrubOffsetMs ?? offsetMs;
  return (
    <div ref={panelRef}>
      <Card className="playback-card-ant" extra={<Button aria-label="Close playback" icon={<CloseOutlined />} onClick={onClose} type="text" />} title={<Space><PlayCircleOutlined />Recorded camera playback</Space>}>
        <Flex className="playback-heading-ant" justify="space-between" wrap>
          <div><Text className="page-kicker">MCAP / SYNCHRONIZED REVIEW</Text><Title level={4}>{sessionId}</Title><Text type="secondary">Original JPEG frames are requested individually and never re-encoded.</Text></div>
          <div className="playback-clock-ant"><Text type="secondary">RECORD TIME</Text><strong>{formatDuration(displayedOffsetMs)} / {formatDuration(info?.duration_ms || 0)}</strong><small>{status}</small></div>
        </Flex>
        <Divider />
        <Checkbox.Group className="playback-camera-picker-ant" onChange={selectCameras} options={(info?.cameras || []).map((camera) => ({ label: `${camera.key} · ${formatNumber(camera.message_count)} frames`, value: camera.key }))} value={selected} />
        <Flex align="center" className="playback-controls-ant" gap={12}>
          <Button disabled={!info} icon={<StepBackwardOutlined />} onClick={restartPlayback}>Restart</Button>
          <Button disabled={!info || !selected.length} icon={playing ? <PauseCircleOutlined /> : <PlayCircleOutlined />} onClick={() => setPlaying((value) => !value)} type="primary">{playing ? "Pause" : "Play"}</Button>
          <Slider max={info?.duration_ms || 0} min={0} onChange={beginScrub} onChangeComplete={finishScrub} step={info?.frame_step_ms || 100} tooltip={{ formatter: (value) => formatDuration(value || 0) }} value={displayedOffsetMs} />
          <Select onChange={setSpeed} options={[0.5, 1, 2].map((value) => ({ label: `${value}×`, value }))} value={speed} />
        </Flex>
        <div className="playback-grid-ant">
          {selectedCameras.length ? selectedCameras.map((camera) => <PlaybackView camera={camera} frame={frames[camera.key]} key={camera.key} />) : <Empty description="Select one or more camera views" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
        </div>
      </Card>
    </div>
  );
}

function PlaybackView({ camera, frame }: { camera: PlaybackCamera; frame?: PlaybackFrame }) {
  const stale = frame?.skewMs == null || Math.abs(frame.skewMs) > 500;
  return (
    <article className="playback-view-ant">
      <header><div><strong>{camera.key}</strong><small>{camera.asset_id}</small></div><Tag color={stale ? "warning" : "success"}>{frame?.skewMs == null ? "Waiting" : `${frame.skewMs >= 0 ? "+" : ""}${frame.skewMs} ms`}</Tag></header>
      <div className="playback-frame-ant">{frame ? <img alt={`${camera.key} recorded frame`} src={frame.url} /> : <Empty description="Waiting for frame" image={Empty.PRESENTED_IMAGE_SIMPLE} />}</div>
      <footer><span>{frame?.offsetMs == null ? "--:--.-" : formatDuration(frame.offsetMs)}</span><span>{frame?.sourceDeltaMs == null ? `${formatNumber(camera.message_count)} frames` : `source ${frame.sourceDeltaMs >= 0 ? "+" : ""}${formatNumber(frame.sourceDeltaMs)} ms`}</span></footer>
    </article>
  );
}
