"use strict";

const initialQuery = new URLSearchParams(location.search);
const state = {
  sessions: [],
  derivations: [],
  selectedId: null,
  configuration: null,
  requestedRunId: initialQuery.get("run_id"),
  requestedSessionId: initialQuery.get("session_id"),
  timeStatus: null,
  playback: {
    sessionId: null,
    info: null,
    selected: new Set(),
    offsetMs: 0,
    speed: 1,
    playing: false,
    animationFrame: null,
    lastAnimationAt: null,
    lastRequestedBucket: null,
    loading: false,
    queued: false,
    generation: 0,
    controllers: new Map(),
    frameUrls: new Map(),
  },
};
const byId = (id) => document.getElementById(id);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
}

function formatTime(value) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("ko-KR", {
    year: "2-digit", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date(value));
}

function formatDuration(value) {
  const totalTenths = Math.max(0, Math.round(Number(value || 0) / 100));
  const minutes = Math.floor(totalTenths / 600);
  const seconds = Math.floor((totalTenths % 600) / 10);
  const tenths = totalTenths % 10;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${tenths}`;
}

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (response.status === 401) {
    location.assign(`/login.html?next=${encodeURIComponent(location.href)}`);
    throw new Error("authentication required");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${path}: HTTP ${response.status}`);
  return payload;
}

function setConnection(live, label) {
  byId("connection-dot").className = `connection-dot ${live ? "is-live" : "is-error"}`;
  byId("connection-label").textContent = label;
}

function populateFilters() {
  const runs = byId("run-filter");
  const agents = byId("agent-filter");
  const selectedRun = runs.value;
  const selectedAgent = agents.value;
  runs.replaceChildren(new Option("전체 Run", ""));
  agents.replaceChildren(new Option("전체 Agent", ""));
  for (const run of state.configuration?.task_runs || []) {
    runs.append(new Option(`${run.display_name} · ${run.run_id}`, run.run_id));
  }
  for (const agent of state.configuration?.agent_registrations || []) {
    agents.append(new Option(`${agent.display_name} · ${agent.agent_id}`, agent.agent_id));
  }
  if ([...runs.options].some((item) => item.value === selectedRun)) runs.value = selectedRun;
  if ([...agents.options].some((item) => item.value === selectedAgent)) agents.value = selectedAgent;
}

function renderSummary() {
  byId("session-count").textContent = state.sessions.length;
  byId("complete-count").textContent = state.sessions.filter((item) => item.state === "COMPLETE").length;
  byId("staging-count").textContent = state.sessions.filter((item) => item.state === "STAGING").length;
  byId("total-size").textContent = formatBytes(
    state.sessions.reduce((total, item) => total + Number(item.total_size_bytes || 0), 0),
  );
  byId("derivation-count").textContent = state.derivations.filter(
    (item) => item.state === "COMPLETE",
  ).length;
  byId("result-count").textContent = `${state.sessions.length} results`;
  const clock = state.timeStatus?.central;
  byId("clock-state").textContent = String(clock?.state || "unknown").toUpperCase();
  byId("clock-offset").textContent = Number.isFinite(Number(clock?.system_offset_ms))
    ? `${Number(clock.system_offset_ms).toFixed(3)} ms offset`
    : (clock?.error_code || "no measurement");
}

