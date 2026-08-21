"use strict";

const state = {
  runs: [],
  runId: null,
  payload: null,
  socket: null,
  reconnectTimer: null,
  selectedCamera: null,
  preview: { active: false, controller: null, frameUrl: null, status: "idle" },
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

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
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

function applyPayload(payload) {
  if (payload.task_run?.run_id !== state.runId) return;
  state.payload = payload;
  renderSummary();
  renderAssets();
  renderTelemetry();
  renderCameras();
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
  state.runId = runId;
  state.payload = null;
  state.selectedCamera = null;
  if (!runId) {
    renderSummary(); renderAssets(); renderTelemetry(); renderCameras();
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
byId("preview-toggle").addEventListener("click", togglePreview);
window.addEventListener("hashchange", () => {
  const runId = runFromHash() || "";
  if (runId !== state.runId) void connectRun(runId);
});
window.addEventListener("beforeunload", () => { disconnectSocket(); stopPreview(); });

void loadRuns();
