import {
  App as AntApp,
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Empty,
  Flex,
  Form,
  Input,
  InputNumber,
  List,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
  type TableColumnsType,
} from "antd";
import {
  ApartmentOutlined,
  CloudServerOutlined,
  DeploymentUnitOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { useEffect, useState } from "react";
import { errorMessage, getJson, requestJson } from "./api";
import AppShell from "./components/AppShell";
import PageHeader from "./components/PageHeader";
import type {
  Asset,
  AssetBinding,
  Configuration,
  Discovery,
  DiscoveryCamera,
  DiscoveryRobot,
  ProcessingNode,
  TaskDefinition,
  TaskRole,
  TaskRun,
  Workcell,
} from "./operations-types";
import type { AgentSnapshot, ConnectionState } from "./types";
import { formatNumber } from "./utils";

const { Text } = Typography;

const API = Object.freeze({
  agents: "/api/v1/agents",
  assets: "/api/v1/config/assets",
  configuration: "/api/v1/configuration",
  discovery: "/api/v1/config/discovery",
  nodes: "/api/v1/config/processing-nodes",
  runs: "/api/v1/config/task-runs",
  tasks: "/api/v1/config/task-definitions",
  workcells: "/api/v1/config/workcells",
});

const ASSET_TYPES = [
  { label: "Robot", value: "robot" },
  { label: "Camera", value: "camera" },
  { label: "End effector", value: "eef" },
  { label: "Welder", value: "welder" },
  { label: "Wire / material feeder", value: "feeder" },
  { label: "Mobile robot", value: "mobile_robot" },
  { label: "Sensor", value: "sensor" },
  { label: "Other", value: "other" },
];

const ACCESS_MODES = [
  { label: "Exclusive", value: "exclusive" },
  { label: "Shared read", value: "shared_read" },
];

const EMPTY_CONFIGURATION: Configuration = {
  assets: [], audit: [], processing_nodes: [], task_definitions: [], task_runs: [], workcells: [],
};
const EMPTY_DISCOVERY: Discovery = { cameras: [], robots: [] };
const ID_RULE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

type ResourceKind = "node" | "asset" | "workcell" | "task" | "run";
type EditableResource = ProcessingNode | Asset | Workcell;

interface ResourceModalState {
  editing?: EditableResource;
  kind: ResourceKind;
}

interface ResourceFormValues {
  agent_ids?: string[];
  asset_id?: string;
  asset_ids?: string[];
  asset_type?: string;
  assignments?: Record<string, string>;
  binding?: AssetBinding | null;
  capabilities?: string;
  description?: string;
  display_name?: string;
  node_id?: string;
  roles?: TaskRole[];
  run_id?: string;
  state?: string;
  task_definition?: string;
  task_definition_id?: string;
  version?: number;
  workcell_id?: string;
}

function isActive(item: { lifecycle_state: string }) {
  return item.lifecycle_state !== "decommissioned";
}

function taskKey(task: TaskDefinition) {
  return `${task.task_definition_id}@${task.version}`;
}

function safeSuggestion(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[^a-z0-9]+/, "").slice(0, 64) || "asset";
}

function lifecycleTag(value: string) {
  return <Tag color={value === "decommissioned" ? "default" : "success"}>{value.toUpperCase()}</Tag>;
}

function runColor(state: string) {
  if (state === "ACTIVE") return "processing";
  if (state === "READY" || state === "COMPLETE") return "success";
  if (state === "FAILED") return "error";
  return "default";
}