function renderDerivations() {
  const list = byId("derivation-list");
  list.replaceChildren();
  byId("derivation-result-count").textContent = `${state.derivations.length} artifacts`;
  if (!state.derivations.length) {
    list.append(element("p", "catalog-empty", "아직 생성된 LeRobot 파생 데이터셋이 없습니다."));
    return;
  }
  for (const derivation of state.derivations) {
    const row = element("article", "derivation-row");
    const identity = element("div", "derivation-identity");
    identity.append(
      element("strong", "", derivation.derivation_id),
      element("small", "", derivation.session_id),
      element(
        "span",
        `derivation-state is-${derivation.state.toLowerCase()}`,
        derivation.state,
      ),
    );
    const durationNs = BigInt(String(derivation.target_end_ns))
      - BigInt(String(derivation.target_start_ns));
    const duration = Math.max(0, Number(durationNs) / 1e9);
    const segment = element("div", "derivation-segment");
    segment.append(
      element("strong", "", `${(derivation.requested_start_offset_ms / 1000).toFixed(1)}–${(derivation.requested_end_offset_ms / 1000).toFixed(1)} s`),
      element("small", "", `${duration.toFixed(1)} s · ${derivation.fps} Hz · ${derivation.frame_count || 0} frames`),
    );
    const features = element("div", "derivation-features");
    features.append(
      element("strong", "", `state + ${derivation.camera_topic_keys.length} cameras`),
      element("small", "", "observation only · no action"),
    );
    const output = element("div", "derivation-output");
    if (derivation.state === "COMPLETE") {
      output.append(
        element("strong", "", formatBytes(derivation.output_size_bytes)),
        element("code", "", derivation.output_sha256),
      );
      const download = element("a", "object-link", "LeRobot tar 받기");
      download.href = derivation.download_url;
      output.append(download);
    } else {
      output.append(
        element("strong", "", derivation.state === "RUNNING" ? "변환 중" : "변환 실패"),
        element("small", "", derivation.error || "출력 대기 중"),
      );
    }
    row.append(identity, segment, features, output);
    list.append(row);
  }
}

function sessionRow(session) {
  const button = element("button", `session-row ${session.session_id === state.selectedId ? "is-selected" : ""}`);
  button.type = "button";
  const identity = element("span", "session-identity");
  identity.append(element("strong", "", session.session_id));
  identity.append(element("small", "", session.run_id || "unscoped capture"));
  const source = element("span", "session-source");
  source.append(element("strong", "", session.robot_id));
  source.append(element("small", "", session.agent_id));
  const status = element("span", "session-status-stack");
  status.append(
    element("span", `session-state is-${session.state.toLowerCase()}`, session.state),
    element(
      "span",
      `quality-state is-${String(session.quality?.state || "unknown").toLowerCase()}`,
      `Q ${session.quality?.state || "UNKNOWN"}`,
    ),
  );
  button.append(
    identity,
    source,
    element("span", "session-time", formatTime(session.started_at)),
    element("span", "session-size", formatBytes(session.total_size_bytes)),
    status,
  );
  button.addEventListener("click", () => selectSession(session.session_id));
  return button;
}

function renderSessions() {
  const list = byId("session-list");
  list.replaceChildren();
  if (!state.sessions.length) {
    list.append(element("p", "catalog-empty", "조건에 맞는 중앙 저장 세션이 없습니다."));
    return;
  }
  for (const session of state.sessions) list.append(sessionRow(session));
}

function detailLine(label, value, mono = false) {
  const row = element("div", "detail-line");
  row.append(element("span", "", label), element("strong", mono ? "is-mono" : "", value || "--"));
  return row;
}

function qualitySummary(quality) {
  const card = element("section", "quality-card");
  const header = element("header");
  header.append(
    element("span", "eyebrow", "OBSERVATION QUALITY"),
    element("strong", `quality-state is-${String(quality?.state || "unknown").toLowerCase()}`, quality?.state || "UNKNOWN"),
  );
  card.append(header);
  if (!quality?.available) {
    card.append(element("p", "", "아직 품질 보고서가 없습니다. 새 업로드는 완료 직후 자동 분석됩니다."));
    return card;
  }
  const summary = quality.summary || {};
  card.append(element(
    "p",
    "",
    `Clock ${quality.clock_state} · FAIL checks ${summary.failed_check_count || 0} · WARNING checks ${summary.warning_check_count || 0}`,
  ));
  const streams = element("div", "quality-streams");
  for (const stream of quality.streams || []) {
    const configured = stream.configured || {};
    const measured = stream.measured || {};
    const expectedSize = configured.width && configured.height ? `${configured.width}×${configured.height}` : "--";
    const measuredSize = measured.width && measured.height ? `${measured.width}×${measured.height}` : "--";
    const row = element("div", "quality-stream");
    row.append(
      element("strong", "", stream.key),
      element(
        "span",
        "",
        `${Number(measured.rate_hz || 0).toFixed(2)} Hz / ≥${configured.minimum_rate_hz ?? "--"} · ${measuredSize} / ${expectedSize} · gap p95 ${Number(measured.p95_gap_ms || 0).toFixed(1)} ms`,
      ),
      element("span", `quality-state is-${stream.state.toLowerCase()}`, stream.state),
    );
    streams.append(row);
  }
  card.append(streams);
  const report = element("a", "quality-report-link", "변경 불가 JSON 보고서");
  report.href = quality.report_url;
  card.append(report);
  return card;
}

