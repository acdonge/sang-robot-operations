"use strict";

const state = {
  runs: [],
  runId: null,
  payload: null,
  socket: null,
  reconnectTimer: null,
  selectedCamera: null,
  preview: { active: false, controller: null, frameUrl: null, status: "idle" },
  capture: {
    workerId: null,
    busy: false,
    integrity: null,
    integritySessionId: null,
    integrityLoading: false,
    integrityFetchedAt: 0,
    pendingRequest: null,
    actionError: null,
    completionError: null,
  },
};

const byId = (id) => document.getElementById(id);

function element(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = String(text);
  return result;
}

function runFromHash() {
  const match = location.hash.match(/^#run\/(.+)$/);
  if (!match) return null;
  try { return decodeURIComponent(match[1]); } catch (_error) { return null; }
}

function formatClock(value) {
  if (!value) return "--:--:--";
  return new Intl.DateTimeFormat("ko-KR", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date(value));
}

function formatDateTime(value) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("ko-KR", {
    year: "2-digit", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date(value));
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
}

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (response.status === 401) {
    location.assign(`/login.html?next=${encodeURIComponent(location.href)}`);
    throw new Error("authentication required");
  }
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

async function requestJson(method, path, body = {}) {
  const csrf = document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith("sang_csrf="))
    ?.split("=", 2)[1];
  const response = await fetch(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...(csrf ? { "X-CSRF-Token": decodeURIComponent(csrf) } : {}),
    },
    body: JSON.stringify(body),
  });
  if (response.status === 401) {
    location.assign(`/login.html?next=${encodeURIComponent(location.href)}`);
    throw new Error("authentication required");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${path}: HTTP ${response.status}`);
  return payload;
}

function setConnection(mode, label) {
  byId("connection-dot").className = `live-dot ${mode === "live" ? "is-live" : mode === "error" ? "is-error" : ""}`;
  byId("connection-label").textContent = label;
}

function assetSeverity(asset) {
  if (["OFFLINE", "UNAVAILABLE"].includes(asset.state)) return "is-critical";
  if (["DEGRADED", "UNINSTRUMENTED"].includes(asset.state)) return "is-warning";
  return "";
}

function renderAssets() {
  const grid = byId("asset-grid");
  grid.replaceChildren();
  const assets = state.payload?.assets || [];
  if (!assets.length) {
    grid.append(element("p", "empty-message", "이 Run에는 배정된 자산이 없습니다."));
    return;
  }
  for (const observation of assets) {
    const card = element("article", `asset-card ${assetSeverity(observation)}`);
    card.append(element("span", "role", observation.role));
    card.append(element("strong", "", observation.asset.display_name));
    card.append(element("span", "asset-type", `${observation.asset.asset_type} · ${observation.asset.asset_id}`));
    const status = element("div", "asset-status");
    status.append(element("b", "", observation.state));
    status.append(element("span", "", observation.access_mode));
    card.append(status);
    grid.append(card);
  }
}

function renderTelemetry() {
  const list = byId("telemetry-list");
  list.replaceChildren();
  const agents = state.payload?.agents || [];
  if (!agents.length) {
    list.append(element("p", "empty-message", "실시간 Agent binding이 없습니다."));
    return;
  }
  for (const agent of agents) {
    for (const robot of agent.robots) {
      const card = element("article", "telemetry-card");
      const head = element("div", "telemetry-head");
      head.append(element("strong", "", robot.robot_id));
      head.append(element("span", "", `${agent.agent_name} · ${agent.status} · ${robot.phase}`));
      card.append(head);
      const sources = element("div", "source-strip");
      for (const source of robot.sources) {
        const health = source.fresh && source.rate_ok !== false ? "is-fresh" : "is-stale";
        const rate = source.topics[0]?.rate_hz;
        sources.append(element(
          "span",
          `source-pill ${health}`,
          `${source.key} · ${rate === null || rate === undefined ? "--" : rate.toFixed(1)} Hz`,
        ));
      }
      card.append(sources);
      list.append(card);
    }
  }
}

function cameraSources() {
  const cameras = [];
  for (const agent of state.payload?.agents || []) {
    for (const robot of agent.robots) {
      for (const source of robot.sources) {
        if (source.kind !== "camera") continue;
        cameras.push({
          id: `${agent.agent_id}/${robot.robot_id}/${source.key}`,
          agent,
          robot,
          source,
        });
      }
    }
  }
  return cameras;
}

function stopPreview() {
  if (state.preview.controller) state.preview.controller.abort();
  if (state.preview.frameUrl) URL.revokeObjectURL(state.preview.frameUrl);
  state.preview = { active: false, controller: null, frameUrl: null, status: "idle" };
  byId("preview-image").hidden = true;
  byId("preview-image").removeAttribute("src");
  byId("preview-placeholder").hidden = false;
  renderPreviewCaption();
}

function selectCamera(camera) {
  if (state.selectedCamera !== camera.id && state.preview.active) stopPreview();
  state.selectedCamera = camera.id;
  renderCameras();
}

function renderPreviewCaption() {
  const camera = cameraSources().find((item) => item.id === state.selectedCamera) || null;
  byId("preview-name").textContent = camera ? camera.source.key : "카메라 미선택";
  byId("preview-topic").textContent = camera
    ? camera.source.topics.map((topic) => topic.topic).join(" · ")
    : "대기 중";
  const button = byId("preview-toggle");
  button.disabled = !camera || !camera.source.preview_url || camera.agent.status === "offline";
  button.textContent = state.preview.active
    ? "미리보기 중지"
    : state.preview.status === "error" ? "다시 시도" : "미리보기 시작";
}

function renderCameras() {
  const list = byId("camera-list");
  list.replaceChildren();
  const cameras = cameraSources();
  if (state.selectedCamera && !cameras.some((item) => item.id === state.selectedCamera)) {
    stopPreview();
    state.selectedCamera = null;
  }
  if (!state.selectedCamera && cameras.length) state.selectedCamera = cameras[0].id;
  if (!cameras.length) list.append(element("p", "empty-message", "배정된 카메라 소스가 없습니다."));
  for (const camera of cameras) {
    const button = element("button", `camera-choice ${camera.id === state.selectedCamera ? "is-selected" : ""}`);
    button.type = "button";
    button.append(element("strong", "", camera.source.key));
    button.append(element(
      "span",
      "",
      `${camera.robot.robot_id} · ${camera.source.fresh ? "FRESH" : "STALE"}`,
    ));
    button.addEventListener("click", () => selectCamera(camera));
    list.append(button);
  }
  renderPreviewCaption();
}

function renderSummary() {
  const payload = state.payload;
  const run = payload?.task_run;
  byId("run-title").textContent = run?.display_name || "Task Run을 선택하세요";
  byId("run-meta").textContent = run
    ? `${run.run_id} · ${run.task_definition_id}@${run.task_version}${run.workcell_id ? ` · ${run.workcell_id}` : ""}`
    : "Admin에서 구성된 실행 단위만 이 화면에 나타납니다.";
  byId("run-state").textContent = run?.state || "IDLE";
  const assets = payload?.assets || [];
  const healthy = assets.filter((item) => item.state === "HEALTHY").length;
  byId("assigned-count").textContent = assets.length;
  byId("healthy-count").textContent = healthy;
  byId("attention-count").textContent = assets.length - healthy;
  byId("camera-count").textContent = cameraSources().length;
  byId("updated-at").textContent = formatClock(payload?.generated_at);
}

const ACTIVE_CAPTURE_STATES = new Set([
  "PENDING", "CLAIMED", "RECORDING", "FINALIZING", "UPLOADING",
]);
const STOPPABLE_CAPTURE_STATES = new Set([
  "PENDING", "CLAIMED", "RECORDING", "FINALIZING",
]);

function captureJobs() {
  return state.payload?.capture_jobs || [];
}

function currentCaptureJob() {
  const jobs = captureJobs();
  return jobs.find((job) => ACTIVE_CAPTURE_STATES.has(job.state)) || jobs[0] || null;
}

function captureWorkers() {
  return state.payload?.capture_workers || [];
}

function selectedCaptureWorker() {
  return captureWorkers().find((worker) => worker.worker_id === state.capture.workerId) || null;
}

function renderCaptureWorkerOptions() {
  const selector = byId("capture-worker-selector");
  const workers = captureWorkers();
  const signature = workers
    .map((worker) => `${worker.worker_id}:${worker.available}:${worker.profile_id}`)
    .join("|");
  if (selector.dataset.signature !== signature) {
    selector.replaceChildren();
    if (!workers.length) {
      const empty = element("option", "", "사용 가능한 작업자 없음");
      empty.value = "";
      selector.append(empty);
    }
    for (const worker of workers) {
      const option = element(
        "option",
        "",
        `${worker.worker_id} · ${worker.available ? "ONLINE" : "STALE"} · ${worker.profile_id}`,
      );
      option.value = worker.worker_id;
      selector.append(option);
    }
    selector.dataset.signature = signature;
  }
  if (!workers.some((worker) => worker.worker_id === state.capture.workerId)) {
    state.capture.workerId = workers.find((worker) => worker.available)?.worker_id
      || workers[0]?.worker_id
      || null;
  }
  selector.value = state.capture.workerId || "";
}

function captureProgress(job) {
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

function captureStateNote(job) {
  if (!job) return "Task Run 녹화 대기 중";
  const labels = {
    PENDING: "Edge 작업자 할당 대기",
    CLAIMED: "Edge 작업자가 세션 준비 중",
    RECORDING: "고정 프로필로 ROS2 관측 기록 중",
    FINALIZING: "MCAP 인덱스와 Manifest 생성 중",
    UPLOADING: "Central Object Store 업로드·검증 중",
    COMPLETE: "중앙 무결성 검증 완료",
    FAILED: "녹화 실패 · 원본 증거 보존",
    CANCELLED: "녹화 시작 전 취소됨",
  };
  return labels[job.state] || job.state;
}

function renderCaptureIntegrity(job) {
  const detail = state.capture.integritySessionId === job?.session_id
    ? state.capture.integrity
    : null;
  const summary = detail || job?.recording || null;
  const artifacts = detail?.artifacts || [];
  const mcapBytes = artifacts
    .filter((artifact) => artifact.path.endsWith(".mcap"))
    .reduce((total, artifact) => total + Number(artifact.size_bytes || 0), 0);
  const expected = Number(summary?.expected_file_count || artifacts.length || 0);
  const completed = Number(
    summary?.completed_file_count
    || artifacts.filter((artifact) => artifact.state === "COMPLETE").length,
  );
  const verified = summary?.state === "COMPLETE"
    && expected > 0
    && completed === expected
    && artifacts.length === expected
    && artifacts.every((artifact) => /^[0-9a-f]{64}$/.test(artifact.sha256));

  byId("capture-integrity-state").textContent = verified
    ? "VERIFIED / SHA-256"
    : summary ? `${summary.state} / ${completed} OF ${expected}` : "AWAITING SESSION";
  byId("capture-integrity-state").className = verified ? "is-verified" : "";
  byId("capture-mcap-size").textContent = mcapBytes
    ? formatBytes(mcapBytes)
    : summary?.total_size_bytes ? formatBytes(summary.total_size_bytes) : "--";
  byId("capture-files").textContent = summary ? `${completed} / ${expected}` : "-- / --";
  const hash = summary?.manifest_sha256 || "--";
  byId("capture-hash").textContent = hash;
  byId("capture-hash").title = hash === "--" ? "" : hash;
  const link = byId("capture-catalog-link");
  if (job) {
    const query = new URLSearchParams({ run_id: job.run_id, session_id: job.session_id });
    link.href = `/catalog.html?${query}`;
    link.removeAttribute("aria-disabled");
  } else {
    link.href = "/catalog.html";
    link.setAttribute("aria-disabled", "true");
  }
}

function captureSession(job) {
  if (!job) return null;
  return (state.payload?.recording_sessions || [])
    .find((session) => session.session_id === job.session_id) || null;
}

function runCompletionGate(run, job) {
  const activeJob = captureJobs().find((item) => ACTIVE_CAPTURE_STATES.has(item.state)) || null;
  const completedJob = !activeJob && job?.state === "COMPLETE" ? job : null;
  const session = captureSession(completedJob);
  const qualityState = session?.quality?.state || "PENDING";
  let message = "최신 완료 세션의 품질 PASS가 필요합니다.";
  if (!run) message = "Task Run을 선택하세요.";
  else if (run.state !== "ACTIVE") message = `${run.state} Run은 더 이상 종료할 수 없습니다.`;
  else if (activeJob) message = `${activeJob.state} Capture Job이 끝날 때까지 기다리세요.`;
  else if (!completedJob) message = "완료된 Capture Job이 필요합니다.";
  else if (session?.state !== "COMPLETE") message = "Central Object Store 검증을 기다리고 있습니다.";
  else if (qualityState !== "PASS") message = `품질 상태 ${qualityState} · PASS 후 종료할 수 있습니다.`;
  else message = "품질 PASS 확인 완료 · Run 종료 시 모든 자산 예약이 해제됩니다.";
  if (state.capture.completionError) message = state.capture.completionError;
  return {
    enabled: run?.state === "ACTIVE"
      && !activeJob
      && completedJob !== null
      && session?.state === "COMPLETE"
      && qualityState === "PASS"
      && !state.capture.busy,
    qualityState,
    message,
  };
}

function renderRunCompletion(run, job) {
  const gate = runCompletionGate(run, job);
  const quality = byId("capture-quality-state");
  quality.textContent = gate.qualityState;
  quality.className = `is-${gate.qualityState.toLowerCase()}`;
  byId("run-complete").disabled = !gate.enabled;
  byId("run-complete-message").textContent = gate.message;
}

function renderCapture() {
  renderCaptureWorkerOptions();
  const run = state.payload?.task_run || null;
  const worker = selectedCaptureWorker();
  const job = currentCaptureJob();
  const activeJob = job && ACTIVE_CAPTURE_STATES.has(job.state) ? job : null;
  const progress = captureProgress(job);
  const duration = byId("capture-duration");
  const start = byId("capture-start");
  const stop = byId("capture-stop");

  byId("capture-state").textContent = job?.state || "IDLE";
  byId("capture-state").className = `capture-state is-${(job?.state || "idle").toLowerCase()}`;
  byId("capture-state-note").textContent = captureStateNote(job);
  byId("capture-progress-value").textContent = `${progress}%`;
  byId("capture-progress-bar").style.width = `${progress}%`;
  byId("capture-progress-bar").className = ["FAILED", "CANCELLED"].includes(job?.state)
    ? "is-error"
    : job?.state === "COMPLETE" ? "is-complete" : "";
  byId("capture-job").textContent = job?.job_id || "--";
  byId("capture-session").textContent = job?.session_id || "--";
  byId("capture-created").textContent = formatDateTime(job?.started_at || job?.created_at);

  const canStart = run?.state === "ACTIVE"
    && worker?.available === true
    && !activeJob
    && !state.capture.busy;
  start.disabled = !canStart;
  stop.disabled = state.capture.busy || !job || !STOPPABLE_CAPTURE_STATES.has(job.state);
  duration.disabled = Boolean(activeJob) || state.capture.busy;
  byId("capture-worker-selector").disabled = Boolean(activeJob) || state.capture.busy;

  let message = "ACTIVE Task Run과 온라인 Edge 작업자가 필요합니다.";
  if (state.capture.actionError) message = state.capture.actionError;
  else if (state.capture.busy) message = "중앙에서 녹화 요청을 처리하고 있습니다.";
  else if (!run) message = "Task Run을 선택하세요.";
  else if (run.state !== "ACTIVE") message = "녹화는 ACTIVE Task Run에서만 시작할 수 있습니다.";
  else if (!worker) message = "이 Task Run의 자산을 모두 포함한 Edge 작업자가 없습니다.";
  else if (!worker.available) message = "Edge 작업자의 heartbeat가 오래되어 시작할 수 없습니다.";
  else if (activeJob) message = `${job.state} · 동일 작업자에는 녹화 세션 하나만 허용됩니다.`;
  else if (job?.state === "COMPLETE") message = "이전 세션 검증 완료 · 새 녹화를 시작할 수 있습니다.";
  else if (job?.error) message = job.error;
  else message = "읽기 전용 프로필로 로봇 관절과 카메라 토픽을 함께 기록합니다.";
  byId("capture-message").textContent = message;
  renderCaptureIntegrity(job);
  renderRunCompletion(run, job);
}

function captureRequestStorageKey(runId, workerId) {
  return `sang.capture.request.${runId}.${workerId}`;
}

function newRequestId() {
  if (crypto.randomUUID) return `ui-${crypto.randomUUID()}`;
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return `ui-${[...bytes].map((value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function pendingCaptureRequest(workerId, durationS) {
  const key = captureRequestStorageKey(state.runId, workerId);
  let stored = state.capture.pendingRequest;
  try { stored = JSON.parse(sessionStorage.getItem(key)) || stored; } catch (_error) { /* ignored */ }
  if (stored && captureJobs().some((job) => job.request_id === stored.requestId)) {
    try { sessionStorage.removeItem(key); } catch (_error) { /* ignored */ }
    stored = null;
    state.capture.pendingRequest = null;
  }
  if (stored?.workerId === workerId && stored?.durationS === durationS) return stored;
  stored = { workerId, durationS, requestId: newRequestId() };
  state.capture.pendingRequest = stored;
  try { sessionStorage.setItem(key, JSON.stringify(stored)); } catch (_error) { /* ignored */ }
  return stored;
}

function reconcileCaptureRequest() {
  if (!state.runId) return;
  for (const worker of captureWorkers()) {
    const key = captureRequestStorageKey(state.runId, worker.worker_id);
    let pending = state.capture.pendingRequest?.workerId === worker.worker_id
      ? state.capture.pendingRequest
      : null;
    try { pending = JSON.parse(sessionStorage.getItem(key)) || pending; } catch (_error) { /* ignored */ }
    if (!pending || !captureJobs().some((job) => job.request_id === pending.requestId)) continue;
    try { sessionStorage.removeItem(key); } catch (_error) { /* ignored */ }
    if (state.capture.pendingRequest?.requestId === pending.requestId) {
      state.capture.pendingRequest = null;
    }
    state.capture.actionError = null;
  }
}

async function refreshCaptureIntegrity() {
  const job = currentCaptureJob();
  if (!job?.recording) return;
  const now = Date.now();
  const changedSession = state.capture.integritySessionId !== job.session_id;
  if (state.capture.integrityLoading) return;
  if (!changedSession && now - state.capture.integrityFetchedAt < 3000) return;
  state.capture.integrityLoading = true;
  if (changedSession) {
    state.capture.integrity = null;
    state.capture.integritySessionId = job.session_id;
  }
  try {
    state.capture.integrity = await getJson(
      `/api/v1/recording-sessions/${encodeURIComponent(job.session_id)}`,
    );
    state.capture.integrityFetchedAt = Date.now();
    renderCapture();
  } catch (_error) {
    state.capture.integrityFetchedAt = Date.now();
  } finally {
    state.capture.integrityLoading = false;
  }
}

async function startCapture(event) {
  event.preventDefault();
  if (state.capture.busy || !state.runId) return;
  const worker = selectedCaptureWorker();
  const durationS = Number.parseInt(byId("capture-duration").value, 10);
  if (!worker || !Number.isInteger(durationS) || durationS < 5 || durationS > 28800) return;
  const pending = pendingCaptureRequest(worker.worker_id, durationS);
  state.capture.busy = true;
  state.capture.actionError = null;
  renderCapture();
  try {
    await requestJson(
      "POST",
      `/api/v1/task-runs/${encodeURIComponent(state.runId)}/capture-jobs`,
      {
        request_id: pending.requestId,
        worker_id: worker.worker_id,
        duration_s: durationS,
      },
    );
    applyPayload(await getJson(`/api/v1/task-runs/${encodeURIComponent(state.runId)}/fleet`));
  } catch (error) {
    state.capture.actionError = error.message;
  } finally {
    state.capture.busy = false;
    renderCapture();
  }
}

async function stopCapture() {
  const job = currentCaptureJob();
  if (state.capture.busy || !job || !STOPPABLE_CAPTURE_STATES.has(job.state)) return;
  state.capture.busy = true;
  state.capture.actionError = null;
  renderCapture();
  try {
    await requestJson("POST", `/api/v1/capture-jobs/${encodeURIComponent(job.job_id)}/stop`);
    applyPayload(await getJson(`/api/v1/task-runs/${encodeURIComponent(state.runId)}/fleet`));
  } catch (error) {
    state.capture.actionError = error.message;
  } finally {
    state.capture.busy = false;
    renderCapture();
  }
}

async function completeRunAfterCapture() {
  const run = state.payload?.task_run || null;
  const job = currentCaptureJob();
  if (!runCompletionGate(run, job).enabled) return;
  if (!window.confirm(`${run.display_name}을 완료하고 배정 자산을 해제할까요?`)) return;
  state.capture.busy = true;
  state.capture.completionError = null;
  renderCapture();
  try {
    const completed = await requestJson(
      "POST",
      `/api/v1/task-runs/${encodeURIComponent(run.run_id)}/complete-after-capture`,
      { expected_revision: run.revision, state: "COMPLETE" },
    );
    state.runs = state.runs.map((item) => item.run_id === completed.run_id ? completed : item);
    const option = [...byId("run-selector").options]
      .find((item) => item.value === completed.run_id);
    if (option) option.textContent = `${completed.display_name} · ${completed.state}`;
    applyPayload(await getJson(`/api/v1/task-runs/${encodeURIComponent(completed.run_id)}/fleet`));
  } catch (error) {
    state.capture.completionError = error.message;
  } finally {
    state.capture.busy = false;
    renderCapture();
  }
}

function applyPayload(payload) {
  if (payload.task_run?.run_id !== state.runId) return;
  state.payload = payload;
  renderSummary();
  renderAssets();
  renderTelemetry();
  renderCameras();
  reconcileCaptureRequest();
  renderCapture();
  void refreshCaptureIntegrity();
  setConnection("live", "Run live");
}

function socketUrl(runId) {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${location.host}/api/v1/task-runs/${encodeURIComponent(runId)}/stream`;
}

function disconnectSocket() {
  clearTimeout(state.reconnectTimer);
  if (state.socket) {
    state.socket.onclose = null;
    state.socket.close();
  }
  state.socket = null;
}

async function connectRun(runId) {
  disconnectSocket();
  stopPreview();
  if (state.runId !== runId) {
    state.capture.workerId = null;
    state.capture.integrity = null;
    state.capture.integritySessionId = null;
    state.capture.integrityFetchedAt = 0;
    state.capture.pendingRequest = null;
    state.capture.actionError = null;
    state.capture.completionError = null;
  }
  state.runId = runId;
  state.payload = null;
  state.selectedCamera = null;
  if (!runId) {
    renderSummary(); renderAssets(); renderTelemetry(); renderCameras();
    renderCapture();
    setConnection("idle", "Run not selected");
    return;
  }
  byId("run-selector").value = runId;
  setConnection("idle", "Run 연결 중");
  try {
    applyPayload(await getJson(`/api/v1/task-runs/${encodeURIComponent(runId)}/fleet`));
  } catch (error) {
    setConnection("error", error.message);
    return;
  }
  const socket = new WebSocket(socketUrl(runId));
  state.socket = socket;
  socket.addEventListener("message", (event) => {
    try { applyPayload(JSON.parse(event.data)); } catch (_error) { setConnection("error", "Invalid live payload"); }
  });
  socket.addEventListener("close", () => {
    if (state.socket !== socket) return;
    state.socket = null;
    setConnection("error", "Live stream reconnecting");
    state.reconnectTimer = setTimeout(() => connectRun(runId), 1600);
  });
  socket.addEventListener("error", () => socket.close());
}

function appendBytes(left, right) {
  const combined = new Uint8Array(left.length + right.length);
  combined.set(left); combined.set(right, left.length);
  return combined;
}

function findMarker(bytes, first, second, offset = 0) {
  for (let index = offset; index < bytes.length - 1; index += 1) {
    if (bytes[index] === first && bytes[index + 1] === second) return index;
  }
  return -1;
}

function showFrame(jpeg) {
  if (!state.preview.active) return;
  const nextUrl = URL.createObjectURL(new Blob([jpeg], { type: "image/jpeg" }));
  const previousUrl = state.preview.frameUrl;
  state.preview.frameUrl = nextUrl;
  state.preview.status = "live";
  const image = byId("preview-image");
  image.src = nextUrl;
  image.hidden = false;
  byId("preview-placeholder").hidden = true;
  if (previousUrl) URL.revokeObjectURL(previousUrl);
}

async function streamPreview(camera, controller) {
  let buffer = new Uint8Array(0);
  try {
    const separator = camera.source.preview_url.includes("?") ? "&" : "?";
    const response = await fetch(`${camera.source.preview_url}${separator}session=${Date.now()}`, {
      cache: "no-store",
      headers: { Accept: "multipart/x-mixed-replace" },
      signal: controller.signal,
    });
    if (!response.ok || !response.body) throw new Error(`preview HTTP ${response.status}`);
    const reader = response.body.getReader();
    while (state.preview.active && !controller.signal.aborted) {
      const { done, value } = await reader.read();
      if (done) throw new Error("preview stream ended");
      buffer = appendBytes(buffer, value);
      while (buffer.length) {
        const start = findMarker(buffer, 0xff, 0xd8);
        if (start < 0) { buffer = buffer.slice(-1); break; }
        const end = findMarker(buffer, 0xff, 0xd9, start + 2);
        if (end < 0) { if (start > 0) buffer = buffer.slice(start); break; }
        showFrame(buffer.slice(start, end + 2));
        buffer = buffer.slice(end + 2);
      }
    }
    await reader.cancel();
  } catch (_error) {
    if (controller.signal.aborted) return;
    state.preview.active = false;
    state.preview.controller = null;
    state.preview.status = "error";
    renderPreviewCaption();
  }
}

function togglePreview() {
  if (state.preview.active) { stopPreview(); return; }
  const camera = cameraSources().find((item) => item.id === state.selectedCamera);
  if (!camera?.source.preview_url) return;
  const controller = new AbortController();
  state.preview = { active: true, controller, frameUrl: null, status: "loading" };
  renderPreviewCaption();
  void streamPreview(camera, controller);
}

async function loadRuns() {
  try {
    const configuration = await getJson("/api/v1/configuration");
    state.runs = configuration.task_runs;
    const selector = byId("run-selector");
    selector.replaceChildren();
    const empty = element("option", "", "Run을 선택하세요");
    empty.value = "";
    selector.append(empty);
    for (const run of state.runs) {
      const item = element("option", "", `${run.display_name} · ${run.state}`);
      item.value = run.run_id;
      selector.append(item);
    }
    const requested = runFromHash();
    if (requested && state.runs.some((run) => run.run_id === requested)) {
      selector.value = requested;
      await connectRun(requested);
    } else {
      await connectRun("");
    }
  } catch (error) {
    setConnection("error", error.message);
  }
}

byId("run-selector").addEventListener("change", (event) => {
  const runId = event.target.value;
  const nextHash = runId ? `#run/${encodeURIComponent(runId)}` : "";
  if (location.hash === nextHash) void connectRun(runId);
  else location.hash = nextHash;
});
byId("capture-form").addEventListener("submit", startCapture);
byId("capture-stop").addEventListener("click", stopCapture);
byId("run-complete").addEventListener("click", completeRunAfterCapture);
byId("capture-worker-selector").addEventListener("change", (event) => {
  state.capture.workerId = event.target.value || null;
  state.capture.actionError = null;
  renderCapture();
});
byId("capture-duration").addEventListener("input", () => {
  state.capture.actionError = null;
});
byId("preview-toggle").addEventListener("click", togglePreview);
window.addEventListener("hashchange", () => {
  const runId = runFromHash() || "";
  if (runId !== state.runId) void connectRun(runId);
});
window.addEventListener("beforeunload", () => { disconnectSocket(); stopPreview(); });

void loadRuns();
