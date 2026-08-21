"use strict";

const API = Object.freeze({
  configuration: "/api/v1/configuration",
  discovery: "/api/v1/config/discovery",
  agents: "/api/v1/agents",
  nodes: "/api/v1/config/processing-nodes",
  assets: "/api/v1/config/assets",
  workcells: "/api/v1/config/workcells",
  tasks: "/api/v1/config/task-definitions",
  runs: "/api/v1/config/task-runs",
});

const ASSET_TYPES = Object.freeze([
  ["robot", "Robot"],
  ["camera", "Camera"],
  ["eef", "End effector"],
  ["welder", "Welder"],
  ["feeder", "Wire / material feeder"],
  ["mobile_robot", "Mobile robot"],
  ["sensor", "Sensor"],
  ["other", "Other"],
]);

const ACCESS_MODES = Object.freeze([
  ["exclusive", "exclusive"],
  ["shared_read", "shared read"],
]);

const state = {
  configuration: {
    processing_nodes: [],
    assets: [],
    workcells: [],
    task_definitions: [],
    task_runs: [],
    audit: [],
  },
  discovery: { robots: [], cameras: [] },
  agents: [],
  roles: [{ role: "primary_robot", asset_type: "robot", access_mode: "exclusive" }],
  toastTimer: null,
};

const byId = (id) => document.getElementById(id);

function element(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = String(text);
  return result;
}

function option(value, label) {
  const result = element("option", "", label);
  result.value = value;
  return result;
}

function safeSuggestion(value) {
  return String(value)
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^[^a-z0-9]+/, "")
    .slice(0, 64) || "asset";
}

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