function renderDetail(session, quality) {
  const detail = byId("session-detail");
  const content = element("div", "detail-content");
  content.append(element("p", "eyebrow", "SESSION / PROVENANCE"));
  content.append(element("h2", "", session.session_id));
  content.append(element("span", `detail-state is-${session.state.toLowerCase()}`, session.state));
  const facts = element("div", "detail-facts");
  facts.append(
    detailLine("Agent / Robot", `${session.agent_id} / ${session.robot_id}`, true),
    detailLine("Task Run", session.run_id || "unscoped", true),
    detailLine("Capture profile", session.profile_id, true),
    detailLine("Source time", `${session.source_time_range.start_ns || "--"} → ${session.source_time_range.end_ns || "--"}`, true),
    detailLine("Manifest SHA-256", session.manifest_sha256, true),
  );
  content.append(facts);
  content.append(qualitySummary(quality));

  content.append(element("h3", "detail-subhead", `Topics · ${session.topics.length}`));
  const topics = element("div", "detail-rows");
  for (const topic of session.topics) {
    const row = element("article", "detail-row");
    row.append(element("strong", "", topic.key));
    row.append(element("span", "", topic.name));
    row.append(element("small", "", `${topic.asset_id} · ${topic.message_count} messages`));
    topics.append(row);
  }
  content.append(topics);

  content.append(element("h3", "detail-subhead", `Objects · ${session.artifacts.length}`));
  const artifacts = element("div", "detail-rows");
  for (const artifact of session.artifacts) {
    const row = element("article", "detail-row artifact-row");
    row.append(element("strong", "", artifact.path));
    row.append(element("span", "", `${formatBytes(artifact.size_bytes)} · ${artifact.state}`));
    row.append(element("small", "hash", artifact.sha256));
    if (artifact.download_url) {
      const link = element("a", "object-link", "객체 받기");
      link.href = artifact.download_url;
      row.append(link);
    }
    artifacts.append(row);
  }
  content.append(artifacts);
  const cameraCount = session.topics.filter(
    (topic) => topic.type === "sensor_msgs/msg/CompressedImage" && Number(topic.message_count) > 0,
  ).length;
  if (session.state === "COMPLETE" && cameraCount) {
    const open = element("button", "playback-open", `녹화본 보기 · 카메라 ${cameraCount}개`);
    open.type = "button";
    open.addEventListener("click", () => openPlayback(session.session_id));
    content.append(open);
  }
  detail.replaceChildren(content);
}

function renderPlaceholder() {
  const placeholder = element("div", "catalog-placeholder");
  placeholder.append(
    element("span", "", "01"),
    element("strong", "", "세션을 선택하세요."),
    element("p", "", "토픽, 자산, 시간 범위와 객체 SHA-256을 확인할 수 있습니다."),
  );
  byId("session-detail").replaceChildren(placeholder);
}

async function selectSession(sessionId) {
  if (state.playback.sessionId && state.playback.sessionId !== sessionId) resetPlayback();
  state.selectedId = sessionId;
  renderSessions();
  try {
    const encoded = encodeURIComponent(sessionId);
    const [session, quality] = await Promise.all([
      getJson(`/api/v1/recording-sessions/${encoded}`),
      getJson(`/api/v1/recording-sessions/${encoded}/quality`),
    ]);
    renderDetail(session, quality);
  } catch (error) {
    setConnection(false, error.message);
  }
}

function setPlaybackStatus(message, error = false) {
  const status = byId("playback-status");
  status.textContent = message;
  status.classList.toggle("is-error", error);
}

function stopPlaybackAnimation() {
  if (state.playback.animationFrame !== null) cancelAnimationFrame(state.playback.animationFrame);
  state.playback.animationFrame = null;
  state.playback.lastAnimationAt = null;
  state.playback.playing = false;
}

