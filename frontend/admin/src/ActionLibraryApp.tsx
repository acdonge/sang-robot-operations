import {
  CheckCircleOutlined,
  CodeOutlined,
  EditOutlined,
  ExperimentOutlined,
  FileProtectOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SendOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App,
  AutoComplete,
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
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Steps,
  Switch,
  Table,
  Tag,
  Typography,
  Upload,
  type TableColumnsType,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import { errorMessage, getJson, requestJson } from "./api";
import AppShell from "./components/AppShell";
import PageHeader from "./components/PageHeader";
import type {
  ActionLibraryEntry,
  SimulationAdapterStatus,
  SkillDefinition,
  SkillParameterProperty,
} from "./operations-types";
import type { ConnectionState } from "./types";
import { formatBytes, formatSessionTime } from "./utils";

const { Dragger } = Upload;
const { Paragraph, Text, Title } = Typography;

const API = {
  adapters: "/api/v1/workflow-authoring/simulation-adapters",
  library: "/api/v1/workflow-authoring/action-library",
  skills: "/api/v1/workflow-authoring/skill-definitions",
};

const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const MAX_SOURCE_BYTES = 256 * 1024;

interface ActionFormValues {
  action_id: string;
  asset_type: string;
  capability: string;
  default_timeout_s: number;
  description: string;
  entrypoint: string;
  robot_label: string;
  robot_type: string;
  title: string;
}

interface SelectedSource {
  name: string;
  runtime: "UNITY_CSHARP" | "ROS2_PYTHON";
  size: number;
  text: string;
}

function requestId(prefix: string) {
  const suffix = crypto.randomUUID
    ? crypto.randomUUID()
    : [...crypto.getRandomValues(new Uint8Array(16))]
      .map((item) => item.toString(16).padStart(2, "0"))
      .join("");
  return `${prefix}-${suffix}`;
}

function gateColor(state: ActionLibraryEntry["source_validation_state"]) {
  if (state === "PASS") return "success";
  if (state === "FAIL") return "error";
  return "default";
}

function runtimeLabel(runtime: ActionLibraryEntry["script"]["runtime"]) {
  return runtime === "UNITY_CSHARP" ? "Unity C#" : "ROS 2 Python";
}

function safeEditable(name: string, property: SkillParameterProperty) {
  if (property.readOnly || property.const !== null || name === "operation") return false;
  if (property.type === "boolean" || property.enum) return true;
  return ["number", "integer"].includes(property.type)
    && property.minimum !== null
    && property.maximum !== null;
}

function initialValue(property: SkillParameterProperty) {
  if (property.const !== null) return property.const;
  if (property.default !== null) return property.default;
  if (property.enum?.length) return property.enum[0];
  if (property.type === "boolean") return false;
  return undefined;
}

function inputRule(property: SkillParameterProperty) {
  if (property.enum) return property.enum.map(String).join(" · ");
  if (["number", "integer"].includes(property.type)) {
    return `${property.minimum ?? "-∞"} to ${property.maximum ?? "+∞"}${property.unit ? ` ${property.unit}` : ""}`;
  }
  return property.type;
}

function FixedInput({
  onChange,
  property,
  value,
}: {
  onChange: (value: unknown) => void;
  property: SkillParameterProperty;
  value: unknown;
}) {
  if (property.enum) {
    return <Select onChange={onChange} options={property.enum.map((item) => ({ label: String(item), value: item }))} value={value} />;
  }
  if (property.type === "number" || property.type === "integer") {
    return (
      <InputNumber
        max={property.maximum ?? undefined}
        min={property.minimum ?? undefined}
        onChange={onChange}
        precision={property.type === "integer" ? 0 : undefined}
        step={property.multipleOf ?? (property.type === "integer" ? 1 : 0.1)}
        value={typeof value === "number" ? value : undefined}
      />
    );
  }
  if (property.type === "boolean") {
    return <Switch checked={Boolean(value)} onChange={onChange} />;
  }
  return <Input onChange={(event) => onChange(event.target.value)} value={typeof value === "string" ? value : ""} />;
}

function ActionSourceSummary({ entry, selected }: { entry: ActionLibraryEntry | null; selected: SelectedSource | null }) {
  const runtime = selected?.runtime || entry?.script.runtime;
  const fileName = selected?.name || entry?.script.file_name || entry?.script.script_ref?.split("/").at(-1);
  const size = selected?.size ?? entry?.script.size_bytes;
  if (!runtime || !fileName) return <Empty description="Select one .cs or .py file" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  return (
    <div className="action-source-summary">
      <span className="action-file-icon"><CodeOutlined /></span>
      <div>
        <strong>{fileName}</strong>
        <Text type="secondary">{runtimeLabel(runtime)}{size ? ` · ${formatBytes(size)}` : ""}</Text>
      </div>
      {entry && !selected ? <Tag>Existing source retained</Tag> : <Tag color="blue">New source</Tag>}
    </div>
  );
}

export default function ActionLibraryApp() {
  const { message, modal } = App.useApp();
  const [entries, setEntries] = useState<ActionLibraryEntry[]>([]);
  const [skills, setSkills] = useState<SkillDefinition[]>([]);
  const [adapters, setAdapters] = useState<SimulationAdapterStatus[]>([]);
  const [connection, setConnection] = useState<ConnectionState>({ detail: "Loading Action registry", kind: "connecting", label: "Connecting" });
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorStep, setEditorStep] = useState(0);
  const [editing, setEditing] = useState<ActionLibraryEntry | null>(null);
  const [selectedSource, setSelectedSource] = useState<SelectedSource | null>(null);
  const [editableInputs, setEditableInputs] = useState<Record<string, boolean>>({});
  const [fixedInputs, setFixedInputs] = useState<Record<string, unknown>>({});
  const [simulationTarget, setSimulationTarget] = useState<ActionLibraryEntry | null>(null);
  const [simulationAdapterId, setSimulationAdapterId] = useState<string | null>(null);
  const [form] = Form.useForm<ActionFormValues>();

  async function refresh() {
    setLoading(true);
    try {
      const [nextEntries, nextSkills, nextAdapters] = await Promise.all([
        getJson<ActionLibraryEntry[]>(API.library),
        getJson<SkillDefinition[]>(API.skills),
        getJson<SimulationAdapterStatus[]>(API.adapters),
      ]);
      setEntries(nextEntries);
      setSkills(nextSkills);
      setAdapters(nextAdapters);
      setConnection({ detail: `${nextEntries.length} registered Action version(s)`, kind: "live", label: "Central live" });
    } catch (error) {
      setConnection({ detail: errorMessage(error), kind: "error", label: "Central unavailable" });
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void refresh(); }, []);

  const robotOptions = useMemo(() => {
    const labels = new Map(entries.map((entry) => [entry.robot_type, entry.robot_label]));
    return [...labels].sort().map(([value, label]) => ({ label: `${label} · ${value}`, value }));
  }, [entries]);
  const selectedCapability = Form.useWatch("capability", form);
  const selectedSkill = skills.find((skill) => skill.capability === selectedCapability) || null;
  const publishedCount = entries.filter((entry) => entry.state === "PUBLISHED").length;
  const draftCount = entries.filter((entry) => entry.state === "DRAFT").length;
  const blockedCount = entries.filter((entry) => entry.state === "DRAFT" && (entry.source_validation_state === "FAIL" || entry.simulation_validation_state === "FAIL")).length;

  function configureInputs(skill: SkillDefinition | null, entry: ActionLibraryEntry | null) {
    if (!skill) {
      setEditableInputs({});
      setFixedInputs({});
      return;
    }
    const editable: Record<string, boolean> = {};
    const fixed: Record<string, unknown> = {};
    for (const [name, property] of Object.entries(skill.parameter_schema.properties)) {
      editable[name] = entry
        ? entry.editable_parameters.includes(name)
        : safeEditable(name, property) && !skill.parameter_schema.required.includes(name);
      fixed[name] = entry?.fixed_parameters[name] ?? initialValue(property);
    }
    setEditableInputs(editable);
    setFixedInputs(fixed);
  }

  function openNew() {
    setEditing(null);
    setSelectedSource(null);
    setEditorStep(0);
    setEditableInputs({});
    setFixedInputs({});
    form.resetFields();
    form.setFieldsValue({ asset_type: "robot", default_timeout_s: 120 });
    setEditorOpen(true);
  }

  function openEdit(entry: ActionLibraryEntry) {
    setEditing(entry);
    setSelectedSource(null);
    setEditorStep(0);
    form.setFieldsValue({
      action_id: entry.action_id,
      asset_type: entry.asset_type,
      capability: entry.capability,
      default_timeout_s: entry.default_timeout_s,
      description: entry.description,
      entrypoint: entry.script.entrypoint,
      robot_label: entry.robot_label,
      robot_type: entry.robot_type,
      title: entry.title,
    });
    configureInputs(skills.find((skill) => skill.capability === entry.capability) || null, entry);
    setEditorOpen(true);
  }

  async function readSource(file: File) {
    if (file.size > MAX_SOURCE_BYTES) {
      void message.error("Action source must be 256 KiB or smaller.");
      return Upload.LIST_IGNORE;
    }
    const suffix = file.name.toLowerCase().split(".").at(-1);
    if (suffix !== "cs" && suffix !== "py") {
      void message.error("Choose one C# (.cs) or Python (.py) file.");
      return Upload.LIST_IGNORE;
    }
    const text = await file.text();
    const runtime = suffix === "cs" ? "UNITY_CSHARP" : "ROS2_PYTHON";
    const inferred = suffix === "cs"
      ? text.match(/\bclass\s+([A-Za-z_][A-Za-z0-9_]*)/)?.[1]
      : text.match(/\bdef\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(/)?.[1];
    setSelectedSource({ name: file.name, runtime, size: file.size, text });
    if (inferred) form.setFieldValue("entrypoint", inferred);
    return false;
  }

  async function nextEditorStep() {
    try {
      if (editorStep === 0) {
        await form.validateFields(["action_id", "robot_type", "robot_label", "title", "description", "asset_type"]);
      }
      if (editorStep === 1) {
        await form.validateFields(["entrypoint"]);
        if (!editing && !selectedSource) throw new Error("Choose one C# or Python file.");
      }
      if (editorStep === 2) {
        await form.validateFields(["capability", "default_timeout_s"]);
        if (!selectedSkill) throw new Error("Choose a Skill capability.");
        const missing = selectedSkill.parameter_schema.required.filter((name) => {
          if (editableInputs[name]) return false;
          const value = fixedInputs[name];
          return value === undefined || value === null || value === "";
        });
        if (missing.length) throw new Error(`Set required inputs: ${missing.join(", ")}`);
      }
      setEditorStep((step) => Math.min(3, step + 1));
    } catch (error) {
      if (error instanceof Error && !("errorFields" in error)) void message.error(error.message);
    }
  }

  async function saveDraft() {
    const values = await form.validateFields();
    const skill = skills.find((item) => item.capability === values.capability);
    if (!skill) throw new Error("Selected Skill is unavailable.");
    const fixed: Record<string, unknown> = {};
    const editable: string[] = [];
    for (const [name] of Object.entries(skill.parameter_schema.properties)) {
      if (editableInputs[name]) editable.push(name);
      else if (fixedInputs[name] !== undefined && fixedInputs[name] !== null && fixedInputs[name] !== "") fixed[name] = fixedInputs[name];
    }
    const retainedRuntime = editing?.script.runtime;
    const retainedName = editing?.script.file_name || editing?.script.script_ref?.split("/").at(-1);
    const runtime = selectedSource?.runtime || retainedRuntime;
    const sourceFileName = selectedSource?.name || retainedName;
    if (!runtime || !sourceFileName) throw new Error("Action source identity is missing.");
    const body = {
      access_mode: "exclusive",
      action_id: values.action_id,
      asset_type: values.asset_type,
      capability: values.capability,
      default_timeout_s: values.default_timeout_s,
      description: values.description,
      editable_parameters: editable,
      entrypoint: values.entrypoint,
      fixed_parameters: fixed,
      request_id: requestId(editing ? "action-update" : "action-create"),
      robot_label: values.robot_label,
      robot_type: values.robot_type,
      runtime,
      source_code: selectedSource?.text,
      source_file_name: sourceFileName,
      title: values.title,
      ...(editing ? { expected_revision: editing.revision } : {}),
    };
    setBusyId(editing?.draft_id || "new");
    try {
      await requestJson(
        editing ? "PUT" : "POST",
        editing
          ? `${API.library}/drafts/${encodeURIComponent(editing.draft_id || "")}`
          : `${API.library}/drafts`,
        body,
      );
      setEditorOpen(false);
      void message.success(editing ? "Action Draft updated. Validation was reset." : "Action Draft created.");
      await refresh();
    } finally {
      setBusyId(null);
    }
  }

  async function runSourceGate(entry: ActionLibraryEntry) {
    if (!entry.draft_id) return;
    setBusyId(entry.draft_id);
    try {
      const result = await requestJson<ActionLibraryEntry>("POST", `${API.library}/drafts/${encodeURIComponent(entry.draft_id)}/source-validation`, {});
      if (result.source_validation_state === "PASS") void message.success("Source Gate passed without executing the uploaded code.");
      else void message.error(result.source_validation?.detail || "Source Gate failed.");
      await refresh();
    } catch (error) {
      void message.error(errorMessage(error));
    } finally {
      setBusyId(null);
    }
  }

  function openSimulationGate(entry: ActionLibraryEntry) {
    setSimulationTarget(entry);
    setSimulationAdapterId(adapters[0]?.adapter_id || null);
  }

  async function runSimulationGate() {
    if (!simulationTarget?.draft_id || !simulationAdapterId) return;
    setBusyId(simulationTarget.draft_id);
    try {
      const result = await requestJson<ActionLibraryEntry>(
        "POST",
        `${API.library}/drafts/${encodeURIComponent(simulationTarget.draft_id)}/simulation-validation`,
        { adapter_id: simulationAdapterId },
      );
      setSimulationTarget(null);
      if (result.simulation_validation_state === "PASS") void message.success("Simulation Gate passed with immutable Run evidence.");
      else void message.warning(result.simulation_validation?.detail || "Simulation Gate is blocked.");
      await refresh();
    } catch (error) {
      void message.error(errorMessage(error));
    } finally {
      setBusyId(null);
    }
  }

  function publish(entry: ActionLibraryEntry) {
    if (!entry.draft_id) return;
    modal.confirm({
      content: "Publishing freezes this source digest and Action contract. Future changes require the next version.",
      okText: "Publish immutable version",
      onOk: async () => {
        setBusyId(entry.draft_id);
        try {
          await requestJson(
            "POST",
            `${API.library}/drafts/${encodeURIComponent(entry.draft_id || "")}/publish`,
            { expected_revision: entry.revision, request_id: requestId("action-publish") },
          );
          void message.success(`${entry.action_ref} published to Workflow Studio.`);
          await refresh();
        } finally {
          setBusyId(null);
        }
      },
      title: `Publish ${entry.action_ref}?`,
    });
  }

  const columns: TableColumnsType<ActionLibraryEntry> = [
    {
      key: "action",
      render: (_, entry) => (
        <span className="table-identity">
          <strong>{entry.title}</strong>
          <small>{entry.action_ref} · {entry.origin === "BUILTIN" ? "built-in" : `rev ${entry.revision}`}</small>
        </span>
      ),
      title: "Action",
      width: 300,
    },
    {
      key: "robot",
      render: (_, entry) => <span className="table-identity"><strong>{entry.robot_label}</strong><small>{entry.robot_type}</small></span>,
      title: "Robot",
      width: 190,
    },
    {
      key: "source",
      render: (_, entry) => (
        <span className="table-identity">
          <strong>{runtimeLabel(entry.script.runtime)}</strong>
          <small>{entry.script.entrypoint} · {entry.script.sha256.slice(0, 10)}…</small>
        </span>
      ),
      title: "Source identity",
      width: 245,
    },
    {
      key: "gates",
      render: (_, entry) => (
        <Space size={4} wrap>
          <Tag color={gateColor(entry.source_validation_state)}>Source {entry.source_validation_state}</Tag>
          <Tag color={gateColor(entry.simulation_validation_state)}>Simulation {entry.simulation_validation_state}</Tag>
        </Space>
      ),
      title: "Gates",
      width: 230,
    },
    {
      key: "state",
      render: (_, entry) => <Tag color={entry.state === "PUBLISHED" ? "success" : "processing"}>{entry.state}</Tag>,
      title: "State",
      width: 110,
    },
    {
      fixed: "right",
      key: "actions",
      render: (_, entry) => entry.origin === "LIBRARY" && entry.state === "DRAFT" ? (
        <Space size={4} wrap>
          <Button icon={<EditOutlined />} onClick={() => openEdit(entry)} size="small">Edit</Button>
          <Button loading={busyId === entry.draft_id} onClick={() => void runSourceGate(entry)} size="small">Source Gate</Button>
          <Button disabled={entry.source_validation_state !== "PASS"} onClick={() => openSimulationGate(entry)} size="small">Simulation Gate</Button>
          <Button
            disabled={entry.source_validation_state !== "PASS" || entry.simulation_validation_state !== "PASS"}
            icon={<SendOutlined />}
            onClick={() => publish(entry)}
            size="small"
            type="primary"
          >Publish</Button>
        </Space>
      ) : <Text type="secondary">Immutable</Text>,
      title: "",
      width: 390,
    },
  ];

  const selectedProperties = selectedSkill?.parameter_schema.properties || {};
  const reviewValues = form.getFieldsValue();

  return (
    <AppShell activeKey="actions" connection={connection} contextTitle="Action Library" scopeDetail="Uploaded source is stored, scanned, and simulation-gated; Central never executes it" scopeTitle="Admin build boundary">
      <div className="page-stack action-library-page">
        <PageHeader
          actions={<Space wrap><Button icon={<PlusOutlined />} onClick={openNew} type="primary">New Action</Button><Button icon={<ReloadOutlined spin={loading} />} loading={loading} onClick={() => void refresh()}>Reload</Button></Space>}
          description="Register one versioned C# or Python file as one reusable robot Action. Only simulation-qualified publications reach Workflow Studio."
          eyebrow="ADMIN / IMMUTABLE ROBOT BEHAVIOR"
          title="Action Library"
        />

        <Alert
          description="Source validation is static and never runs uploaded code. Simulation PASS additionally requires the exact script digest to be installed in a trusted output-disconnected build and observed in a completed Run."
          icon={<SafetyCertificateOutlined />}
          message="Unvalidated source cannot be published or sent to a robot"
          showIcon
          type="info"
        />

        <Card className="summary-card" styles={{ body: { padding: 0 } }}>
          <Row>
            <Col className="summary-cell" xs={12} lg={6}><Statistic prefix={<FileProtectOutlined />} title="Published" value={publishedCount} /></Col>
            <Col className="summary-cell" xs={12} lg={6}><Statistic prefix={<CodeOutlined />} title="Drafts" value={draftCount} /></Col>
            <Col className="summary-cell" xs={12} lg={6}><Statistic prefix={<ExperimentOutlined />} title="Ready adapters" value={adapters.filter((adapter) => adapter.ready).length} /></Col>
            <Col className="summary-cell" xs={12} lg={6}><Statistic prefix={<SafetyCertificateOutlined />} title="Blocked" value={blockedCount} /></Col>
          </Row>
        </Card>

        <Card className="registry-card" title="Registered Action versions">
          <Table columns={columns} dataSource={entries} loading={loading} pagination={{ pageSize: 10, hideOnSinglePage: true }} rowKey={(entry) => `${entry.origin}:${entry.action_ref}:${entry.draft_id || "published"}`} scroll={{ x: 1480 }} />
        </Card>
      </div>

      <Modal
        destroyOnHidden
        footer={null}
        onCancel={() => setEditorOpen(false)}
        open={editorOpen}
        title={editing ? `Edit ${editing.action_ref}` : "Register a new robot Action"}
        width={920}
      >
        <Steps className="action-editor-steps" current={editorStep} items={[
          { title: "Robot" },
          { title: "Script" },
          { title: "Allowed inputs" },
          { title: "Review" },
        ]} />
        <Form form={form} layout="vertical" requiredMark={false}>
          <div hidden={editorStep !== 0}>
            <Row gutter={16}>
              <Col xs={24} md={12}><Form.Item label="Robot type" name="robot_type" rules={[{ pattern: ID_PATTERN, required: true }]}><AutoComplete onSelect={(value) => { const match = entries.find((entry) => entry.robot_type === value); if (match) form.setFieldValue("robot_label", match.robot_label); }} options={robotOptions} placeholder="omy-f3m" /></Form.Item></Col>
              <Col xs={24} md={12}><Form.Item label="Robot display name" name="robot_label" rules={[{ required: true }]}><Input placeholder="OMY F3M" /></Form.Item></Col>
              <Col xs={24} md={12}><Form.Item extra="Stable ID; publishing creates @1, @2, ..." label="Action ID" name="action_id" rules={[{ pattern: ID_PATTERN, required: true }]}><Input disabled={Boolean(editing)} placeholder="omy-place-on-waam-table" /></Form.Item></Col>
              <Col xs={24} md={12}><Form.Item label="Asset type" name="asset_type" rules={[{ required: true }]}><Select options={[{ label: "Robot arm", value: "robot" }, { label: "Mobile robot", value: "mobile_robot" }]} /></Form.Item></Col>
            </Row>
            <Form.Item label="Action name" name="title" rules={[{ required: true }]}><Input placeholder="Place material on WAAM table" /></Form.Item>
            <Form.Item label="Description" name="description" rules={[{ required: true }]}><Input.TextArea placeholder="What this Action does in one sentence" rows={3} /></Form.Item>
          </div>

          <div hidden={editorStep !== 1}>
            <Dragger accept=".cs,.py" beforeUpload={(file) => { void readSource(file); return false; }} fileList={[]} maxCount={1} multiple={false} showUploadList={false}>
              <p className="ant-upload-drag-icon"><UploadOutlined /></p>
              <p className="ant-upload-text">Choose one C# or Python Action file</p>
              <p className="ant-upload-hint">Maximum 256 KiB. Central stores the source by SHA-256 and never executes it.</p>
            </Dragger>
            <ActionSourceSummary entry={editing} selected={selectedSource} />
            <Form.Item extra="C# class or Python function implementing the Action contract" label="Entrypoint" name="entrypoint" rules={[{ pattern: /^[A-Za-z_][A-Za-z0-9_.]*$/, required: true }]}><Input placeholder="OmyPlaceOnWaamTableAction" /></Form.Item>
          </div>

          <div hidden={editorStep !== 2}>
            <Row gutter={16}>
              <Col xs={24} md={16}><Form.Item label="Skill contract" name="capability" rules={[{ required: true }]}><Select onChange={(value) => configureInputs(skills.find((skill) => skill.capability === value) || null, null)} options={skills.filter((skill) => skill.execution_modes.includes("SIMULATION")).map((skill) => ({ label: `${skill.title} · ${skill.capability}`, value: skill.capability }))} /></Form.Item></Col>
              <Col xs={24} md={8}><Form.Item label="Timeout" name="default_timeout_s" rules={[{ required: true }]}><InputNumber addonAfter="s" max={86400} min={1} /></Form.Item></Col>
            </Row>
            {selectedSkill ? <div className="action-input-contract">
              <div className="action-input-heading"><div><strong>Allowed inputs</strong><Text type="secondary">Bounds come from {selectedSkill.capability} and cannot be widened here.</Text></div></div>
              {Object.entries(selectedProperties).map(([name, property]) => {
                const canEdit = safeEditable(name, property);
                const editable = Boolean(editableInputs[name]);
                return <div className="action-input-row" key={name}>
                  <div><strong>{property.title}</strong><code>{name}</code><Text type="secondary">{property.description || inputRule(property)}</Text></div>
                  <div className="action-input-value">{editable ? <Tag color="blue">Set when building a Task</Tag> : <FixedInput onChange={(value) => setFixedInputs((current) => ({ ...current, [name]: value }))} property={property} value={fixedInputs[name]} />}</div>
                  <Checkbox checked={editable} disabled={!canEdit} onChange={(event) => setEditableInputs((current) => ({ ...current, [name]: event.target.checked }))}>Editable in Task</Checkbox>
                </div>;
              })}
            </div> : <Empty description="Choose a Skill contract to configure inputs" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
          </div>

          <div hidden={editorStep !== 3}>
            <Alert message="Creating a Draft does not execute or publish this source" showIcon type="info" />
            <Descriptions bordered column={2} size="small">
              <Descriptions.Item label="Robot">{reviewValues.robot_label || "--"}</Descriptions.Item>
              <Descriptions.Item label="Action">{reviewValues.action_id || "--"}</Descriptions.Item>
              <Descriptions.Item label="Source">{selectedSource?.name || editing?.script.file_name || "Existing immutable source"}</Descriptions.Item>
              <Descriptions.Item label="Entrypoint">{reviewValues.entrypoint || "--"}</Descriptions.Item>
              <Descriptions.Item label="Skill">{reviewValues.capability || "--"}</Descriptions.Item>
              <Descriptions.Item label="Workflow inputs">{Object.entries(editableInputs).filter(([, value]) => value).map(([name]) => name).join(", ") || "None"}</Descriptions.Item>
            </Descriptions>
            <div className="action-publish-path">
              <span><CodeOutlined /><strong>Draft</strong><small>Stored only</small></span>
              <span><FileProtectOutlined /><strong>Source Gate</strong><small>No execution</small></span>
              <span><ExperimentOutlined /><strong>Simulation Gate</strong><small>Exact build + Run</small></span>
              <span><CheckCircleOutlined /><strong>Publish</strong><small>Immutable</small></span>
            </div>
          </div>
        </Form>
        <Flex className="action-editor-footer" justify="space-between">
          <Button disabled={editorStep === 0} onClick={() => setEditorStep((step) => Math.max(0, step - 1))}>Back</Button>
          {editorStep < 3 ? <Button onClick={() => void nextEditorStep()} type="primary">Continue</Button> : <Button icon={<FileProtectOutlined />} loading={busyId === (editing?.draft_id || "new")} onClick={() => void saveDraft()} type="primary">{editing ? "Update Draft" : "Create Draft"}</Button>}
        </Flex>
      </Modal>

      <Modal
        okButtonProps={{ disabled: !simulationAdapterId, loading: busyId === simulationTarget?.draft_id }}
        okText="Check simulation evidence"
        onCancel={() => setSimulationTarget(null)}
        onOk={() => void runSimulationGate()}
        open={Boolean(simulationTarget)}
        title="Simulation Gate"
      >
        <Space direction="vertical" size={16} style={{ width: "100%" }}>
          <Alert description="This checks for an exact installed source digest and a completed output-disconnected Run containing the same Action contract. It does not execute uploaded source in Central." message="Trusted build evidence required" showIcon type="warning" />
          <Select onChange={setSimulationAdapterId} options={adapters.map((adapter) => ({ label: `${adapter.adapter_id} · ${adapter.ready ? "ready" : adapter.detail}`, value: adapter.adapter_id }))} placeholder="Select simulation adapter" value={simulationAdapterId} />
          {simulationTarget?.simulation_validation?.detail ? <Paragraph type="secondary">Last result: {simulationTarget.simulation_validation.detail}</Paragraph> : null}
        </Space>
      </Modal>
    </AppShell>
  );
}