async function postJson(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${path}: HTTP ${response.status}`);
  return payload;
}

function showToast(message, isError = false) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.classList.toggle("is-error", isError);
  toast.hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { toast.hidden = true; }, 4200);
}

function setConnection(live, detail) {
  byId("connection-dot").className = `connection-dot ${live ? "is-live" : "is-error"}`;
  byId("connection-label").textContent = detail;
}

function selectedValues(container, name) {
  return [...container.querySelectorAll(`input[name="${name}"]:checked`)].map(
    (input) => input.value,
  );
}

function registryItem(title, subtitle, aside) {
  const row = element("div", "registry-item");
  row.append(element("strong", "", title));
  row.append(element("span", "", subtitle));
  row.append(element("span", "", aside));
  return row;
}

function renderMetrics() {
  const config = state.configuration;
  byId("metric-nodes").textContent = config.processing_nodes.length;
  byId("metric-assets").textContent = config.assets.length;
  byId("metric-workcells").textContent = config.workcells.length;
  byId("metric-runs").textContent = config.task_runs.length;
}

function renderAgentOptions() {
  const container = byId("agent-options");
  container.replaceChildren();
  const assigned = new Set(
    state.configuration.processing_nodes.flatMap((node) => node.agent_ids),
  );
  const available = state.agents.filter((agent) => !assigned.has(agent.agent_id));
  if (!available.length) {
    container.append(element("p", "empty-copy", "새 Node에 배정할 미등록 Agent가 없습니다."));
    return;
  }
  for (const agent of available) {
    const label = element("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "agent_ids";
    input.value = agent.agent_id;
    label.append(input, document.createTextNode(`${agent.agent_name} · ${agent.agent_id}`));
    container.append(label);
  }
}

function renderNodeChoices() {
  const select = document.querySelector('#asset-form select[name="node_id"]');
  const current = select.value;
  select.replaceChildren(option("", "미지정"));
  for (const node of state.configuration.processing_nodes) {
    select.append(option(node.node_id, `${node.display_name} · ${node.node_id}`));
  }
  if ([...select.options].some((item) => item.value === current)) select.value = current;
}

function renderNodes() {
  const registry = byId("node-registry");
  registry.replaceChildren();
  if (!state.configuration.processing_nodes.length) {
    registry.append(registryItem("등록된 Node 없음", "Agent를 먼저 Node에 연결하세요.", "0"));
  }
  for (const node of state.configuration.processing_nodes) {
    registry.append(registryItem(
      node.display_name,
      node.node_id,
      `${node.agent_ids.length} agents`,
    ));
  }
  renderAgentOptions();
  renderNodeChoices();
}

function claimCandidate(candidate, assetType) {
  const form = byId("asset-form");
  const sourceKey = assetType === "camera" ? candidate.source_key : "";
  const suffix = sourceKey ? `-${sourceKey}` : "";
  form.elements.asset_id.value = safeSuggestion(`${candidate.robot_id}${suffix}`);
  form.elements.display_name.value = sourceKey
    ? `${candidate.robot_id} · ${sourceKey}`
    : `${candidate.model || candidate.robot_id}`;
  form.elements.asset_type.value = assetType;
  form.elements.node_id.value = candidate.node_id || "";
  form.elements.binding_agent_id.value = candidate.agent_id;
  form.elements.binding_robot_id.value = candidate.robot_id;
  form.elements.binding_source_key.value = sourceKey;
  const banner = byId("binding-banner");
  banner.textContent = `OBSERVED BINDING · ${candidate.agent_id} / ${candidate.robot_id}${sourceKey ? ` / ${sourceKey}` : ""}`;
  banner.hidden = false;
  byId("asset-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  form.elements.asset_id.focus({ preventScroll: true });
}

function discoveryCard(candidate, type) {
  const card = element("article", "discovery-card");
  const title = type === "camera" ? candidate.source_key : candidate.robot_id;
  const details = type === "camera"
    ? `${candidate.robot_id} · ${candidate.fresh ? "fresh" : "stale"}`
    : `${candidate.model} · ${candidate.status}`;
  card.append(element("strong", "", title));
  card.append(element("span", "", `${candidate.agent_name} / ${details}`));
  const button = element("button", "claim-button", type === "camera" ? "Camera 등록" : "Robot 등록");
  button.type = "button";
  button.disabled = !candidate.node_id;
  button.title = candidate.node_id ? "Asset 폼에 연결 정보 채우기" : "Processing Node를 먼저 등록하세요";
  button.addEventListener("click", () => claimCandidate(candidate, type));
  card.append(button);
  return card;
}

function renderDiscovery() {
  const list = byId("discovery-list");
  list.replaceChildren();
  const total = state.discovery.robots.length + state.discovery.cameras.length;
  byId("discovery-count").textContent = `${total} unclaimed`;
  for (const robot of state.discovery.robots) list.append(discoveryCard(robot, "robot"));
  for (const camera of state.discovery.cameras) list.append(discoveryCard(camera, "camera"));
  if (!total) list.append(element("p", "empty-copy", "모든 관측 후보가 등록됐거나 Heartbeat를 기다리는 중입니다."));
}

function renderAssets() {
  const registry = byId("asset-registry");
  registry.replaceChildren();
  if (!state.configuration.assets.length) {
    registry.append(registryItem("등록된 Asset 없음", "관측 후보 또는 일반 장비를 등록하세요.", "0"));
  }
  for (const asset of state.configuration.assets) {
    const binding = asset.binding
      ? `${asset.binding.agent_id}/${asset.binding.robot_id}${asset.binding.source_key ? `/${asset.binding.source_key}` : ""}`
      : "configuration only";
    registry.append(registryItem(asset.display_name, `${asset.asset_id} · ${binding}`, asset.asset_type));
  }
  const options = byId("workcell-asset-options");
  options.replaceChildren();
  for (const asset of state.configuration.assets) {
    const label = element("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "asset_ids";
    input.value = asset.asset_id;
    label.append(input, document.createTextNode(`${asset.display_name} · ${asset.asset_type}`));
    options.append(label);
  }
  if (!state.configuration.assets.length) {
    options.append(element("p", "empty-copy", "등록된 Asset이 없습니다."));
  }
}

function renderWorkcells() {
  const registry = byId("workcell-registry");
  registry.replaceChildren();
  if (!state.configuration.workcells.length) {
    registry.append(registryItem("등록된 Workcell 없음", "공간·안전 경계를 공유하는 Asset을 묶으세요.", "0"));
  }
  for (const workcell of state.configuration.workcells) {
    registry.append(registryItem(
      workcell.display_name,
      workcell.workcell_id,
      `${workcell.asset_ids.length} assets`,
    ));
  }
  const select = document.querySelector('#run-form select[name="workcell_id"]');
  const current = select.value;
  select.replaceChildren(option("", "전체 자산"));
  for (const workcell of state.configuration.workcells) {
    select.append(option(workcell.workcell_id, `${workcell.display_name} · ${workcell.workcell_id}`));
  }
  if ([...select.options].some((item) => item.value === current)) select.value = current;
}

function selectWithChoices(choices, selected) {
  const select = document.createElement("select");
  for (const [value, label] of choices) select.append(option(value, label));
  select.value = selected;
  return select;
}

function renderRoleEditor() {
  const editor = byId("role-editor");
  editor.replaceChildren();
  state.roles.forEach((role, index) => {
    const row = element("div", "role-row");
    const roleLabel = element("label", "", "Role ID");
    const roleInput = document.createElement("input");
    roleInput.required = true;
    roleInput.pattern = "[A-Za-z0-9][A-Za-z0-9._-]*";
    roleInput.value = role.role;
    roleInput.addEventListener("input", () => { state.roles[index].role = roleInput.value; });
    roleLabel.append(roleInput);
    const typeLabel = element("label", "", "Asset type");
    const typeSelect = selectWithChoices(ASSET_TYPES, role.asset_type);
    typeSelect.addEventListener("change", () => { state.roles[index].asset_type = typeSelect.value; });
    typeLabel.append(typeSelect);
    const accessLabel = element("label", "", "Access");
    const accessSelect = selectWithChoices(ACCESS_MODES, role.access_mode);
    accessSelect.addEventListener("change", () => { state.roles[index].access_mode = accessSelect.value; });
    accessLabel.append(accessSelect);
    const remove = element("button", "", "삭제");
    remove.type = "button";
    remove.disabled = state.roles.length === 1;
    remove.addEventListener("click", () => {
      state.roles.splice(index, 1);
      renderRoleEditor();
    });
    row.append(roleLabel, typeLabel, accessLabel, remove);
    editor.append(row);
  });
}

function taskKey(task) {
  return `${task.task_definition_id}@${task.version}`;
}

function selectedTask() {
  const key = document.querySelector('#run-form select[name="task_definition"]').value;
  return state.configuration.task_definitions.find((task) => taskKey(task) === key) || null;
}

function eligibleAssets(role) {
  const workcellId = document.querySelector('#run-form select[name="workcell_id"]').value;
  const workcell = state.configuration.workcells.find((item) => item.workcell_id === workcellId);
  const allowed = workcell ? new Set(workcell.asset_ids) : null;
  return state.configuration.assets.filter(
    (asset) => asset.asset_type === role.asset_type && (!allowed || allowed.has(asset.asset_id)),
  );
}

function renderAssignments() {
  const editor = byId("assignment-editor");
  editor.replaceChildren();
  const task = selectedTask();
  if (!task) {
    editor.append(element("p", "empty-copy", "Task 버전을 먼저 선택하세요."));
    return;
  }
  for (const role of task.roles) {
    const row = element("div", "assignment-row");
    row.dataset.role = role.role;
    row.dataset.accessMode = role.access_mode;
    const identity = element("div");
    identity.append(element("strong", "", role.role), element("span", "", role.asset_type));
    const select = document.createElement("select");
    select.append(option("", "배정 안 함"));
    for (const asset of eligibleAssets(role)) {
      select.append(option(asset.asset_id, `${asset.display_name} · ${asset.asset_id}`));
    }
    const access = element("span", "", role.access_mode);
    row.append(identity, select, access);
    editor.append(row);
  }
}

function renderTasks() {
  const registry = byId("task-registry");
  registry.replaceChildren();
  if (!state.configuration.task_definitions.length) {
    registry.append(registryItem("등록된 Task 없음", "역할 중심의 재사용 가능한 recipe를 정의하세요.", "0"));
  }
  for (const task of state.configuration.task_definitions) {
    registry.append(registryItem(
      task.display_name,
      taskKey(task),
      `${task.roles.length} roles`,
    ));
  }
  const select = document.querySelector('#run-form select[name="task_definition"]');
  const current = select.value;
  select.replaceChildren(option("", "선택"));
  for (const task of state.configuration.task_definitions) {
    select.append(option(taskKey(task), `${task.display_name} · v${task.version}`));
  }
  if ([...select.options].some((item) => item.value === current)) select.value = current;
  renderAssignments();
}

function renderRuns() {
  const registry = byId("run-registry");
  registry.replaceChildren();
  if (!state.configuration.task_runs.length) {
    registry.append(element("p", "empty-copy", "아직 생성된 Task Run이 없습니다."));
    return;
  }
  for (const run of state.configuration.task_runs) {
    const card = element("article", "run-card");
    card.append(element("strong", "", run.display_name));
    card.append(element("span", "", `${run.run_id} · ${run.task_definition_id}@${run.task_version}`));
    card.append(element("span", "run-state", `${run.state} · ${run.assignments.length} assigned`));
    const link = element("a", "", "OPERATOR 열기");
    link.href = `/operator/#run/${encodeURIComponent(run.run_id)}`;
    card.append(link);
    registry.append(card);
  }
}