function releasePlaybackFrames() {
  for (const controller of state.playback.controllers.values()) controller.abort();
  state.playback.controllers.clear();
  for (const url of state.playback.frameUrls.values()) URL.revokeObjectURL(url);
  state.playback.frameUrls.clear();
}

function resetPlayback(hide = true) {
  stopPlaybackAnimation();
  releasePlaybackFrames();
  state.playback.generation += 1;
  state.playback.sessionId = null;
  state.playback.info = null;
  state.playback.selected = new Set();
  state.playback.offsetMs = 0;
  state.playback.lastRequestedBucket = null;
  state.playback.loading = false;
  state.playback.queued = false;
  byId("playback-panel").hidden = hide;
  byId("playback-camera-picker").replaceChildren();
  byId("playback-grid").replaceChildren(playbackEmpty());
  updatePlaybackControls();
}

function playbackEmpty() {
  const empty = element("div", "playback-empty");
  empty.append(
    element("strong", "", "카메라를 선택하세요."),
    element("span", "", "선택한 뷰만 MCAP에서 요청합니다."),
  );
  return empty;
}

function updatePlaybackControls() {
  const info = state.playback.info;
  const duration = Number(info?.duration_ms || 0);
  const toggle = byId("playback-toggle");
  toggle.textContent = state.playback.playing ? "일시정지" : "재생";
  toggle.setAttribute("aria-pressed", String(state.playback.playing));
  toggle.disabled = !info || !state.playback.selected.size;
  byId("playback-rewind").disabled = !info;
  const timeline = byId("playback-timeline");
  timeline.max = String(duration);
  timeline.step = String(info?.frame_step_ms || 100);
  timeline.value = String(Math.min(duration, Math.round(state.playback.offsetMs)));
  byId("playback-clock").textContent = `${formatDuration(state.playback.offsetMs)} / ${formatDuration(duration)}`;
}

function renderCameraPicker() {
  const picker = byId("playback-camera-picker");
  picker.replaceChildren();
  const maximum = state.playback.info.max_camera_views;
  for (const camera of state.playback.info.cameras) {
    const label = element("label", "playback-camera-option");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = camera.key;
    input.checked = state.playback.selected.has(camera.key);
    label.classList.toggle("is-selected", input.checked);
    label.append(input, document.createTextNode(`${camera.key} · ${camera.message_count}`));
    input.addEventListener("change", () => {
      if (input.checked && state.playback.selected.size >= maximum) {
        input.checked = false;
        setPlaybackStatus(`동시 카메라는 최대 ${maximum}개입니다.`, true);
        return;
      }
      if (input.checked) state.playback.selected.add(camera.key);
      else state.playback.selected.delete(camera.key);
      label.classList.toggle("is-selected", input.checked);
      stopPlaybackAnimation();
      state.playback.lastRequestedBucket = null;
      renderPlaybackViews();
      updatePlaybackControls();
      void refreshFrames(true);
    });
    picker.append(label);
  }
}

function selectedCameras() {
  return state.playback.info.cameras.filter((camera) => state.playback.selected.has(camera.key));
}

function renderPlaybackViews() {
  releasePlaybackFrames();
  const grid = byId("playback-grid");
  grid.replaceChildren();
  const cameras = selectedCameras();
  if (!cameras.length) {
    grid.append(playbackEmpty());
    return;
  }
  for (const camera of cameras) {
    const view = element("article", "playback-view");
    view.dataset.cameraKey = camera.key;
    const header = element("header");
    header.append(element("strong", "", camera.key), element("span", "", camera.asset_id));
    const frame = element("div", "playback-frame");
    const image = document.createElement("img");
    image.alt = `${camera.key} 녹화 프레임`;
    frame.append(image, element("span", "", "프레임 대기"));
    const footer = element("footer");
    footer.append(
      element("span", "frame-time", "--:--.-"),
      element("span", "frame-skew", `${camera.message_count} frames`),
    );
    view.append(header, frame, footer);
    grid.append(view);
  }
}

function cameraView(cameraKey) {
  return byId("playback-grid").querySelector(`[data-camera-key="${cameraKey}"]`);
}