export default function ConfigApp() {
  const { message, modal } = AntApp.useApp();
  const [configuration, setConfiguration] = useState(EMPTY_CONFIGURATION);
  const [discovery, setDiscovery] = useState(EMPTY_DISCOVERY);
  const [agents, setAgents] = useState<AgentSnapshot[]>([]);
  const [connection, setConnection] = useState<ConnectionState>({ detail: "Loading registry", kind: "connecting", label: "Connecting" });
  const [loading, setLoading] = useState(false);
  const [resourceModal, setResourceModal] = useState<ResourceModalState | null>(null);
  const [decommissionTarget, setDecommissionTarget] = useState<{ collection: "assets" | "processing-nodes" | "workcells"; id: string; revision: number } | null>(null);
  const [failureRun, setFailureRun] = useState<TaskRun | null>(null);
  const [form] = Form.useForm<ResourceFormValues>();
  const [reasonForm] = Form.useForm<{ reason: string }>();
  const selectedTaskKey = Form.useWatch("task_definition", form);
  const selectedWorkcellId = Form.useWatch("workcell_id", form);

  async function refresh() {
    setLoading(true);
    try {
      const [nextConfiguration, nextDiscovery, nextAgents] = await Promise.all([
        getJson<Configuration>(API.configuration),
        getJson<Discovery>(API.discovery),
        getJson<AgentSnapshot[]>(API.agents),
      ]);
      setConfiguration(nextConfiguration);
      setDiscovery(nextDiscovery);
      setAgents(nextAgents);
      setConnection({ detail: "Configuration registry", kind: "live", label: "Central live" });
    } catch (error) {
      setConnection({ detail: errorMessage(error), kind: "error", label: "Central unavailable" });
      void message.error(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void refresh(); }, []);

  function closeResourceModal() {
    setResourceModal(null);
    form.resetFields();
  }

  function openResource(kind: ResourceKind, editing?: EditableResource, initial?: Partial<ResourceFormValues>) {
    setResourceModal({ editing, kind });
    if (kind === "node") {
      const node = editing as ProcessingNode | undefined;
      form.setFieldsValue(node ? { agent_ids: node.agent_ids, display_name: node.display_name, node_id: node.node_id } : { agent_ids: [], ...initial });
    } else if (kind === "asset") {
      const asset = editing as Asset | undefined;
      form.setFieldsValue(asset ? {
        asset_id: asset.asset_id,
        asset_type: asset.asset_type,
        binding: asset.binding,
        capabilities: asset.capabilities.join(", "),
        display_name: asset.display_name,
        node_id: asset.node_id || undefined,
      } : { asset_type: "robot", binding: null, ...initial });
    } else if (kind === "workcell") {
      const workcell = editing as Workcell | undefined;
      form.setFieldsValue(workcell ? {
        asset_ids: workcell.asset_ids,
        description: workcell.description || undefined,
        display_name: workcell.display_name,
        workcell_id: workcell.workcell_id,
      } : { asset_ids: [], ...initial });
    } else if (kind === "task") {
      form.setFieldsValue({ roles: [{ access_mode: "exclusive", asset_type: "robot", role: "primary_robot" }], version: 1, ...initial });
    } else {
      form.setFieldsValue({ assignments: {}, state: "DRAFT", ...initial });
    }
  }

  async function saveResource(values: ResourceFormValues) {
    if (!resourceModal) return;
    const { editing, kind } = resourceModal;
    try {
      if (kind === "node") {
        const node = editing as ProcessingNode | undefined;
        await requestJson(node ? "PUT" : "POST", node ? `${API.nodes}/${encodeURIComponent(node.node_id)}` : API.nodes, {
          ...(node ? { expected_revision: node.revision } : { node_id: values.node_id }),
          agent_ids: values.agent_ids || [], display_name: values.display_name,
        });
      } else if (kind === "asset") {
        const asset = editing as Asset | undefined;
        await requestJson(asset ? "PUT" : "POST", asset ? `${API.assets}/${encodeURIComponent(asset.asset_id)}` : API.assets, {
          ...(asset ? { expected_revision: asset.revision } : { asset_id: values.asset_id, asset_type: values.asset_type }),
          binding: values.binding || null,
          capabilities: (values.capabilities || "").split(",").map((item) => item.trim()).filter(Boolean),
          display_name: values.display_name,
          node_id: values.node_id || null,
        });
      } else if (kind === "workcell") {
        const workcell = editing as Workcell | undefined;
        await requestJson(workcell ? "PUT" : "POST", workcell ? `${API.workcells}/${encodeURIComponent(workcell.workcell_id)}` : API.workcells, {
          ...(workcell ? { expected_revision: workcell.revision } : { workcell_id: values.workcell_id }),
          asset_ids: values.asset_ids || [], description: values.description || null, display_name: values.display_name,
        });
      } else if (kind === "task") {
        await requestJson("POST", API.tasks, {
          description: values.description || null,
          display_name: values.display_name,
          roles: values.roles || [],
          task_definition_id: values.task_definition_id,
          version: values.version,
        });
      } else {
        const task = configuration.task_definitions.find((item) => taskKey(item) === values.task_definition);
        if (!task) throw new Error("Select a Task version.");
        const assignments = task.roles.flatMap((role) => {
          const assetId = values.assignments?.[role.role];
          return assetId ? [{ access_mode: role.access_mode, asset_id: assetId, role: role.role }] : [];
        });
        await requestJson("POST", API.runs, {
          assignments,
          display_name: values.display_name,
          run_id: values.run_id,
          state: values.state,
          task_definition_id: task.task_definition_id,
          task_version: task.version,
          workcell_id: values.workcell_id || null,
        });
      }
      void message.success(`${kind === "run" ? "Task Run" : kind[0].toUpperCase() + kind.slice(1)} saved.`);
      closeResourceModal();
      await refresh();
    } catch (error) {
      void message.error(errorMessage(error));
    }
  }

  async function decommission(values: { reason: string }) {
    if (!decommissionTarget) return;
    try {
      const endpoint = decommissionTarget.collection === "processing-nodes" ? API.nodes : API[decommissionTarget.collection];
      await requestJson("POST", `${endpoint}/${encodeURIComponent(decommissionTarget.id)}/decommission`, {
        expected_revision: decommissionTarget.revision,
        reason: values.reason.trim(),
      });
      setDecommissionTarget(null);
      reasonForm.resetFields();
      void message.success(`${decommissionTarget.id} decommissioned.`);
      await refresh();
    } catch (error) {
      void message.error(errorMessage(error));
    }
  }

  async function transitionRun(run: TaskRun, state: string, reason?: string) {
    try {
      await requestJson("POST", `${API.runs}/${encodeURIComponent(run.run_id)}/transition`, {
        expected_revision: run.revision,
        state,
        ...(reason ? { reason } : {}),
      });
      void message.success(`${run.run_id}: ${state}`);
      setFailureRun(null);
      reasonForm.resetFields();
      await refresh();
    } catch (error) {
      void message.error(errorMessage(error));
    }
  }

  function confirmTransition(run: TaskRun, state: string) {
    if (state === "FAILED") {
      setFailureRun(run);
      return;
    }
    modal.confirm({
      content: `Move ${run.display_name} from ${run.state} to ${state}?`,
      okText: `Mark ${state.toLowerCase()}`,
      onOk: () => transitionRun(run, state),
      title: "Confirm Task Run transition",
    });
  }

  function claimCandidate(candidate: DiscoveryRobot | DiscoveryCamera, assetType: "robot" | "camera") {
    const sourceKey = assetType === "camera" ? (candidate as DiscoveryCamera).source_key : null;
    const robotId = candidate.robot_id;
    openResource("asset", undefined, {
      asset_id: safeSuggestion(`${robotId}${sourceKey ? `-${sourceKey}` : ""}`),
      asset_type: assetType,
      binding: { agent_id: candidate.agent_id, robot_id: robotId, source_key: sourceKey },
      display_name: sourceKey ? `${robotId} · ${sourceKey}` : (candidate as DiscoveryRobot).model || robotId,
      node_id: candidate.node_id || undefined,
    });
  }

  const activeNodes = configuration.processing_nodes.filter(isActive);
  const activeAssets = configuration.assets.filter(isActive);
  const activeWorkcells = configuration.workcells.filter(isActive);
  const editingNode = resourceModal?.kind === "node" ? resourceModal.editing as ProcessingNode | undefined : undefined;
  const assignedAgents = new Set(activeNodes.filter((node) => node.node_id !== editingNode?.node_id).flatMap((node) => node.agent_ids));
  const agentOptions = agents.filter((agent) => !assignedAgents.has(agent.agent_id) || editingNode?.agent_ids.includes(agent.agent_id)).map((agent) => ({ label: `${agent.agent_name} · ${agent.agent_id}`, value: agent.agent_id }));
  const selectedTask = configuration.task_definitions.find((task) => taskKey(task) === selectedTaskKey) || null;
  const selectedWorkcell = activeWorkcells.find((workcell) => workcell.workcell_id === selectedWorkcellId) || null;
  const allowedAssets = selectedWorkcell ? new Set(selectedWorkcell.asset_ids) : null;

  const resourceActions = (collection: "assets" | "processing-nodes" | "workcells", item: EditableResource, kind: "asset" | "node" | "workcell", id: string) => isActive(item) ? (
    <Space size={4}>
      <Button onClick={() => openResource(kind, item)} size="small" type="link">Edit</Button>
      <Button danger onClick={() => setDecommissionTarget({ collection, id, revision: item.revision })} size="small" type="link">Decommission</Button>
    </Space>
  ) : null;

  const nodeColumns: TableColumnsType<ProcessingNode> = [
    { dataIndex: "display_name", key: "name", render: (value, row) => <span className="table-identity"><strong>{value}</strong><small>{row.node_id} · rev {row.revision}</small></span>, title: "Processing node" },
    { dataIndex: "agent_ids", key: "agents", render: (values: string[]) => values.length ? values.map((value) => <Tag key={value}>{value}</Tag>) : <Text type="secondary">No agents</Text>, title: "Agents" },
    { dataIndex: "lifecycle_state", key: "state", render: lifecycleTag, title: "Lifecycle", width: 130 },
    { key: "actions", render: (_, row) => resourceActions("processing-nodes", row, "node", row.node_id), title: "", width: 180 },
  ];
  const assetColumns: TableColumnsType<Asset> = [
    { dataIndex: "display_name", key: "name", render: (value, row) => <span className="table-identity"><strong>{value}</strong><small>{row.asset_id} · rev {row.revision}</small></span>, title: "Asset" },
    { dataIndex: "asset_type", key: "type", render: (value) => <Tag>{String(value).toUpperCase()}</Tag>, title: "Type", width: 130 },
    { key: "binding", render: (_, row) => row.binding ? <Text code>{[row.binding.agent_id, row.binding.robot_id, row.binding.source_key].filter(Boolean).join(" / ")}</Text> : <Text type="secondary">Configuration only</Text>, title: "Observation binding" },
    { dataIndex: "lifecycle_state", key: "state", render: lifecycleTag, title: "Lifecycle", width: 130 },
    { key: "actions", render: (_, row) => resourceActions("assets", row, "asset", row.asset_id), title: "", width: 180 },
  ];
  const workcellColumns: TableColumnsType<Workcell> = [
    { dataIndex: "display_name", key: "name", render: (value, row) => <span className="table-identity"><strong>{value}</strong><small>{row.workcell_id} · rev {row.revision}</small></span>, title: "Workcell" },
    { dataIndex: "description", key: "description", title: "Description" },
    { dataIndex: "asset_ids", key: "assets", render: (values: string[]) => `${values.length} assets`, title: "Scope", width: 100 },
    { dataIndex: "lifecycle_state", key: "state", render: lifecycleTag, title: "Lifecycle", width: 130 },
    { key: "actions", render: (_, row) => resourceActions("workcells", row, "workcell", row.workcell_id), title: "", width: 180 },
  ];
  const taskColumns: TableColumnsType<TaskDefinition> = [
    { dataIndex: "display_name", key: "name", render: (value, row) => <span className="table-identity"><strong>{value}</strong><small>{taskKey(row)}</small></span>, title: "Task definition" },
    { dataIndex: "description", key: "description", title: "Description" },
    { dataIndex: "roles", key: "roles", render: (roles: TaskRole[]) => roles.map((role) => <Tag key={role.role}>{role.role}</Tag>), title: "Required roles" },
    { key: "actions", render: (_, row) => <Button href={`/workflow.html?source_task=${encodeURIComponent(taskKey(row))}`} size="small" type="link">Use in Workflow Studio</Button>, title: "", width: 190 },
  ];
  const runColumns: TableColumnsType<TaskRun> = [
    { dataIndex: "display_name", key: "name", render: (value, row) => <span className="table-identity"><strong>{value}</strong><small>{row.run_id}</small></span>, title: "Task Run" },
    { key: "recipe", render: (_, row) => <Text code>{row.task_definition_id}@{row.task_version}</Text>, title: "Recipe" },
    { dataIndex: "assignments", key: "assignments", render: (items: TaskRun["assignments"]) => `${items.length} assigned`, title: "Assets", width: 110 },
    { dataIndex: "state", key: "state", render: (value: string) => <Tag color={runColor(value)}>{value}</Tag>, title: "State", width: 110 },
    { key: "actions", render: (_, run) => {
      const transitions: Record<string, Array<[string, string]>> = { DRAFT: [["READY", "Mark ready"]], READY: [["ACTIVE", "Mark active (metadata only)"]], ACTIVE: [["COMPLETE", "Complete"], ["FAILED", "Fail"]] };
      return <Space wrap>{(transitions[run.state] || []).map(([state, label]) => <Button danger={state === "FAILED"} key={state} onClick={() => confirmTransition(run, state)} size="small">{label}</Button>)}<Button href={`/operator/#run/${encodeURIComponent(run.run_id)}`} size="small" type="link">Open Operator</Button></Space>;
    }, title: "Actions", width: 290 },
  ];

  const tabItems = [
    { key: "nodes", label: `Processing nodes (${configuration.processing_nodes.length})`, children: <RegistryTable actionLabel="Register node" columns={nodeColumns} data={configuration.processing_nodes} onAdd={() => openResource("node")} rowKey="node_id" /> },
    { key: "assets", label: `Assets (${configuration.assets.length})`, children: <RegistryTable actionLabel="Register asset" columns={assetColumns} data={configuration.assets} onAdd={() => openResource("asset")} rowKey="asset_id" /> },
    { key: "workcells", label: `Workcells (${configuration.workcells.length})`, children: <RegistryTable actionLabel="Create workcell" columns={workcellColumns} data={configuration.workcells} onAdd={() => openResource("workcell")} rowKey="workcell_id" /> },
    { key: "tasks", label: `Task definitions (${configuration.task_definitions.length})`, children: <RegistryTable actionLabel="Create observation Task" columns={taskColumns} data={configuration.task_definitions} onAdd={() => openResource("task")} rowKey={(row: TaskDefinition) => taskKey(row)} /> },
  ];

  return (
    <AppShell activeKey="setup" connection={connection} contextTitle="Operations setup" scopeDetail="Metadata writes only · fully audited" scopeTitle="Configuration boundary">
      <div className="page-stack configuration-page">
        <PageHeader actions={<Button icon={<ReloadOutlined spin={loading} />} loading={loading} onClick={() => void refresh()}>Reload status</Button>} description="Register observed equipment and assemble it into task-ready units." eyebrow="ADMIN / OPERATIONS TOPOLOGY" title="Operations setup" />
        <Alert description="This surface creates Node, Asset, Workcell, legacy observation Task, and Task Run metadata. ACTIVE is a metadata state, not robot execution. Executable versions are published in Workflow Studio." icon={<SafetyCertificateOutlined />} message="Configuration and execution remain isolated" showIcon type="info" />

        <Card className="summary-card" styles={{ body: { padding: 0 } }}>
          <Row>
            <SummaryStat icon={<CloudServerOutlined />} title="Processing nodes" value={configuration.processing_nodes.length} />
            <SummaryStat icon={<RobotOutlined />} title="Assets" value={configuration.assets.length} />
            <SummaryStat icon={<ApartmentOutlined />} title="Workcells" value={configuration.workcells.length} />
            <SummaryStat icon={<ThunderboltOutlined />} title="Task Runs" value={configuration.task_runs.length} />
          </Row>
        </Card>

        <Card className="discovery-card-ant" extra={<Tag color={discovery.robots.length + discovery.cameras.length ? "processing" : "success"}>{discovery.robots.length + discovery.cameras.length} unclaimed</Tag>} title={<Space><DeploymentUnitOutlined />Live discovery candidates</Space>}>
          <List
            dataSource={[
              ...discovery.robots.map((item) => ({ item, type: "robot" as const })),
              ...discovery.cameras.map((item) => ({ item, type: "camera" as const })),
            ]}
            locale={{ emptyText: <Empty description="All candidates are registered, or Central is waiting for heartbeats" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
            renderItem={({ item, type }) => (
              <List.Item actions={[<Button disabled={!item.node_id} key="register" onClick={() => claimCandidate(item, type)} size="small" type="primary">Register {type}</Button>]}>
                <List.Item.Meta description={`${item.agent_name} · ${type === "camera" ? `${(item as DiscoveryCamera).fresh ? "fresh" : "stale"} · ${(item as DiscoveryCamera).robot_id}` : `${(item as DiscoveryRobot).model} · ${item.status}`}`} title={type === "camera" ? (item as DiscoveryCamera).source_key : item.robot_id} />
                {!item.node_id ? <Tag color="warning">Assign agent to a node first</Tag> : <Text code>{item.node_id}</Text>}
              </List.Item>
            )}
          />
        </Card>

        <Card className="registry-tabs-card">
          <Tabs items={tabItems} />
        </Card>

        <Card extra={<Button icon={<PlusOutlined />} onClick={() => openResource("run")} type="primary">Create Task Run</Button>} title={<Space><ThunderboltOutlined />Task Run metadata contracts</Space>}>
          <Table columns={runColumns} dataSource={configuration.task_runs} pagination={{ hideOnSinglePage: true, pageSize: 10 }} rowKey="run_id" scroll={{ x: 980 }} />
        </Card>
      </div>

      <ResourceModal
        activeAssets={activeAssets}
        activeNodes={activeNodes}
        activeWorkcells={activeWorkcells}
        agentOptions={agentOptions}
        allowedAssets={allowedAssets}
        configuration={configuration}
        form={form}
        modalState={resourceModal}
        onCancel={closeResourceModal}
        onFinish={saveResource}
        selectedTask={selectedTask}
      />

      <Modal footer={null} onCancel={() => { setDecommissionTarget(null); reasonForm.resetFields(); }} open={Boolean(decommissionTarget)} title={`Decommission ${decommissionTarget?.id || "resource"}`}>
        <Form form={reasonForm} layout="vertical" onFinish={decommission}>
          <Alert message="This keeps the historical record and removes the resource from active assignment choices." showIcon type="warning" />
          <Form.Item label="Reason" name="reason" rules={[{ required: true, whitespace: true }]}><Input.TextArea rows={3} /></Form.Item>
          <Flex justify="end"><Button danger htmlType="submit">Decommission</Button></Flex>
        </Form>
      </Modal>

      <Modal footer={null} onCancel={() => { setFailureRun(null); reasonForm.resetFields(); }} open={Boolean(failureRun)} title={`Fail ${failureRun?.display_name || "Task Run"}`}>
        <Form form={reasonForm} layout="vertical" onFinish={(values) => failureRun && transitionRun(failureRun, "FAILED", values.reason.trim())}>
          <Form.Item label="Failure reason" name="reason" rules={[{ required: true, whitespace: true }]}><Input.TextArea rows={3} /></Form.Item>
          <Flex justify="end"><Button danger htmlType="submit">Mark failed</Button></Flex>
        </Form>
      </Modal>
    </AppShell>
  );
}

function SummaryStat({ icon, title, value }: { icon: React.ReactNode; title: string; value: number }) {
  return <Col className="summary-cell" xs={12} lg={6}><Statistic prefix={icon} title={title} value={value} /></Col>;
}

function RegistryTable<T extends object>({ actionLabel, columns, data, onAdd, rowKey }: {
  actionLabel: string;
  columns: TableColumnsType<T>;
  data: T[];
  onAdd: () => void;
  rowKey: string | ((row: T) => string);
}) {
  return (
    <div className="registry-table">
      <Flex justify="end"><Button icon={<PlusOutlined />} onClick={onAdd} type="primary">{actionLabel}</Button></Flex>
      <Table columns={columns} dataSource={data} pagination={{ hideOnSinglePage: true, pageSize: 8 }} rowKey={rowKey} scroll={{ x: 820 }} size="middle" />
    </div>
  );
}

function ResourceModal({
  activeAssets,
  activeNodes,
  activeWorkcells,
  agentOptions,
  allowedAssets,
  configuration,
  form,
  modalState,
  onCancel,
  onFinish,
  selectedTask,
}: {
  activeAssets: Asset[];
  activeNodes: ProcessingNode[];
  activeWorkcells: Workcell[];
  agentOptions: Array<{ label: string; value: string }>;
  allowedAssets: Set<string> | null;
  configuration: Configuration;
  form: ReturnType<typeof Form.useForm<ResourceFormValues>>[0];
  modalState: ResourceModalState | null;
  onCancel: () => void;
  onFinish: (values: ResourceFormValues) => Promise<void>;
  selectedTask: TaskDefinition | null;
}) {
  const kind = modalState?.kind;
  const editing = Boolean(modalState?.editing);
  const title = kind === "node" ? `${editing ? "Edit" : "Register"} Processing Node`
    : kind === "asset" ? `${editing ? "Edit" : "Register"} Asset`
      : kind === "workcell" ? `${editing ? "Edit" : "Create"} Workcell`
        : kind === "task" ? "Create observation Task version" : "Create Task Run";
  const binding = Form.useWatch("binding", form);

  return (
    <Modal destroyOnHidden maskClosable={false} okText="Save" onCancel={onCancel} onOk={() => form.submit()} open={Boolean(modalState)} title={title} width={kind === "task" || kind === "run" ? 760 : 620}>
      <Form<ResourceFormValues> form={form} layout="vertical" onFinish={onFinish} preserve={false} requiredMark={false}>
        {kind === "node" ? <>
          <Form.Item label="Node ID" name="node_id" rules={[{ pattern: ID_RULE, required: true }]}><Input disabled={editing} placeholder="node-omy-01" /></Form.Item>
          <Form.Item label="Display name" name="display_name" rules={[{ required: true }]}><Input placeholder="OMY Robot PC" /></Form.Item>
          <Form.Item label="Agents running on this PC" name="agent_ids"><Checkbox.Group className="vertical-checks" options={agentOptions} /></Form.Item>
        </> : null}

        {kind === "asset" ? <>
          {binding ? <Alert closable description={[binding.agent_id, binding.robot_id, binding.source_key].filter(Boolean).join(" / ")} message="Observed source binding" onClose={() => form.setFieldValue("binding", null)} showIcon type="info" /> : null}
          <Form.Item name="binding" noStyle><FormStoreValue /></Form.Item>
          <Row gutter={12}>
            <Col xs={24} sm={14}><Form.Item label="Asset ID" name="asset_id" rules={[{ pattern: ID_RULE, required: true }]}><Input disabled={editing} placeholder="omy-arm-01" /></Form.Item></Col>
            <Col xs={24} sm={10}><Form.Item label="Type" name="asset_type" rules={[{ required: true }]}><Select disabled={editing} options={ASSET_TYPES} /></Form.Item></Col>
          </Row>
          <Form.Item label="Display name" name="display_name" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item label="Processing Node" name="node_id"><Select allowClear options={activeNodes.map((node) => ({ label: `${node.display_name} · ${node.node_id}`, value: node.node_id }))} placeholder="Unassigned" /></Form.Item>
          <Form.Item extra="Comma separated" label="Capabilities" name="capabilities"><Input placeholder="telemetry, dual_arm" /></Form.Item>
        </> : null}

        {kind === "workcell" ? <>
          <Form.Item label="Workcell ID" name="workcell_id" rules={[{ pattern: ID_RULE, required: true }]}><Input disabled={editing} placeholder="weld-cell-01" /></Form.Item>
          <Form.Item label="Display name" name="display_name" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item label="Description" name="description"><Input.TextArea rows={2} /></Form.Item>
          <Form.Item label="Included assets" name="asset_ids"><Checkbox.Group className="vertical-checks" options={activeAssets.map((asset) => ({ label: `${asset.display_name} · ${asset.asset_type}`, value: asset.asset_id }))} /></Form.Item>
        </> : null}

        {kind === "task" ? <>
          <Row gutter={12}>
            <Col xs={24} sm={16}><Form.Item label="Task ID" name="task_definition_id" rules={[{ pattern: ID_RULE, required: true }]}><Input onChange={(event) => {
              const versions = configuration.task_definitions.filter((task) => task.task_definition_id === event.target.value).map((task) => task.version);
              form.setFieldValue("version", versions.length ? Math.max(...versions) + 1 : 1);
            }} /></Form.Item></Col>
            <Col xs={24} sm={8}><Form.Item label="Version" name="version" rules={[{ required: true }]}><InputNumber min={1} style={{ width: "100%" }} /></Form.Item></Col>
          </Row>
          <Form.Item label="Display name" name="display_name" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item label="Description" name="description"><Input.TextArea rows={2} /></Form.Item>
          <Form.List name="roles">
            {(fields, { add, remove }) => <Space className="role-form-list" direction="vertical" size={10}>
              {fields.map((field) => <Row gutter={8} key={field.key}>
                <Col xs={24} sm={9}><Form.Item label="Role ID" name={[field.name, "role"]} rules={[{ pattern: ID_RULE, required: true }]}><Input /></Form.Item></Col>
                <Col xs={12} sm={6}><Form.Item label="Asset type" name={[field.name, "asset_type"]}><Select options={ASSET_TYPES} /></Form.Item></Col>
                <Col xs={12} sm={6}><Form.Item label="Access" name={[field.name, "access_mode"]}><Select options={ACCESS_MODES} /></Form.Item></Col>
                <Col xs={24} sm={3}><Form.Item label=" "><Button danger disabled={fields.length === 1} onClick={() => remove(field.name)}>Remove</Button></Form.Item></Col>
              </Row>)}
              <Button block icon={<PlusOutlined />} onClick={() => add({ access_mode: "exclusive", asset_type: "robot", role: `role_${fields.length + 1}` })}>Add role</Button>
            </Space>}
          </Form.List>
        </> : null}

        {kind === "run" ? <>
          <Row gutter={12}>
            <Col xs={24} sm={16}><Form.Item label="Run ID" name="run_id" rules={[{ pattern: ID_RULE, required: true }]}><Input /></Form.Item></Col>
            <Col xs={24} sm={8}><Form.Item label="Initial state" name="state"><Select options={[{ label: "DRAFT", value: "DRAFT" }, { label: "READY · reserve", value: "READY" }]} /></Form.Item></Col>
          </Row>
          <Form.Item label="Display name" name="display_name" rules={[{ required: true }]}><Input /></Form.Item>
          <Row gutter={12}>
            <Col xs={24} sm={14}><Form.Item label="Task version" name="task_definition" rules={[{ required: true }]}><Select onChange={() => form.setFieldValue("assignments", {})} options={configuration.task_definitions.map((task) => ({ label: `${task.display_name} · v${task.version}`, value: taskKey(task) }))} /></Form.Item></Col>
            <Col xs={24} sm={10}><Form.Item label="Workcell" name="workcell_id"><Select allowClear onChange={() => form.setFieldValue("assignments", {})} options={activeWorkcells.map((workcell) => ({ label: workcell.display_name, value: workcell.workcell_id }))} placeholder="All assets" /></Form.Item></Col>
          </Row>
          {selectedTask ? <Card className="assignment-card" size="small" title="Assign assets by role">
            {selectedTask.roles.map((role) => {
              const choices = activeAssets.filter((asset) => asset.asset_type === role.asset_type && (!allowedAssets || allowedAssets.has(asset.asset_id)));
              return <Form.Item extra={`${role.asset_type} · ${role.access_mode}`} key={role.role} label={role.role} name={["assignments", role.role]}><Select allowClear options={choices.map((asset) => ({ label: `${asset.display_name} · ${asset.asset_id}`, value: asset.asset_id }))} placeholder="Not assigned" /></Form.Item>;
            })}
          </Card> : <Alert message="Select a Task version to assign assets." type="info" />}
        </> : null}
      </Form>
    </Modal>
  );
}

function FormStoreValue(_props: { onChange?: (value: unknown) => void; value?: unknown }) {
  return null;
}