function renderAll() {
  renderMetrics();
  renderNodes();
  renderDiscovery();
  renderAssets();
  renderWorkcells();
  renderTasks();
  renderRuns();
}

async function refresh() {
  try {
    const [configuration, discovery, agents] = await Promise.all([
      getJson(API.configuration),
      getJson(API.discovery),
      getJson(API.agents),
    ]);
    state.configuration = configuration;
    state.discovery = discovery;
    state.agents = agents;
    renderAll();
    setConnection(true, "Central live");
  } catch (error) {
    setConnection(false, "Central unavailable");
    showToast(error.message, true);
  }
}

function clearBinding() {
  const form = byId("asset-form");
  form.elements.binding_agent_id.value = "";
  form.elements.binding_robot_id.value = "";
  form.elements.binding_source_key.value = "";
  byId("binding-banner").hidden = true;
}

function updateSuggestedVersion() {
  const form = byId("task-form");
  const id = form.elements.task_definition_id.value;
  const versions = state.configuration.task_definitions
    .filter((task) => task.task_definition_id === id)
    .map((task) => task.version);
  form.elements.version.value = versions.length ? Math.max(...versions) + 1 : 1;
}

async function submit(form, path, body, successMessage) {
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await postJson(path, body);
    showToast(successMessage);
    form.reset();
    await refresh();
    return true;
  } catch (error) {
    showToast(error.message, true);
    return false;
  } finally {
    button.disabled = false;
  }
}