async function loadPlaybackFrame(camera, bucketMs, generation) {
  const previousController = state.playback.controllers.get(camera.key);
  if (previousController) previousController.abort();
  const controller = new AbortController();
  state.playback.controllers.set(camera.key, controller);
  const parameters = new URLSearchParams({
    topic_key: camera.key,
    offset_ms: String(bucketMs),
  });
  const response = await fetch(
    `/api/v1/recording-sessions/${encodeURIComponent(state.playback.sessionId)}/playback/frame?${parameters}`,
    { headers: { Accept: "image/jpeg" }, signal: controller.signal, cache: "no-store" },
  );
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || `${camera.key}: HTTP ${response.status}`);
  }
  const blob = await response.blob();
  if (generation !== state.playback.generation || !state.playback.selected.has(camera.key)) return;
  const view = cameraView(camera.key);
  if (!view) return;
  const image = view.querySelector("img");
  const nextUrl = URL.createObjectURL(blob);
  const previousUrl = state.playback.frameUrls.get(camera.key);
  image.addEventListener("load", () => {
    if (previousUrl) URL.revokeObjectURL(previousUrl);
  }, { once: true });
  image.src = nextUrl;
  state.playback.frameUrls.set(camera.key, nextUrl);
  const frameOffset = Number(response.headers.get("X-SANG-Frame-Offset-Ms"));
  const sourceOffset = Number(response.headers.get("X-SANG-Source-Offset-Ms"));
  const skew = Number(response.headers.get("X-SANG-Skew-Ms"));
  view.querySelector(".frame-time").textContent = formatDuration(frameOffset);
  const skewNode = view.querySelector(".frame-skew");
  const sourceDelta = Number.isFinite(sourceOffset) ? sourceOffset - bucketMs : null;
  skewNode.textContent = Number.isFinite(skew)
    ? `${skew >= 0 ? "+" : ""}${skew} ms${sourceDelta === null ? "" : ` · source ${sourceDelta >= 0 ? "+" : ""}${sourceDelta} ms`}`
    : "시간 정보 없음";
  skewNode.classList.toggle("is-stale", !Number.isFinite(skew) || Math.abs(skew) > 500);
}

async function refreshFrames(force = false) {
  const info = state.playback.info;
  const cameras = info ? selectedCameras() : [];
  if (!info || !cameras.length) return;
  const step = info.frame_step_ms;
  const bucket = Math.min(info.duration_ms, Math.round(state.playback.offsetMs / step) * step);
  if (!force && bucket === state.playback.lastRequestedBucket) return;
  if (state.playback.loading) {
    state.playback.queued = true;
    return;
  }
  state.playback.loading = true;
  state.playback.queued = false;
  state.playback.lastRequestedBucket = bucket;
  const generation = state.playback.generation;
  setPlaybackStatus(`${cameras.length}개 카메라 동기화 중`);
  const results = await Promise.allSettled(
    cameras.map((camera) => loadPlaybackFrame(camera, bucket, generation)),
  );
  if (generation !== state.playback.generation) return;
  const failures = results.filter(
    (result) => result.status === "rejected" && result.reason?.name !== "AbortError",
  );
  state.playback.loading = false;
  setPlaybackStatus(
    failures.length ? `${failures.length}개 프레임을 읽지 못했습니다.` : `${cameras.length}개 카메라 동기화됨`,
    Boolean(failures.length),
  );
  if (state.playback.queued || Math.round(state.playback.offsetMs / step) * step !== bucket) {
    state.playback.queued = false;
    void refreshFrames(true);
  }
}

function playbackTick(timestamp) {
  if (!state.playback.playing || !state.playback.info) return;
  if (state.playback.lastAnimationAt === null) state.playback.lastAnimationAt = timestamp;
  const elapsed = timestamp - state.playback.lastAnimationAt;
  state.playback.lastAnimationAt = timestamp;
  state.playback.offsetMs = Math.min(
    state.playback.info.duration_ms,
    state.playback.offsetMs + elapsed * state.playback.speed,
  );
  updatePlaybackControls();
  void refreshFrames();
  if (state.playback.offsetMs >= state.playback.info.duration_ms) {
    stopPlaybackAnimation();
    updatePlaybackControls();
    return;
  }
  state.playback.animationFrame = requestAnimationFrame(playbackTick);
}

