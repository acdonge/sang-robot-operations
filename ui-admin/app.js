"use strict";

const API = Object.freeze({
  agents: "/api/v1/agents",
  summary: "/api/v1/summary",
  events: "/api/v1/events?limit=100",
  fleetSocket: "/api/v1/fleet",
});

const state = {
  agents: [],
  summary: null,
  events: [],
  selectedAgentId: null,
  filter: "all",
  socket: null,
  detailCameraKeys: new Map(),
  preview: {
    identity: null,
    sourceKey: null,
    status: "idle",
    active: false,
    controller: null,
    frameUrl: null,
  },
};

const byId = (id) => document.getElementById(id);

function node(tag, className, value) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (value !== undefined && value !== null) element.textContent = String(value);
  return element;
}

function setText(id, value) {
  byId(id).textContent = value;
}

function renderKey(item) {
  return item.nodeType === Node.ELEMENT_NODE ? item.dataset.renderKey || null : null;
}

function sameNodeKind(current, next) {
  if (current.nodeType !== next.nodeType) return false;
  if (current.nodeType !== Node.ELEMENT_NODE) return true;
  return current.tagName === next.tagName && renderKey(current) === renderKey(next);
}

function syncAttributes(current, next) {
  for (const attribute of [...current.attributes]) {
    if (!next.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
  }
  for (const attribute of [...next.attributes]) {
    if (current.getAttribute(attribute.name) !== attribute.value) {
      current.setAttribute(attribute.name, attribute.value);
    }
  }
}

function patchNode(current, next) {
  if (!sameNodeKind(current, next)) {
    current.replaceWith(next);
    return next;
  }
  if (current.nodeType === Node.TEXT_NODE) {
    if (current.data !== next.data) current.data = next.data;
    return current;
  }
  syncAttributes(current, next);
  reconcileChildren(current, [...next.childNodes]);
  return current;
}

function reconcileChildren(parent, nextChildren) {
  const previousChildren = [...parent.childNodes];
  const keyedChildren = new Map();
  const unkeyedChildren = [];
  for (const child of previousChildren) {
    const key = renderKey(child);
    if (key) keyedChildren.set(key, child);
    else unkeyedChildren.push(child);
  }

  const retained = new Set();
  let cursor = parent.firstChild;
  for (const next of nextChildren) {
    const key = renderKey(next);
    const candidate = key ? keyedChildren.get(key) : unkeyedChildren.shift();
    if (key) keyedChildren.delete(key);
    const rendered = candidate && sameNodeKind(candidate, next)
      ? patchNode(candidate, next)
      : next;
    if (rendered !== cursor) parent.insertBefore(rendered, cursor);
    retained.add(rendered);
    cursor = rendered.nextSibling;
  }

  for (const child of previousChildren) {
    if (!retained.has(child) && child.parentNode === parent) child.remove();
  }
}

function formatNumber(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return new Intl.NumberFormat("ko-KR", { maximumFractionDigits: digits }).format(value);
}

function formatBytes(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  const bytes = Number(value);
  if (bytes < 1024) return `${formatNumber(bytes)} B`;
  if (bytes < 1024 ** 2) return `${formatNumber(bytes / 1024, 1)} KiB`;
  if (bytes < 1024 ** 3) return `${formatNumber(bytes / 1024 ** 2, 1)} MiB`;
  return `${formatNumber(bytes / 1024 ** 3, 2)} GiB`;
}

function formatAge(seconds) {
  if (seconds < 1) return "now";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  return `${Math.round(seconds / 60)}m`;
}

function formatLatency(milliseconds) {
  if (milliseconds === null || milliseconds === undefined) return "NO DATA";
  if (milliseconds < 1000) return `${formatNumber(milliseconds)} ms`;
  return `${formatNumber(milliseconds / 1000, 1)} s`;
}

function formatRate(rateHz, minimumRateHz = null) {
  if (rateHz === null || rateHz === undefined) return "-- Hz";
  const actual = formatNumber(rateHz, 1);
  if (minimumRateHz === null || minimumRateHz === undefined) return `${actual} Hz`;
  return `${actual} / ${formatNumber(minimumRateHz, 1)} Hz`;
}

function formatClock(value) {
  if (!value) return "--:--:--";
  return new Intl.DateTimeFormat("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatSessionTime(value) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function statusLabel(status) {
  return { online: "ONLINE", stale: "STALE", offline: "OFFLINE" }[status] || "UNKNOWN";
}

function recordingInventory(agent) {
  return agent?.recordings || {
    enabled: false,
    state: "disabled",
    error_code: null,
    complete_count: 0,
    failed_count: 0,
    partial_count: 0,
    invalid_count: 0,
    scanned_count: 0,
    total_size_bytes: 0,
    truncated: false,
    recent: [],
  };
}

function detailHash(agentId, robotId) {
  return `#robot/${encodeURIComponent(agentId)}/${encodeURIComponent(robotId)}`;
}

function currentRoute() {
  const parts = location.hash.replace(/^#/, "").split("/");
  if (parts[0] !== "robot") return { view: "fleet" };
  try {
    return {
      view: "robot",
      agentId: parts[1] ? decodeURIComponent(parts[1]) : null,
      robotId: parts[2] ? decodeURIComponent(parts[2]) : null,
    };
  } catch (_error) {
    return { view: "robot", agentId: null, robotId: null };
  }
}

function observedRobots() {
  return state.agents.flatMap((agent) => agent.robots.map((robot) => ({ agent, robot })));
}

function resolveRouteRobot(route = currentRoute()) {
  if (route.view !== "robot" || !route.agentId || !route.robotId) return null;
  const agent = state.agents.find((item) => item.agent_id === route.agentId);
  const robot = agent?.robots.find((item) => item.robot_id === route.robotId);
  return agent && robot ? { agent, robot } : null;
}

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

function applyFleet(agents, summary, events = state.events) {
  state.agents = Array.isArray(agents) ? agents : [];
  state.summary = summary;
  state.events = Array.isArray(events) ? events : [];
  if (!state.selectedAgentId || !state.agents.some((item) => item.agent_id === state.selectedAgentId)) {
    state.selectedAgentId = state.agents[0]?.agent_id || null;
  }
  renderSummary();
  renderAgents();
  renderInspector();
  renderRobotSelector();
  if (currentRoute().view === "robot") renderRobotDetailView();
}

function renderSummary() {
  const summary = state.summary;
  if (!summary) return;
  setText("agents-online", summary.agents.online);
  setText("agents-total", `/ ${summary.agents.total}`);
  setText("robots-total", summary.robots.total);
  setText("robots-recording", summary.robots.recording);
  setText("needs-attention", summary.agents.needs_attention);
  setText(
    "disk-free",
    summary.storage.disk_free_gb_min === null
      ? "--"
      : `${formatNumber(summary.storage.disk_free_gb_min, 1)} GB`,
  );
  const recordings = summary.recordings || {};
  setText("recordings-total", formatNumber(recordings.total || 0));
  setText(
    "recordings-issues",
    formatNumber((recordings.failed || 0) + (recordings.invalid || 0)),
  );
  setText("generated-at", formatClock(summary.generated_at));
}

function agentNeedsAttention(agent) {
  return agent.status !== "online" || agent.robots.some(
    (robot) => robot.fault || !robot.sources_fresh || robot.rates_ok === false,
  );
}

function filteredAgents() {
  const items = state.agents.filter((agent) => {
    if (state.filter === "online") return agent.status === "online";
    if (state.filter === "attention") return agentNeedsAttention(agent);
    return true;
  });
  const priority = { online: 0, stale: 1, offline: 2 };
  return items.sort((left, right) => (
    priority[left.status] - priority[right.status]
    || left.agent_name.localeCompare(right.agent_name)
  ));
}

function primaryRobot(agent) {
  return agent.robots[0] || null;
}

function agentRow(agent, index) {
  const robot = primaryRobot(agent);
  const recordings = recordingInventory(agent);
  const row = node("div", "agent-row");
  row.setAttribute("role", "row");
  row.dataset.agentId = agent.agent_id;
  row.dataset.renderKey = `agent:${agent.agent_id}`;
  row.style.setProperty("--row-index", index);
  if (agent.agent_id === state.selectedAgentId) row.classList.add("is-selected");

  const rowSelect = node("button", "agent-row-select");
  rowSelect.type = "button";
  rowSelect.setAttribute("aria-label", `${agent.agent_name} Agent 상세 보기`);

  const identity = node("span", "agent-identity");
  identity.append(node("span", `status-line ${agent.status}`));
  const names = node("span");
  names.append(
    node("strong", "", agent.agent_name),
    node("span", "", `${agent.agent_id} / ${robot?.robot_id || "no robot"}`),
  );
  identity.append(names);

  const activity = node("span", "activity");
  const recording = robot?.recording?.active;
  activity.append(
    node("strong", recording ? "is-recording" : "", recording ? "CAPTURING" : robot?.fsm_state || "NO ROBOT"),
    node(
      "span",
      "",
      recording
        ? `EP ${robot.recording.episode_index + 1}/${robot.recording.episodes_total || "-"} · ${formatNumber(robot.recording.frames)} frames`
        : robot?.model || "--",
    ),
  );

  const sourcesGood = agent.robots.length > 0 && agent.robots.every(
    (item) => item.sources_fresh && item.rates_ok !== false,
  );
  const sourceState = node(
    robot ? "a" : "span",
    `source-state source-link ${sourcesGood ? "is-good" : "is-bad"}`,
  );
  sourceState.append(
    node("strong", "", sourcesGood ? "FRESH" : "CHECK"),
    node("span", "source-link-label", robot ? "상세 →" : "NO ROBOT"),
  );
  if (robot) {
    sourceState.href = detailHash(agent.agent_id, robot.robot_id);
    sourceState.setAttribute("aria-label", `${robot.robot_id} 로봇 상세 열기`);
  }

  const storage = node("span", "cell-subtle");
  storage.append(
    node("strong", "", agent.storage.disk_free_gb == null ? "--" : `${formatNumber(agent.storage.disk_free_gb)} GB`),
    node(
      "span",
      "",
      recordings.enabled
        ? `${formatNumber(recordings.complete_count)} MCAP · ${formatNumber(recordings.failed_count + recordings.invalid_count)} issues`
        : "recording inventory off",
    ),
  );

  const age = node("span", "age-state", formatAge(agent.age_s));
  row.append(rowSelect, identity, activity, sourceState, storage, age);
  rowSelect.addEventListener("click", () => {
    state.selectedAgentId = agent.agent_id;
    renderAgents();
    renderInspector();
  });
  return row;
}

function renderAgents() {
  const list = byId("agent-list");
  const agents = filteredAgents();
  if (!agents.some((agent) => agent.agent_id === state.selectedAgentId)) {
    state.selectedAgentId = agents[0]?.agent_id || null;
  }
  reconcileChildren(list, agents.map(agentRow));
  byId("empty-state").hidden = agents.length !== 0;
}

function detailPair(label, value) {
  const row = node("div", "detail-pair");
  row.append(node("span", "", label), node("strong", "", value));
  return row;
}

function sourceDetail(source) {
  const rateLow = source.rate_ok === false;
  const healthLabel = !source.fresh ? "STALE" : rateLow ? "LOW RATE" : "FRESH";
  const healthClass = !source.fresh ? "is-stale" : rateLow ? "is-rate-low" : "is-fresh";
  const wrapper = node("div", `source-detail ${healthClass}`);
  wrapper.dataset.renderKey = `source:${source.key}`;
  const head = node("div", "source-head");
  const identity = node("div", "source-identity");
  identity.append(
    node("span", `source-kind kind-${source.kind}`, source.kind),
    node("strong", "", source.key),
  );
  head.append(
    identity,
    node("span", "source-health", healthLabel),
  );

  const meta = node("div", "source-meta");
  meta.append(
    node("span", "", source.required ? "REQUIRED" : "OPTIONAL"),
    node("span", "", `TTL ${formatNumber(source.freshness_ms)} ms`),
  );

  const topics = node("div", "topic-list");
  for (const topic of source.topics || []) {
    const rowState = !topic.fresh
      ? "is-stale"
      : topic.rate_ok === false
        ? "is-rate-low"
        : "is-fresh";
    const row = node("div", `topic-row ${rowState}`);
    row.dataset.renderKey = `topic:${topic.topic}`;
    row.append(
      node("span", "topic-state"),
      node("code", "topic-name", topic.topic),
      node(
        "span",
        "topic-rate",
        formatRate(topic.rate_hz, topic.minimum_rate_hz),
      ),
      node("span", "topic-age", formatLatency(topic.age_ms)),
      node("span", "topic-seq", `#${formatNumber(topic.seq)}`),
    );
    topics.append(row);
  }
  wrapper.append(head, meta, topics);
  return wrapper;
}

function robotDetail(robot, agentId) {
  const wrapper = node("div", "robot-detail");
  wrapper.dataset.renderKey = `robot:${robot.robot_id}`;
  const title = node("div", "robot-title");
  title.append(node("strong", "", robot.robot_id), node("span", "", robot.model));
  const detailLink = node("a", "inline-detail-link", "로봇 상세 →");
  detailLink.href = detailHash(agentId, robot.robot_id);
  wrapper.append(title, detailLink);

  const progress = robot.recording;
  if (progress.active) {
    const total = progress.episodes_total || 0;
    const percent = total ? Math.min(100, ((progress.episode_index + 1) / total) * 100) : 0;
    const track = node("div", "progress-track");
    const fill = node("div", "progress-fill");
    fill.style.setProperty("--progress", `${percent}%`);
    track.append(fill);
    wrapper.append(
      track,
      detailPair("Episode", `${progress.episode_index + 1} / ${total || "--"}`),
      detailPair("Frames", formatNumber(progress.frames)),
      detailPair("Elapsed", `${formatNumber(progress.elapsed_s, 1)} s`),
    );
    if (progress.current_instruction) {
      wrapper.append(detailPair("Instruction", progress.current_instruction));
    }
  } else {
    wrapper.append(
      node("div", "progress-track"),
      detailPair("State", robot.fsm_state),
      detailPair("Sources", robot.sources_fresh ? "Fresh" : "Needs check"),
      detailPair("Rate contract", robot.rates_ok === false ? "Below minimum" : "Healthy"),
    );
  }
  if (robot.fault) wrapper.append(node("p", "fault-message", robot.fault));
  if (Array.isArray(robot.sources) && robot.sources.length) {
    const sources = node("div", "robot-sources");
    const sourceTitle = node("div", "robot-sources-title");
    const healthy = robot.sources.filter(
      (source) => source.fresh && source.rate_ok !== false,
    ).length;
    sourceTitle.append(
      node("span", "", "SOURCE TELEMETRY"),
      node("span", "", `${healthy} / ${robot.sources.length} healthy`),
    );
    sources.append(sourceTitle, ...robot.sources.map(sourceDetail));
    wrapper.append(sources);
  }
  return wrapper;
}

function inspectorSection(title, value) {
  const section = node("section", "inspector-section");
  const heading = node("div", "inspector-section-title");
  heading.append(node("span", "", title), node("span", "", value));
  section.append(heading);
  return section;
}

const EVENT_LABELS = Object.freeze({
  agent_online: "AGENT ONLINE",
  agent_stale: "AGENT STALE",
  agent_offline: "AGENT OFFLINE",
  agent_recovered: "AGENT RECOVERED",
  topic_stale: "TOPIC STALE",
  topic_recovered: "TOPIC RECOVERED",
  topic_rate_low: "RATE LOW",
  topic_rate_recovered: "RATE RECOVERED",
});

function eventRow(event) {
  const item = node("article", `event-row severity-${event.severity}`);
  item.dataset.renderKey = `event:${event.id}`;
  const head = node("div", "event-head");
  head.append(
    node("span", "event-type", EVENT_LABELS[event.event_type] || event.event_type),
    node("time", "event-time", formatClock(event.occurred_at)),
  );
  const identity = event.source_key || event.robot_id || event.agent_id;
  item.append(head, node("strong", "event-identity", identity));
  if (event.topic) item.append(node("code", "event-topic", event.topic));
  item.append(node("p", "event-message", event.message));
  return item;
}

function healthHistory(agent) {
  const counts = agent.event_counts || {};
  const recoveries = (counts.agent_recovered || 0)
    + (counts.topic_recovered || 0)
    + (counts.topic_rate_recovered || 0);
  const incidents = (counts.agent_stale || 0)
    + (counts.agent_offline || 0)
    + (counts.topic_stale || 0)
    + (counts.topic_rate_low || 0);
  const section = inspectorSection("Health history", `${recoveries} recoveries`);
  const summary = node("div", "event-summary");
  summary.append(
    detailPair("Incidents retained", formatNumber(incidents)),
    detailPair("Recoveries retained", formatNumber(recoveries)),
  );
  const events = state.events
    .filter((event) => event.agent_id === agent.agent_id)
    .slice(0, 12);
  const timeline = node("div", "event-timeline");
  if (events.length) {
    timeline.append(...events.map(eventRow));
  } else {
    timeline.append(node("p", "event-empty", "아직 기록된 health 전이가 없습니다."));
  }
  section.append(summary, timeline);
  return section;
}

const RECORDING_STATE_LABELS = Object.freeze({
  complete: "COMPLETE",
  failed: "FAILED",
  partial: "PARTIAL",
  invalid: "INVALID",
});

function recordingSessionCard(session) {
  const item = node("article", `recording-session state-${session.state}`);
  item.dataset.renderKey = `recording:${session.session_id}`;
  const head = node("div", "recording-session-head");
  head.append(
    node("span", "recording-state", RECORDING_STATE_LABELS[session.state] || session.state),
    node("code", "recording-session-id", session.session_id),
  );
  const scope = node("p", "recording-session-scope");
  scope.textContent = [session.profile_id, session.robot_id].filter(Boolean).join(" · ") || "metadata unavailable";

  const facts = node("div", "recording-session-facts");
  facts.append(
    detailPair("Size", formatBytes(session.total_size_bytes)),
    detailPair("Topics", session.topic_count == null ? "--" : formatNumber(session.topic_count)),
    detailPair("Messages", session.message_count == null ? "--" : formatNumber(session.message_count)),
    detailPair(
      "Transport loss",
      session.transport_messages_lost == null ? "--" : formatNumber(session.transport_messages_lost),
    ),
    detailPair("Task Run", session.run_id || "Unassigned"),
    detailPair(
      "Task definition",
      session.task_definition_id
        ? `${session.task_definition_id} v${session.task_definition_version}`
        : "Unassigned",
    ),
    detailPair("Started", formatSessionTime(session.started_at)),
    detailPair("Finished", formatSessionTime(session.finished_at)),
  );
  item.append(head, scope, facts);
  return item;
}

function recordingInventoryContent(agent, robotId = null) {
  const recordings = recordingInventory(agent);
  const content = node("div", "recording-inventory");
  const summary = node("div", "recording-inventory-summary");
  summary.append(
    detailPair("Complete", formatNumber(recordings.complete_count)),
    detailPair("Failed", formatNumber(recordings.failed_count)),
    detailPair("Partial", formatNumber(recordings.partial_count)),
    detailPair("Invalid", formatNumber(recordings.invalid_count)),
    detailPair("Declared size", formatBytes(recordings.total_size_bytes)),
    detailPair("Central transfer", "Metadata only"),
  );
  content.append(summary);

  if (!recordings.enabled) {
    content.append(node("p", "event-empty", "이 Agent에는 로컬 녹화 inventory가 설정되지 않았습니다."));
    return content;
  }
  if (recordings.error_code) {
    content.append(node("p", "recording-warning", `Inventory ${recordings.state}: ${recordings.error_code}`));
  }
  if (recordings.truncated) {
    content.append(node("p", "recording-warning", "세션 수가 scan 한도를 넘어 최신 범위만 집계했습니다."));
  }
  const sessions = (recordings.recent || []).filter(
    (session) => !robotId || session.robot_id === robotId,
  );
  const list = node("div", "recording-session-list");
  if (sessions.length) list.append(...sessions.map(recordingSessionCard));
  else list.append(node("p", "event-empty", robotId ? "이 로봇의 최근 녹화 세션이 없습니다." : "최근 녹화 세션이 없습니다."));
  content.append(list);
  return content;
}

function renderInspector() {
  const inspector = byId("agent-inspector");
  const agent = state.agents.find((item) => item.agent_id === state.selectedAgentId);
  if (!agent) {
    const placeholder = node("div", "inspector-placeholder");
    placeholder.dataset.renderKey = "placeholder";
    placeholder.append(
      node("span", "placeholder-index", "--"),
      node("p", "", "현재 필터에서 Agent를 선택할 수 없습니다."),
    );
    reconcileChildren(inspector, [placeholder]);
    return;
  }

  const content = node("div", "inspector-content");
  content.dataset.renderKey = `agent:${agent.agent_id}`;
  const head = node("header", "inspector-head");
  const top = node("div", "inspector-head-top");
  top.append(
    node("span", "eyebrow", "SELECTED AGENT"),
    node("span", `status-badge ${agent.status}`, statusLabel(agent.status)),
  );
  head.append(
    top,
    node("h3", "", agent.agent_name),
    node("p", "", `${agent.agent_id} · ${agent.sw_version}`),
  );
  content.append(head);

  const robots = inspectorSection("Robots", `${agent.robots.length} attached`);
  robots.append(...agent.robots.map((robot) => robotDetail(robot, agent.agent_id)));
  content.append(robots);

  const resources = inspectorSection("Agent resources", "latest snapshot");
  const resourceGrid = node("div", "detail-grid");
  resourceGrid.append(
    detailPair("CPU", agent.storage.cpu_pct == null ? "--" : `${formatNumber(agent.storage.cpu_pct, 1)}%`),
    detailPair("RAM free", agent.storage.ram_free_gb == null ? "--" : `${formatNumber(agent.storage.ram_free_gb, 1)} GB`),
    detailPair("Disk free", agent.storage.disk_free_gb == null ? "--" : `${formatNumber(agent.storage.disk_free_gb, 1)} GB`),
    detailPair("Clock skew", `${formatNumber(agent.clock_skew_s, 2)} s`),
  );
  resources.append(resourceGrid);
  content.append(resources);

  const recordings = recordingInventory(agent);
  const data = inspectorSection(
    "Local recordings",
    recordings.enabled ? `${recordings.scanned_count} scanned` : "not configured",
  );
  data.append(recordingInventoryContent(agent));
  content.append(data);

  content.append(healthHistory(agent));

  reconcileChildren(inspector, [content]);
}

function detailSection(kicker, title, meta = "") {
  const section = node("section", "dossier-section");
  const heading = node("header", "dossier-heading");
  const headingText = node("div");
  headingText.append(node("p", "eyebrow", kicker), node("h2", "", title));
  heading.append(headingText);
  if (meta) heading.append(node("span", "dossier-meta", meta));
  const body = node("div", "dossier-body");
  section.append(heading, body);
  return { section, body };
}

function robotRouteIdentity(agent, robot) {
  return `${agent.agent_id}:${robot.robot_id}`;
}

function renderRobotSelector() {
  const selector = byId("robot-selector");
  if (!selector) return;
  const route = currentRoute();
  const options = [node("option", "", "로봇을 선택하세요")];
  options[0].value = "";
  options[0].dataset.renderKey = "robot-option:none";
  for (const { agent, robot } of observedRobots()) {
    const option = node(
      "option",
      "",
      `${agent.agent_name} · ${robot.robot_id}`,
    );
    option.value = detailHash(agent.agent_id, robot.robot_id);
    option.dataset.renderKey = `robot-option:${agent.agent_id}:${robot.robot_id}`;
    options.push(option);
  }
  reconcileChildren(selector, options);
  selector.value = route.view === "robot" && route.agentId && route.robotId
    ? detailHash(route.agentId, route.robotId)
    : "";
  setText("robot-selector-meta", `${observedRobots().length} robots observed`);
}

function stopPreview({ render = true, status = "idle" } = {}) {
  const image = byId("active-camera-preview");
  const controller = state.preview.controller;
  const frameUrl = state.preview.frameUrl;
  state.preview = {
    identity: state.preview.identity,
    sourceKey: state.preview.sourceKey,
    status,
    active: false,
    controller: null,
    frameUrl: null,
  };
  if (controller) controller.abort();
  if (image) {
    image.src = "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=";
    image.removeAttribute("src");
  }
  if (frameUrl) URL.revokeObjectURL(frameUrl);
  if (render && currentRoute().view === "robot") renderRobotDetailView();
}

function detailEmpty(title, message) {
  const empty = node("section", "detail-empty");
  empty.dataset.renderKey = `empty:${title}`;
  empty.append(
    node("span", "placeholder-index", "02"),
    node("strong", "", title),
    node("p", "", message),
  );
  return empty;
}

function overviewDossier(agent, robot) {
  const { section, body } = detailSection(
    "ROBOT / LIVE CONTRACT",
    "로봇 개요",
    `${statusLabel(agent.status)} · ${robot.phase}`,
  );
  section.classList.add("overview-dossier");
  const status = node("div", "dossier-status");
  const identity = node("div", "dossier-status-identity");
  identity.append(
    node("span", `status-line ${agent.status}`),
    node("div", "", robot.recording.active ? "CAPTURING" : robot.fsm_state),
  );
  status.append(
    identity,
    node("span", `status-badge ${agent.status}`, statusLabel(agent.status)),
  );

  const facts = node("div", "overview-facts");
  facts.append(
    detailPair("Model", robot.model),
    detailPair("Host Agent", agent.agent_name),
    detailPair("Sources", robot.sources_fresh ? "Fresh" : "Needs check"),
    detailPair("Rate contract", robot.rates_ok === false ? "Below minimum" : "Healthy"),
  );
  body.append(status, facts);
  if (robot.recording.active) {
    const recording = node("div", "recording-strip");
    recording.append(
      detailPair("Episode", `${robot.recording.episode_index + 1} / ${robot.recording.episodes_total || "--"}`),
      detailPair("Frames", formatNumber(robot.recording.frames)),
      detailPair("Elapsed", `${formatNumber(robot.recording.elapsed_s, 1)} s`),
    );
    if (robot.recording.current_instruction) {
      recording.append(detailPair("Instruction", robot.recording.current_instruction));
    }
    body.append(recording);
  }
  if (robot.fault) body.append(node("p", "fault-message", robot.fault));
  return section;
}

function selectedCamera(agent, robot, cameras) {
  const identity = robotRouteIdentity(agent, robot);
  const savedKey = state.detailCameraKeys.get(identity);
  const source = cameras.find((item) => item.key === savedKey) || cameras[0] || null;
  if (source && source.key !== savedKey) state.detailCameraKeys.set(identity, source.key);
  return source;
}

function cameraVisualState(agent, robot, source) {
  const identity = robotRouteIdentity(agent, robot);
  const sessionMatches = state.preview.identity === identity
    && state.preview.sourceKey === source.key;
  if (agent.status === "offline") return "offline";
  if (!source.preview_url) return "unavailable";
  if (sessionMatches && state.preview.status === "error") return "error";
  if (sessionMatches && state.preview.active) {
    if (state.preview.status === "loading") return "loading";
    return source.fresh ? "live" : "stale";
  }
  return "idle";
}

const CAMERA_STATE_LABELS = Object.freeze({
  idle: "대기",
  loading: "불러오는 중…",
  live: "실시간",
  stale: "지연됨",
  offline: "오프라인",
  error: "불러오기 실패",
  unavailable: "미리보기 미지원",
});

function appendBytes(left, right) {
  const combined = new Uint8Array(left.length + right.length);
  combined.set(left);
  combined.set(right, left.length);
  return combined;
}

function findMarker(bytes, first, second, offset = 0) {
  for (let index = offset; index < bytes.length - 1; index += 1) {
    if (bytes[index] === first && bytes[index + 1] === second) return index;
  }
  return -1;
}

function showPreviewFrame(identity, sourceKey, jpeg) {
  if (!state.preview.active
    || state.preview.identity !== identity
    || state.preview.sourceKey !== sourceKey) return;
  const frameUrl = URL.createObjectURL(new Blob([jpeg], { type: "image/jpeg" }));
  const previousUrl = state.preview.frameUrl;
  const firstFrame = state.preview.status === "loading";
  state.preview.frameUrl = frameUrl;
  state.preview.status = "live";
  if (firstFrame) {
    renderRobotDetailView();
  } else {
    const image = byId("active-camera-preview");
    if (image) image.src = frameUrl;
  }
  if (previousUrl) URL.revokeObjectURL(previousUrl);
}

async function streamCameraPreview(url, identity, sourceKey, controller) {
  let buffer = new Uint8Array(0);
  try {
    const separator = url.includes("?") ? "&" : "?";
    const response = await fetch(`${url}${separator}session=${Date.now()}`, {
      cache: "no-store",
      headers: { Accept: "multipart/x-mixed-replace" },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`preview HTTP ${response.status}`);
    if (!response.body) throw new Error("preview stream body unavailable");
    const reader = response.body.getReader();
    while (state.preview.active && !controller.signal.aborted) {
      const { done, value } = await reader.read();
      if (done) throw new Error("preview stream ended");
      buffer = appendBytes(buffer, value);
      while (buffer.length) {
        const start = findMarker(buffer, 0xff, 0xd8);
        if (start < 0) {
          buffer = buffer.slice(-1);
          break;
        }
        const end = findMarker(buffer, 0xff, 0xd9, start + 2);
        if (end < 0) {
          if (start > 0) buffer = buffer.slice(start);
          break;
        }
        showPreviewFrame(identity, sourceKey, buffer.slice(start, end + 2));
        buffer = buffer.slice(end + 2);
      }
    }
    await reader.cancel();
  } catch (error) {
    if (controller.signal.aborted) return;
    if (!state.preview.active
      || state.preview.identity !== identity
      || state.preview.sourceKey !== sourceKey) return;
    const frameUrl = state.preview.frameUrl;
    state.preview.active = false;
    state.preview.controller = null;
    state.preview.frameUrl = null;
    state.preview.status = "error";
    if (frameUrl) URL.revokeObjectURL(frameUrl);
    renderRobotDetailView();
  }
}

function cameraDossier(agent, robot) {
  const cameras = robot.sources.filter((source) => source.kind === "camera");
  const { section, body } = detailSection(
    "VISION / ON DEMAND",
    "카메라 미리보기",
    cameras.length ? `${cameras.length} sources · 1 stream max` : "NO CAMERA",
  );
  section.classList.add("camera-dossier");
  if (!cameras.length) {
    body.append(node("p", "camera-empty", "이 로봇에는 카메라 소스가 없습니다."));
    return section;
  }

  const source = selectedCamera(agent, robot, cameras);
  const identity = robotRouteIdentity(agent, robot);
  if (state.preview.active && (
    state.preview.identity !== identity || state.preview.sourceKey !== source.key
  )) {
    stopPreview({ render: false });
  }
  const visualState = cameraVisualState(agent, robot, source);
  const layout = node("div", "camera-layout");
  const cameraList = node("div", "camera-source-list");
  cameraList.setAttribute("role", "list");
  for (const camera of cameras) {
    const button = node("button", `camera-source ${camera === source ? "is-selected" : ""}`);
    button.type = "button";
    button.dataset.action = "select-camera";
    button.dataset.sourceKey = camera.key;
    button.dataset.renderKey = `camera-source:${camera.key}`;
    button.setAttribute("aria-pressed", String(camera === source));
    button.append(
      node("span", "source-kind kind-camera", "CAM"),
      node("strong", "", camera.key),
      node(
        "span",
        `camera-source-health ${camera.fresh ? "is-fresh" : "is-stale"}`,
        camera.fresh ? "FRESH" : "STALE",
      ),
    );
    cameraList.append(button);
  }

  const viewer = node("div", "camera-viewer");
  viewer.dataset.renderKey = `camera-viewer:${identity}:${source.key}`;
  const frame = node("div", `camera-frame state-${visualState}`);
  frame.setAttribute("aria-live", "polite");
  if (state.preview.active && state.preview.identity === identity && state.preview.sourceKey === source.key) {
    const image = node("img", "camera-image");
    image.id = "active-camera-preview";
    image.dataset.renderKey = `camera-image:${identity}:${source.key}`;
    image.hidden = !state.preview.frameUrl;
    if (state.preview.frameUrl) image.src = state.preview.frameUrl;
    image.alt = `${robot.robot_id} ${source.key} 실시간 카메라 영상`;
    frame.append(image);
  } else {
    const placeholder = node("div", "camera-placeholder");
    placeholder.append(
      node("span", "camera-state-index", CAMERA_STATE_LABELS[visualState]),
      node(
        "p",
        "",
        visualState === "error"
          ? "Agent 미리보기 서버에서 영상을 불러오지 못했습니다."
          : visualState === "unavailable"
            ? "이 Agent는 아직 카메라 미리보기를 제공하지 않습니다."
            : "영상은 요청할 때만 네트워크로 수신합니다.",
      ),
    );
    frame.append(placeholder);
  }
  frame.append(node("span", `camera-state-label state-${visualState}`, CAMERA_STATE_LABELS[visualState]));

  const topic = source.topics[0] || {};
  const caption = node("div", "camera-caption");
  const captionText = node("div");
  captionText.append(
    node("strong", "", source.key),
    node("code", "", topic.topic || "--"),
  );
  const metrics = node("div", "camera-metrics");
  metrics.append(
    node("span", "", formatRate(topic.rate_hz, topic.minimum_rate_hz)),
    node("span", "", formatLatency(topic.age_ms)),
    node("span", "", `#${formatNumber(topic.seq)}`),
  );
  const previewButton = node("button", "camera-preview-button");
  previewButton.type = "button";
  if (state.preview.active && state.preview.identity === identity && state.preview.sourceKey === source.key) {
    previewButton.dataset.action = "stop-preview";
    previewButton.textContent = "미리보기 중지";
  } else {
    previewButton.dataset.action = "start-preview";
    previewButton.textContent = visualState === "error" ? "다시 시도" : "미리보기 시작";
  }
  previewButton.disabled = agent.status === "offline" || !source.preview_url;
  previewButton.setAttribute("aria-label", `${source.key} ${previewButton.textContent}`);
  caption.append(captionText, metrics, previewButton);
  viewer.append(frame, caption);
  layout.append(cameraList, viewer);
  body.append(layout);
  return section;
}

function telemetryDossier(robot) {
  const healthy = robot.sources.filter(
    (source) => source.fresh && source.rate_ok !== false,
  ).length;
  const { section, body } = detailSection(
    "ROS 2 / SOURCE CONTRACT",
    "소스 텔레메트리",
    `${healthy} / ${robot.sources.length} healthy`,
  );
  const sources = node("div", "dossier-sources");
  sources.append(...robot.sources.map(sourceDetail));
  body.append(sources);
  return section;
}

function hostDossier(agent, robot) {
  const { section, body } = detailSection(
    "HOST / ROBOT AGENT",
    "호스트 Agent",
    "latest snapshot",
  );
  const facts = node("div", "detail-grid host-facts");
  facts.append(
    detailPair("Agent", agent.agent_name),
    detailPair("Agent ID", agent.agent_id),
    detailPair("Software", agent.sw_version),
    detailPair("Heartbeat age", formatAge(agent.age_s)),
    detailPair("CPU", agent.storage.cpu_pct == null ? "--" : `${formatNumber(agent.storage.cpu_pct, 1)}%`),
    detailPair("RAM free", agent.storage.ram_free_gb == null ? "--" : `${formatNumber(agent.storage.ram_free_gb, 1)} GB`),
    detailPair("Disk free", agent.storage.disk_free_gb == null ? "--" : `${formatNumber(agent.storage.disk_free_gb, 1)} GB`),
    detailPair("Clock skew", `${formatNumber(agent.clock_skew_s, 2)} s`),
  );
  body.append(facts);
  const siblings = agent.robots.filter((item) => item.robot_id !== robot.robot_id);
  if (siblings.length) {
    const siblingLinks = node("div", "sibling-links");
    siblingLinks.append(node("span", "", "같은 Agent의 다른 로봇"));
    for (const sibling of siblings) {
      const link = node("a", "inline-detail-link", `${sibling.robot_id} →`);
      link.href = detailHash(agent.agent_id, sibling.robot_id);
      siblingLinks.append(link);
    }
    body.append(siblingLinks);
  }
  return section;
}

function robotEvents(agent, robot) {
  const sourceKeys = new Set(robot.sources.map((source) => source.key));
  return state.events.filter((event) => (
    event.agent_id === agent.agent_id
    && (
      event.robot_id === robot.robot_id
      || (!event.robot_id && !event.source_key)
      || sourceKeys.has(event.source_key)
    )
  )).slice(0, 20);
}

function eventsDossier(agent, robot) {
  const events = robotEvents(agent, robot);
  const incidents = events.filter((event) => event.severity !== "info").length;
  const { section, body } = detailSection(
    "HEALTH / RETAINED EVENTS",
    "이벤트 이력",
    `${incidents} incidents`,
  );
  const timeline = node("div", "event-timeline");
  if (events.length) timeline.append(...events.map(eventRow));
  else timeline.append(node("p", "event-empty", "기록된 health 전이가 없습니다."));
  body.append(timeline);
  return section;
}

function dataDossier(agent, robot) {
  const recordings = recordingInventory(agent);
  const { section, body } = detailSection(
    "DATA / LOCAL MCAP",
    "로컬 녹화 세션",
    recordings.enabled ? `${recordings.scanned_count} scanned · metadata only` : "not configured",
  );
  body.append(recordingInventoryContent(agent, robot.robot_id));
  return section;
}

function renderRobotDetailView() {
  const route = currentRoute();
  const content = byId("robot-detail-content");
  setText("robot-detail-generated-at", formatClock(state.summary?.generated_at));
  if (!route.agentId || !route.robotId) {
    setText("robot-detail-title", "로봇 상세");
    setText("robot-detail-model", "로봇을 선택하세요");
    if (state.preview.active) stopPreview({ render: false });
    reconcileChildren(content, [detailEmpty(
      "로봇을 선택하세요.",
      "Fleet 행의 Sources 또는 위 선택 상자에서 상세 대상을 고를 수 있습니다.",
    )]);
    return;
  }
  const resolved = resolveRouteRobot(route);
  if (!resolved) {
    setText("robot-detail-title", route.robotId);
    setText("robot-detail-model", "현재 플릿 snapshot에서 찾을 수 없음");
    if (state.preview.active) stopPreview({ render: false });
    reconcileChildren(content, [detailEmpty(
      "해당 로봇을 찾을 수 없습니다.",
      "Agent가 아직 heartbeat를 보내지 않았거나 로봇이 플릿에서 제거되었습니다.",
    )]);
    return;
  }

  const { agent, robot } = resolved;
  const identity = robotRouteIdentity(agent, robot);
  if (state.preview.active && state.preview.identity !== identity) {
    stopPreview({ render: false });
  }
  setText("robot-detail-title", robot.robot_id);
  setText("robot-detail-model", `${robot.model} · ${agent.agent_name}`);
  const dossier = node("div", "robot-dossier");
  dossier.dataset.renderKey = `dossier:${identity}`;
  const columns = node("div", "dossier-columns");
  const primary = node("div", "dossier-column");
  const secondary = node("div", "dossier-column");
  primary.append(telemetryDossier(robot), eventsDossier(agent, robot));
  secondary.append(hostDossier(agent, robot), dataDossier(agent, robot));
  columns.append(primary, secondary);
  dossier.append(overviewDossier(agent, robot), cameraDossier(agent, robot), columns);
  reconcileChildren(content, [dossier]);
}

function renderRoute({ focusTitle = false } = {}) {
  const route = currentRoute();
  const isRobot = route.view === "robot";
  if (!isRobot && state.preview.active) stopPreview({ render: false });
  byId("fleet").hidden = isRobot;
  byId("robot-detail").hidden = !isRobot;
  const fleetNav = byId("fleet-nav");
  const detailNav = byId("robot-detail-nav");
  fleetNav.classList.toggle("is-active", !isRobot);
  detailNav.classList.toggle("is-active", isRobot);
  if (isRobot) {
    fleetNav.removeAttribute("aria-current");
    detailNav.setAttribute("aria-current", "page");
    renderRobotSelector();
    renderRobotDetailView();
    if (focusTitle) window.requestAnimationFrame(() => byId("robot-detail-title").focus());
  } else {
    detailNav.removeAttribute("aria-current");
    fleetNav.setAttribute("aria-current", "page");
  }
}

function setConnection(kind, label, detail) {
  const dot = byId("connection-dot");
  dot.className = `connection-dot ${kind ? `is-${kind}` : ""}`;
  setText("connection-label", label);
  setText("connection-detail", detail);
}

async function refresh() {
  const button = byId("refresh-button");
  button.disabled = true;
  try {
    const [agents, summary, events] = await Promise.all([
      getJson(API.agents),
      getJson(API.summary),
      getJson(API.events),
    ]);
    applyFleet(agents, summary, events);
    setConnection("live", "Central live", "Polling + fleet stream");
  } catch (error) {
    setConnection("error", "Central unavailable", error.message);
  } finally {
    button.disabled = false;
  }
}

function connectFleetSocket() {
  if (state.socket) state.socket.close();
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}${API.fleetSocket}`);
  state.socket = socket;
  socket.addEventListener("open", () => {
    setConnection("live", "Central live", "Fleet stream connected");
  });
  socket.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "fleet") {
      applyFleet(payload.agents, payload.summary, payload.events);
    }
  });
  socket.addEventListener("close", () => {
    if (state.socket !== socket) return;
    setConnection("", "재연결 중", "Polling remains active");
    window.setTimeout(connectFleetSocket, 2000);
  });
  socket.addEventListener("error", () => socket.close());
}

document.querySelectorAll(".filter").forEach((button) => {
  button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    document.querySelectorAll(".filter").forEach((item) => {
      item.classList.toggle("is-active", item === button);
    });
    renderAgents();
    renderInspector();
  });
});

byId("robot-selector").addEventListener("change", (event) => {
  if (event.target.value) location.hash = event.target.value;
});

byId("robot-detail-content").addEventListener("click", (event) => {
  const control = event.target.closest("[data-action]");
  if (!control) return;
  const resolved = resolveRouteRobot();
  if (!resolved) return;
  const { agent, robot } = resolved;
  const identity = robotRouteIdentity(agent, robot);
  if (control.dataset.action === "select-camera") {
    stopPreview({ render: false });
    state.detailCameraKeys.set(identity, control.dataset.sourceKey);
    state.preview = {
      identity,
      sourceKey: control.dataset.sourceKey,
      status: "idle",
      active: false,
      controller: null,
      frameUrl: null,
    };
    renderRobotDetailView();
    return;
  }
  if (control.dataset.action === "stop-preview") {
    stopPreview();
    return;
  }
  if (control.dataset.action === "start-preview") {
    const cameras = robot.sources.filter((source) => source.kind === "camera");
    const source = selectedCamera(agent, robot, cameras);
    if (!source?.preview_url || agent.status === "offline") return;
    const controller = new AbortController();
    state.preview = {
      identity,
      sourceKey: source.key,
      status: "loading",
      active: true,
      controller,
      frameUrl: null,
    };
    renderRobotDetailView();
    void streamCameraPreview(source.preview_url, identity, source.key, controller);
  }
});

window.addEventListener("hashchange", () => renderRoute({ focusTitle: true }));
byId("refresh-button").addEventListener("click", refresh);
renderRoute();
refresh();
connectFleetSocket();
window.setInterval(refresh, 5000);