function bindForms() {
  byId("reload-button").addEventListener("click", refresh);
  byId("clear-binding").addEventListener("click", clearBinding);
  byId("add-role").addEventListener("click", () => {
    state.roles.push({ role: `role_${state.roles.length + 1}`, asset_type: "robot", access_mode: "exclusive" });
    renderRoleEditor();
  });
  document.querySelector('#task-form input[name="task_definition_id"]').addEventListener("input", updateSuggestedVersion);
  document.querySelector('#run-form select[name="task_definition"]').addEventListener("change", renderAssignments);
  document.querySelector('#run-form select[name="workcell_id"]').addEventListener("change", renderAssignments);

  byId("node-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    await submit(form, API.nodes, {
      node_id: form.elements.node_id.value,
      display_name: form.elements.display_name.value,
      agent_ids: selectedValues(byId("agent-options"), "agent_ids"),
    }, "Processing Node가 등록되었습니다.");
  });

  byId("asset-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const agentId = form.elements.binding_agent_id.value;
    const body = {
      asset_id: form.elements.asset_id.value,
      asset_type: form.elements.asset_type.value,
      display_name: form.elements.display_name.value,
      node_id: form.elements.node_id.value || null,
      binding: agentId ? {
        agent_id: agentId,
        robot_id: form.elements.binding_robot_id.value,
        source_key: form.elements.binding_source_key.value || null,
      } : null,
      capabilities: form.elements.capabilities.value
        .split(",").map((item) => item.trim()).filter(Boolean),
    };
    if (await submit(form, API.assets, body, "Asset이 등록되었습니다.")) clearBinding();
  });

  byId("workcell-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    await submit(form, API.workcells, {
      workcell_id: form.elements.workcell_id.value,
      display_name: form.elements.display_name.value,
      description: form.elements.description.value || null,
      asset_ids: selectedValues(byId("workcell-asset-options"), "asset_ids"),
    }, "Workcell이 생성되었습니다.");
  });

  byId("task-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const body = {
      task_definition_id: form.elements.task_definition_id.value,
      version: Number(form.elements.version.value),
      display_name: form.elements.display_name.value,
      description: form.elements.description.value || null,
      roles: state.roles.map((role) => ({ ...role })),
    };
    if (await submit(form, API.tasks, body, "Task Definition 버전이 저장되었습니다.")) {
      state.roles = [{ role: "primary_robot", asset_type: "robot", access_mode: "exclusive" }];
      renderRoleEditor();
    }
  });

  byId("run-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const task = selectedTask();
    if (!task) {
      showToast("Task 버전을 선택하세요.", true);
      return;
    }
    const assignments = [...byId("assignment-editor").querySelectorAll(".assignment-row")]
      .filter((row) => row.querySelector("select").value)
      .map((row) => ({
        role: row.dataset.role,
        asset_id: row.querySelector("select").value,
        access_mode: row.dataset.accessMode,
      }));
    await submit(form, API.runs, {
      run_id: form.elements.run_id.value,
      task_definition_id: task.task_definition_id,
      task_version: task.version,
      workcell_id: form.elements.workcell_id.value || null,
      display_name: form.elements.display_name.value,
      state: form.elements.state.value,
      assignments,
    }, "Task Run이 생성되었습니다. Operator 화면에서 열 수 있습니다.");
  });
}

function initialize() {
  const typeSelect = document.querySelector('#asset-form select[name="asset_type"]');
  for (const [value, label] of ASSET_TYPES) typeSelect.append(option(value, label));
  renderRoleEditor();
  bindForms();
  void refresh();
}

initialize();