function setPlaybackPlaying(playing) {
  stopPlaybackAnimation();
  if (playing && state.playback.info && state.playback.selected.size) {
    if (state.playback.offsetMs >= state.playback.info.duration_ms) state.playback.offsetMs = 0;
    state.playback.playing = true;
    state.playback.animationFrame = requestAnimationFrame(playbackTick);
  }
  updatePlaybackControls();
}

async function openPlayback(sessionId) {
  resetPlayback(false);
  state.playback.sessionId = sessionId;
  state.playback.generation += 1;
  const generation = state.playback.generation;
  byId("playback-panel").hidden = false;
  byId("playback-session").textContent = sessionId;
  setPlaybackStatus("MCAP 인덱스 확인 중");
  try {
    const info = await getJson(`/api/v1/recording-sessions/${encodeURIComponent(sessionId)}/playback`);
    if (generation !== state.playback.generation) return;
    state.playback.info = info;
    state.playback.selected = new Set(
      info.cameras.slice(0, info.max_camera_views).map((camera) => camera.key),
    );
    state.playback.offsetMs = 0;
    renderCameraPicker();
    renderPlaybackViews();
    updatePlaybackControls();
    await refreshFrames(true);
    byId("playback-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setPlaybackStatus(error.message, true);
  }
}

function filterUrl() {
  const parameters = new URLSearchParams({ limit: "500" });
  for (const [id, key] of [["run-filter", "run_id"], ["agent-filter", "agent_id"], ["state-filter", "state"]]) {
    const value = byId(id).value || (key === "run_id" ? state.requestedRunId : "");
    if (value) parameters.set(key, value);
  }
  return `/api/v1/recording-sessions?${parameters}`;
}

function derivationUrl() {
  const parameters = new URLSearchParams({ limit: "500" });
  const runId = byId("run-filter").value || state.requestedRunId;
  if (runId) parameters.set("run_id", runId);
  return `/api/v1/dataset-derivations?${parameters}`;
}

async function load() {
  setConnection(true, "동기화 중");
  try {
    const [configuration, sessions, derivations, timeStatus] = await Promise.all([
      getJson("/api/v1/configuration"),
      getJson(filterUrl()),
      getJson(derivationUrl()),
      getJson("/api/v1/time-status"),
    ]);
    state.configuration = configuration;
    state.sessions = sessions;
    state.derivations = derivations;
    state.timeStatus = timeStatus;
    if (state.selectedId && !sessions.some((item) => item.session_id === state.selectedId)) {
      state.selectedId = null;
      renderPlaceholder();
    }
    populateFilters();
    if (
      state.requestedRunId
      && [...byId("run-filter").options].some((item) => item.value === state.requestedRunId)
    ) {
      byId("run-filter").value = state.requestedRunId;
    }
    renderSummary();
    renderSessions();
    renderDerivations();
    if (
      state.requestedSessionId
      && sessions.some((item) => item.session_id === state.requestedSessionId)
    ) {
      await selectSession(state.requestedSessionId);
    }
    state.requestedRunId = null;
    state.requestedSessionId = null;
    setConnection(true, "Catalog live");
  } catch (error) {
    setConnection(false, error.message);
  }
}

byId("reload-button").addEventListener("click", load);
byId("apply-filter").addEventListener("click", load);
byId("playback-toggle").addEventListener("click", () => {
  setPlaybackPlaying(!state.playback.playing);
});
byId("playback-rewind").addEventListener("click", () => {
  setPlaybackPlaying(false);
  state.playback.offsetMs = 0;
  state.playback.lastRequestedBucket = null;
  updatePlaybackControls();
  void refreshFrames(true);
});
byId("playback-timeline").addEventListener("input", (event) => {
  const offsetMs = Number(event.currentTarget.value);
  setPlaybackPlaying(false);
  state.playback.offsetMs = offsetMs;
  updatePlaybackControls();
  void refreshFrames();
});
byId("playback-speed").addEventListener("change", (event) => {
  state.playback.speed = Number(event.target.value);
});
window.addEventListener("beforeunload", () => resetPlayback());
updatePlaybackControls();
load();
