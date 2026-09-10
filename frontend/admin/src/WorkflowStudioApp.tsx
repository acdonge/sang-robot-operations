import {
  App as AntApp,
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Flex,
  Form,
  Input,
  InputNumber,
  List,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  type TableColumnsType,
} from "antd";
import {
  ApartmentOutlined,
  BranchesOutlined,
  CheckCircleOutlined,
  CopyOutlined,
  ExperimentOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  RocketOutlined,
  SafetyCertificateOutlined,
  SaveOutlined,
  SendOutlined,
  StopOutlined,
  SwapOutlined,
  UndoOutlined,
  VideoCameraOutlined,
} from "@ant-design/icons";
import { startTransition, useEffect, useRef, useState } from "react";
import { errorMessage, getJson, requestJson } from "./api";
import AppShell from "./components/AppShell";
import PageHeader from "./components/PageHeader";
import type {
  CommissioningCandidate,
  Configuration,
  RobotActionDefinition,
  SimulationAdapterStatus,
  SimulationValidationProfile,
  SkillDefinition,
  SkillParameterProperty,
  TaskDefinition,
  WorkflowDraft,
  WorkflowDraftRole,
  WorkflowCompatibility,
  WorkflowPublication,
  WorkflowSimulationRun,
  WorkflowSimulationControl,
  WorkflowStep,
  WorkflowValidation,
  UnityRuntimeStatus,
} from "./operations-types";
import type { ConnectionState } from "./types";
import {
  latestSimulationProfile,
  selectPublicationSimulation,
  simulationAdapterKey,
} from "./workflow-simulation-selection";

const { Paragraph, Text, Title } = Typography;

const API = Object.freeze({
  adapters: "/api/v1/workflow-authoring/simulation-adapters",
  configuration: "/api/v1/configuration",
  drafts: "/api/v1/workflow-authoring/drafts",
  publications: "/api/v1/workflow-authoring/publications",
  profiles: "/api/v1/workflow-authoring/simulation-profiles",
  candidates: "/api/v1/workflow-authoring/commissioning-candidates",
  runs: "/api/v1/workflow-authoring/simulation-runs",
  runtime: "/api/v1/workflow-authoring/runtime",
  runtimePreview: "/api/v1/workflow-authoring/runtime/preview.mjpg",
  runtimeStart: "/api/v1/workflow-authoring/runtime/start",
  runtimeStop: "/api/v1/workflow-authoring/runtime/stop",
  actions: "/api/v1/workflow-authoring/action-definitions",
  skills: "/api/v1/workflow-authoring/skill-definitions",
});

const EMPTY_CONFIGURATION: Configuration = {
  assets: [], audit: [], processing_nodes: [], task_definitions: [], task_runs: [], workcells: [],
};
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const initialSourceTask = new URLSearchParams(window.location.search).get("source_task");

interface NewDraftValues {
  description?: string;
  display_name: string;
  task_definition_id: string;
  workflow_id?: string;
}

interface CloneValues {
  display_name?: string;
  mode: "new_task" | "new_version";
  source_task: string;
  target_task_definition_id?: string;
  target_workflow_id?: string;
}

interface ActionValues {
  action_ref: string;
  parameters?: Record<string, unknown>;
  robot_type: string;
}

interface ValidationProfileValues {
  description?: string;
  display_name: string;
  max_duration_s: number;
  profile_id: string;
  require_reset_acknowledged: boolean;
  required_successful_runs: number;
}

const EMPTY_RUNTIME: UnityRuntimeStatus = {
  configured: false,
  heartbeat: null,
  last_exit_code: null,
  physical_output_connected: false,
  pid: null,
  preview: null,
  preview_available: false,
  ready: false,
  running: false,
  scenario_id: null,
  schema_version: 1,
  started_at: null,
};

function parameterExtra(property: SkillParameterProperty) {
  const range = property.minimum !== null || property.maximum !== null
    ? `Range ${property.minimum ?? "-inf"} to ${property.maximum ?? "+inf"}${property.unit ? ` ${property.unit}` : ""}.`
    : "";
  return [property.description, range].filter(Boolean).join(" ");
}

function ActionParameterFields({ action, definition }: {
  action: RobotActionDefinition;
  definition: SkillDefinition;
}) {
  return <div className="workflow-skill-fields">
    {Object.entries(definition.parameter_schema.properties)
      .filter(([name]) => action.editable_parameters.includes(name))
      .map(([name, property]) => {
      const required = definition.parameter_schema.required.includes(name);
      const locked = property.readOnly || property.const !== null;
      const rules = [{ required, message: `${property.title} is required.` }];
      if (property.type === "number" || property.type === "integer") {
        return <Form.Item extra={parameterExtra(property)} key={name} label={property.title} name={["parameters", name]} rules={rules}>
          <InputNumber
            addonAfter={property.unit || undefined}
            disabled={locked}
            max={property.maximum ?? undefined}
            min={property.minimum ?? undefined}
            precision={property.type === "integer" ? 0 : 2}
            step={property.multipleOf ?? (property.type === "integer" ? 1 : 0.05)}
          />
        </Form.Item>;
      }
      if (property.type === "boolean") {
        return <Form.Item extra={parameterExtra(property)} key={name} label={property.title} name={["parameters", name]} rules={rules} valuePropName="checked">
          <Switch disabled={locked} />
        </Form.Item>;
      }
      if (property.enum) {
        return <Form.Item extra={parameterExtra(property)} key={name} label={property.title} name={["parameters", name]} rules={rules}>
          <Select disabled={locked} options={property.enum.map((value) => ({ label: String(value), value }))} />
        </Form.Item>;
      }
      return <Form.Item extra={parameterExtra(property)} key={name} label={property.title} name={["parameters", name]} rules={rules}>
        <Input disabled={locked} />
      </Form.Item>;
      })}
  </div>;
}

function actionForStep(
  step: WorkflowStep,
  actions: RobotActionDefinition[],
) {
  return actions.find((action) => action.capability === step.capability
    && Object.entries(action.fixed_parameters).every(([name, value]) => (
      JSON.stringify(step.parameters?.[name]) === JSON.stringify(value)
    )));
}

function actionParametersWithDefaults(
  action: RobotActionDefinition,
  definition: SkillDefinition | undefined,
  current: Record<string, unknown> = {},
) {
  const parameters: Record<string, unknown> = { ...action.fixed_parameters };
  if (!definition) return parameters;
  for (const name of action.editable_parameters) {
    const property = definition.parameter_schema.properties[name];
    if (!property) continue;
    if (Object.hasOwn(current, name)) parameters[name] = current[name];
    else if (property.default !== null) parameters[name] = property.default;
    else if (property.const !== null) parameters[name] = property.const;
  }
  return parameters;
}

function normalizeLinearSteps(steps: WorkflowStep[]) {
  return steps.map((step, index) => ({
    ...step,
    depends_on: index === 0 ? [] : [steps[index - 1].step_id],
  }));
}

function rolesForActionSequence(
  steps: WorkflowStep[],
  actions: RobotActionDefinition[],
  previousRoles: WorkflowDraftRole[],
) {
  const roles = new Map<string, WorkflowDraftRole>();
  for (const step of steps) {
    const action = actionForStep(step, actions);
    if (!action) {
      const previous = previousRoles.find((role) => role.role === step.role);
      if (previous) roles.set(previous.role, structuredClone(previous));
      continue;
    }
    const current = roles.get(action.role_template);
    if (current) {
      if (!current.requires.includes(action.capability)) {
        current.requires.push(action.capability);
      }
      continue;
    }
    roles.set(action.role_template, {
      access_mode: action.access_mode,
      asset_type: action.asset_type,
      cardinality: "1",
      requires: [action.capability],
      role: action.role_template,
    });
  }
  return Array.from(roles.values());
}

function taskKey(task: TaskDefinition) {
  return `${task.task_definition_id}@${task.version}`;
}

function copyDraft(draft: WorkflowDraft): WorkflowDraft {
  return structuredClone(draft);
}

function runColor(state: string) {
  if (state === "COMPLETE") return "success";
  if (state === "FAILED" || state === "INTERRUPTED") return "error";
  if (state === "RUNNING") return "processing";
  return "default";
}

function stepColor(state: string) {
  if (state === "SUCCEEDED") return "success";
  if (state === "FAILED" || state === "HELD") return "error";
  if (state === "DISPATCHED" || state === "RUNNING") return "processing";
  return "default";
}

function validationColor(state: string) {
  if (state === "PASS") return "success";
  if (state === "FAIL") return "error";
  if (state === "AWAITING_RESET") return "warning";
  return "processing";
}

export default function WorkflowStudioApp() {
  const { message, modal } = AntApp.useApp();
  const [configuration, setConfiguration] = useState(EMPTY_CONFIGURATION);
  const [drafts, setDrafts] = useState<WorkflowDraft[]>([]);
  const [publications, setPublications] = useState<WorkflowPublication[]>([]);
  const [adapters, setAdapters] = useState<SimulationAdapterStatus[]>([]);
  const [runs, setRuns] = useState<WorkflowSimulationRun[]>([]);
  const [profiles, setProfiles] = useState<SimulationValidationProfile[]>([]);
  const [simulationSelections, setSimulationSelections] = useState<Record<string, string | null>>({});
  const [candidates, setCandidates] = useState<CommissioningCandidate[]>([]);
  const [runtime, setRuntime] = useState<UnityRuntimeStatus>(EMPTY_RUNTIME);
  const [actions, setActions] = useState<RobotActionDefinition[]>([]);
  const [skills, setSkills] = useState<SkillDefinition[]>([]);
  const [working, setWorking] = useState<WorkflowDraft | null>(null);
  const [validation, setValidation] = useState<WorkflowValidation | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [newOpen, setNewOpen] = useState(false);
  const [cloneOpen, setCloneOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [profilePublication, setProfilePublication] = useState<WorkflowPublication | null>(null);
  const [profileAdapterId, setProfileAdapterId] = useState<string | null>(null);
  const [runtimeBusy, setRuntimeBusy] = useState(false);
  const [sourcePromptHandled, setSourcePromptHandled] = useState(false);
  const [actionOpen, setActionOpen] = useState(false);
  const [stepIndex, setStepIndex] = useState<number | null>(null);
  const [connection, setConnection] = useState<ConnectionState>({
    detail: "Loading authoring registry", kind: "connecting", label: "Connecting",
  });
  const [newForm] = Form.useForm<NewDraftValues>();
  const [cloneForm] = Form.useForm<CloneValues>();
  const [actionForm] = Form.useForm<ActionValues>();
  const [profileForm] = Form.useForm<ValidationProfileValues>();
  const pendingRequestIds = useRef(new Map<string, string>());
  const cloneMode = Form.useWatch("mode", cloneForm);
  const actionRobotType = Form.useWatch("robot_type", actionForm);
  const actionRef = Form.useWatch("action_ref", actionForm);
  const selectedAction = actions.find((action) => action.action_ref === actionRef);
  const selectedSkill = skills.find((skill) => skill.capability === selectedAction?.capability);

  async function requestIdempotent<T>(
    method: "POST" | "PUT",
    path: string,
    operation: string,
    payload: Record<string, unknown>,
  ) {
    const encoded = new TextEncoder().encode(`${operation}:${JSON.stringify(payload)}`);
    const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", encoded)))
      .map((value) => value.toString(16).padStart(2, "0"))
      .join("");
    const key = `sang.workflow.request.${operation}.${digest}`;
    let requestId = pendingRequestIds.current.get(key);
    try {
      requestId ||= window.sessionStorage.getItem(key) || undefined;
    } catch {
      // The in-memory registry still preserves retries when storage is unavailable.
    }
    if (!requestId) {
      requestId = `req-${crypto.randomUUID()}`;
      pendingRequestIds.current.set(key, requestId);
      try {
        window.sessionStorage.setItem(key, requestId);
      } catch {
        // Storage can be disabled by browser policy.
      }
    }
    const result = await requestJson<T>(method, path, { ...payload, request_id: requestId });
    pendingRequestIds.current.delete(key);
    try {
      window.sessionStorage.removeItem(key);
    } catch {
      // Nothing to clear outside the in-memory registry.
    }
    return result;
  }

  async function refresh(selectDraftId?: string) {
    setLoading(true);
    try {
      const [nextDrafts, nextPublications, nextConfiguration, nextAdapters, nextRuns, nextActions, nextSkills, nextProfiles, nextCandidates, nextRuntime] = await Promise.all([
        getJson<WorkflowDraft[]>(API.drafts),
        getJson<WorkflowPublication[]>(API.publications),
        getJson<Configuration>(API.configuration),
        getJson<SimulationAdapterStatus[]>(API.adapters),
        getJson<WorkflowSimulationRun[]>(API.runs),
        getJson<RobotActionDefinition[]>(API.actions),
        getJson<SkillDefinition[]>(API.skills),
        getJson<SimulationValidationProfile[]>(API.profiles),
        getJson<CommissioningCandidate[]>(API.candidates),
        getJson<UnityRuntimeStatus>(API.runtime),
      ]);
      setDrafts(nextDrafts);
      setPublications(nextPublications);
      setConfiguration(nextConfiguration);
      setAdapters(nextAdapters);
      setRuns(nextRuns);
      setActions(nextActions);
      setSkills(nextSkills);
      setProfiles(nextProfiles);
      setCandidates(nextCandidates);
      setRuntime(nextRuntime);
      const wanted = selectDraftId || working?.draft_id;
      if (wanted) {
        const selected = nextDrafts.find((item) => item.draft_id === wanted);
        if (selected) setWorking(copyDraft(selected));
      }
      setConnection({ detail: "Draft and publication registry", kind: "live", label: "Central live" });
    } catch (error) {
      setConnection({ detail: errorMessage(error), kind: "error", label: "Central unavailable" });
      void message.error(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }

  async function refreshRuntime() {
    try {
      const [nextAdapters, nextRuns, nextProfiles, nextCandidates, nextRuntime] = await Promise.all([
        getJson<SimulationAdapterStatus[]>(API.adapters),
        getJson<WorkflowSimulationRun[]>(API.runs),
        getJson<SimulationValidationProfile[]>(API.profiles),
        getJson<CommissioningCandidate[]>(API.candidates),
        getJson<UnityRuntimeStatus>(API.runtime),
      ]);
      startTransition(() => {
        setAdapters(nextAdapters);
        setRuns(nextRuns);
        setProfiles(nextProfiles);
        setCandidates(nextCandidates);
        setRuntime(nextRuntime);
      });
    } catch (error) {
      setConnection({ detail: errorMessage(error), kind: "error", label: "Central unavailable" });
    }
  }

  useEffect(() => { void refresh(); }, []);

  useEffect(() => {
    if (!initialSourceTask || sourcePromptHandled || !configuration.task_definitions.length) return;
    if (configuration.task_definitions.some((task) => taskKey(task) === initialSourceTask)) {
      cloneForm.setFieldsValue({ mode: "new_task", source_task: initialSourceTask });
      setCloneOpen(true);
    }
    setSourcePromptHandled(true);
  }, [cloneForm, configuration.task_definitions, sourcePromptHandled]);

  const hasActiveRun = runs.some((run) => run.state === "QUEUED" || run.state === "RUNNING");
  useEffect(() => {
    if (!hasActiveRun && !runtime.running) return;
    const timer = window.setInterval(() => void refreshRuntime(), 1000);
    return () => window.clearInterval(timer);
  }, [hasActiveRun, runtime.running]);

  async function createDraft(values: NewDraftValues) {
    const payload = {
      ...values,
      workflow_id: values.workflow_id || undefined,
    };
    try {
      const created = await requestIdempotent<WorkflowDraft>(
        "POST", API.drafts, "create", payload,
      );
      setNewOpen(false);
      newForm.resetFields();
      void message.success("Draft created.");
      await refresh(created.draft_id);
    } catch (error) {
      void message.error(errorMessage(error));
    }
  }

  async function cloneDraft(values: CloneValues) {
    const source = configuration.task_definitions.find((task) => taskKey(task) === values.source_task);
    if (!source) return;
    const payload = {
      source_task_definition_id: source.task_definition_id,
      source_task_version: source.version,
      mode: values.mode,
      ...(values.mode === "new_task" ? {
        target_task_definition_id: values.target_task_definition_id,
        target_workflow_id: values.target_workflow_id || undefined,
      } : {}),
      display_name: values.display_name || undefined,
    };
    try {
      const created = await requestIdempotent<WorkflowDraft>(
        "POST", `${API.drafts}/clone`, "clone", payload,
      );
      setCloneOpen(false);
      cloneForm.resetFields();
      void message.success(values.mode === "new_task" ? "Task copied into a new Draft." : "New version Draft created.");
      await refresh(created.draft_id);
    } catch (error) {
      void message.error(errorMessage(error));
    }
  }

  async function branchPublication(publication: WorkflowPublication) {
    const source = `${publication.task_definition_id}@${publication.task_version}`;
    await cloneDraft({ mode: "new_version", source_task: source });
  }

  async function saveDraft(): Promise<WorkflowDraft | null> {
    if (!working || working.state !== "DRAFT") return working;
    setSaving(true);
    const payload = {
      expected_revision: working.revision,
      display_name: working.display_name,
      description: working.description,
      roles: working.roles,
      steps: working.steps,
    };
    try {
      const saved = await requestIdempotent<WorkflowDraft>(
        "PUT", `${API.drafts}/${encodeURIComponent(working.draft_id)}`,
        `save-${working.draft_id}`, payload,
      );
      setWorking(copyDraft(saved));
      setDrafts((current) => current.map((item) => item.draft_id === saved.draft_id ? saved : item));
      setValidation(null);
      void message.success("Draft saved.");
      return saved;
    } catch (error) {
      void message.error(errorMessage(error));
      await refresh(working.draft_id);
      return null;
    } finally {
      setSaving(false);
    }
  }

  async function validateDraft(savedDraft?: WorkflowDraft | null) {
    const saved = savedDraft || await saveDraft();
    if (!saved) return null;
    try {
      const result = await requestJson<WorkflowValidation>("POST", `${API.drafts}/${encodeURIComponent(saved.draft_id)}/validate`, {});
      setValidation(result);
      if (result.valid) void message.success("Task and Workflow contracts are valid.");
      else void message.warning("Validation found issues.");
      return result;
    } catch (error) {
      void message.error(errorMessage(error));
      return null;
    }
  }

  async function publishDraft() {
    const saved = await saveDraft();
    if (!saved) return;
    const result = await validateDraft(saved);
    if (!result?.valid) return;
    modal.confirm({
      title: `Publish ${saved.display_name}?`,
      content: `Task ${saved.task_definition_id}@${saved.task_version} and Workflow ${saved.workflow_id}@${saved.workflow_version} become immutable.`,
      okText: "Publish immutable version",
      onOk: async () => {
        try {
          await requestIdempotent(
            "POST", `${API.drafts}/${encodeURIComponent(saved.draft_id)}/publish`,
            `publish-${saved.draft_id}`,
            { expected_revision: saved.revision },
          );
          void message.success("Immutable Task and Workflow published.");
          setWorking(null);
          setValidation(null);
          await refresh();
        } catch (error) {
          void message.error(errorMessage(error));
        }
      },
    });
  }

  function openNewAction() {
    if (!actions.length) {
      void message.warning("No registered robot actions are available.");
      return;
    }
    setStepIndex(null);
    actionForm.resetFields();
    actionForm.setFieldsValue({ robot_type: actions[0].robot_type });
    setActionOpen(true);
  }

  function openStep(index: number) {
    const step = working?.steps[index];
    if (!step) return;
    const action = actionForStep(step, actions);
    if (!action) {
      void message.warning("This legacy step has no registered robot action and is read-only.");
      return;
    }
    const definition = skills.find((skill) => skill.capability === action.capability);
    setStepIndex(index);
    actionForm.setFieldsValue({
      action_ref: action.action_ref,
      parameters: actionParametersWithDefaults(
        action,
        definition,
        step.parameters || {},
      ),
      robot_type: action.robot_type,
    });
    setActionOpen(true);
  }

  function selectActionRobot(robotType: string) {
    actionForm.resetFields(["action_ref", "parameters"]);
    actionForm.setFieldsValue({ robot_type: robotType });
  }

  function selectAction(actionReference: string) {
    const action = actions.find((item) => item.action_ref === actionReference);
    if (!action) return;
    const definition = skills.find((skill) => skill.capability === action.capability);
    actionForm.setFieldsValue({
      action_ref: action.action_ref,
      parameters: actionParametersWithDefaults(action, definition),
      robot_type: action.robot_type,
    });
  }

  function saveAction(values: ActionValues) {
    if (!working) return;
    const action = actions.find((item) => item.action_ref === values.action_ref);
    if (!action) return;
    const duplicate = working.steps.some((step, index) => (
      index !== stepIndex && actionForStep(step, actions)?.action_ref === action.action_ref
    ));
    if (duplicate) {
      actionForm.setFields([{ name: "action_ref", errors: ["This action is already in the sequence."] }]);
      return;
    }
    const definition = skills.find((skill) => skill.capability === action.capability);
    const nextStep: WorkflowStep = {
      capability: action.capability,
      depends_on: [],
      kind: "action",
      parameters: actionParametersWithDefaults(
        action,
        definition,
        values.parameters || {},
      ),
      role: action.role_template,
      step_id: action.step_id_template,
      timeout_s: action.default_timeout_s,
    };
    const nextSteps = stepIndex === null
      ? [...working.steps, nextStep]
      : working.steps.map((step, index) => index === stepIndex ? nextStep : step);
    const steps = normalizeLinearSteps(nextSteps);
    setWorking({
      ...working,
      roles: rolesForActionSequence(steps, actions, working.roles),
      steps,
    });
    setValidation(null);
    setActionOpen(false);
    setStepIndex(null);
    actionForm.resetFields();
  }

  function removeStep(index: number) {
    if (!working) return;
    const steps = normalizeLinearSteps(
      working.steps.filter((_, current) => current !== index),
    );
    setWorking({
      ...working,
      roles: rolesForActionSequence(steps, actions, working.roles),
      steps,
    });
    setValidation(null);
  }

  function moveStep(index: number, offset: number) {
    if (!working) return;
    const target = index + offset;
    if (target < 0 || target >= working.steps.length) return;
    const steps = [...working.steps];
    [steps[index], steps[target]] = [steps[target], steps[index]];
    setWorking({ ...working, steps: normalizeLinearSteps(steps) });
    setValidation(null);
  }

  function publicationSimulation(publication: WorkflowPublication) {
    return selectPublicationSimulation(
      publication, profiles,
      simulationSelections[`${publication.task_definition_id}@${publication.task_version}`],
    );
  }

  function openValidationProfile(publication: WorkflowPublication, compatibility: WorkflowCompatibility) {
    const existing = latestSimulationProfile(publication, compatibility, profiles);
    setProfilePublication(publication);
    setProfileAdapterId(compatibility.adapter_id);
    profileForm.setFieldsValue({
      description: existing?.description || "Output-disconnected simulation acceptance gate",
      display_name: existing?.display_name || `${publication.display_name} simulation acceptance`,
      max_duration_s: existing?.max_duration_s || 600,
      profile_id: existing?.profile_id || `${publication.task_definition_id}-simulation-validation`,
      require_reset_acknowledged: existing?.require_reset_acknowledged ?? true,
      required_successful_runs: existing?.required_successful_runs || 1,
    });
    setProfileOpen(true);
  }

  async function createValidationProfile(values: ValidationProfileValues) {
    if (!profilePublication || !profileAdapterId) return;
    try {
      await requestIdempotent<SimulationValidationProfile>(
        "POST",
        API.profiles,
        `validation-profile-${values.profile_id}`,
        {
          ...values,
          description: values.description || undefined,
          task_definition_id: profilePublication.task_definition_id,
          task_version: profilePublication.task_version,
          adapter_id: profileAdapterId,
        },
      );
      setProfileOpen(false);
      setProfilePublication(null);
      setProfileAdapterId(null);
      profileForm.resetFields();
      void message.success("Immutable simulation validation profile created.");
      await refreshRuntime();
    } catch (error) {
      void message.error(errorMessage(error));
    }
  }

  async function waitForRuntimeReady(adapterId: string) {
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const [nextRuntime, nextAdapters] = await Promise.all([
        getJson<UnityRuntimeStatus>(API.runtime),
        getJson<SimulationAdapterStatus[]>(API.adapters),
      ]);
      setRuntime(nextRuntime);
      setAdapters(nextAdapters);
      if (nextRuntime.physical_output_connected) {
        throw new Error("Unity runtime reported a physical output connection.");
      }
      if (nextRuntime.ready && nextAdapters.some((item) => item.adapter_id === adapterId && item.ready)) {
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 500));
    }
    throw new Error("Unity did not reach output-isolated READY state within 30 seconds.");
  }

  async function startRuntime(adapterId = "unity-iitp-scenario1", manageBusy = true) {
    if (manageBusy) setRuntimeBusy(true);
    try {
      const current = await getJson<UnityRuntimeStatus>(API.runtime);
      if (!current.configured) throw new Error("Unity runtime supervisor is not configured.");
      if (!current.running) {
        await requestJson<UnityRuntimeStatus>("POST", API.runtimeStart, {});
      }
      await waitForRuntimeReady(adapterId);
      void message.success("Unity is ready with physical outputs disconnected.");
    } finally {
      if (manageBusy) setRuntimeBusy(false);
    }
  }

  async function stopRuntime() {
    setRuntimeBusy(true);
    try {
      const stopped = await requestJson<UnityRuntimeStatus>("POST", API.runtimeStop, {});
      setRuntime(stopped);
      await refreshRuntime();
      void message.success("Unity simulation Player stopped.");
    } catch (error) {
      void message.error(errorMessage(error));
    } finally {
      setRuntimeBusy(false);
    }
  }

  async function queueSimulation(
    publication: WorkflowPublication,
    compatibility: WorkflowCompatibility,
    profile: SimulationValidationProfile,
  ) {
    const adapterId = compatibility.adapter_id;
    const payload = {
      task_definition_id: publication.task_definition_id,
      task_version: publication.task_version,
      adapter_id: adapterId,
      display_name: `${publication.display_name} simulation`,
      validation_profile_id: profile.profile_id,
      validation_profile_version: profile.version,
    };
    setRuntimeBusy(true);
    try {
      const adapter = adapters.find((item) => item.adapter_id === adapterId
        && item.profile_sha256 === compatibility.profile_sha256);
      if (!adapter || profile.adapter_profile_sha256 !== compatibility.profile_sha256) {
        throw new Error("The selected simulation adapter or gate changed. Refresh before validating.");
      }
      if (adapter.execution_runtime === "isaac") {
        if (!adapter.ready) throw new Error("The local Isaac Worker is not ready. Start it outside the browser.");
      } else {
        await startRuntime(adapterId, false);
      }
      const queued = await requestIdempotent<WorkflowSimulationRun>(
        "POST", API.runs, "simulate", payload,
      );
      void message.success(`Validation Run queued against ${profile.profile_id}@${profile.version}.`);
      await refreshRuntime();
      return queued;
    } catch (error) {
      void message.error(errorMessage(error));
    } finally {
      setRuntimeBusy(false);
    }
  }

  async function promoteProfile(profile: SimulationValidationProfile) {
    modal.confirm({
      title: "Create commissioning candidate?",
      content: "This records simulation approval evidence only. It does not enable or send any physical robot command.",
      okText: "Create non-actuating candidate",
      onOk: async () => {
        try {
          await requestIdempotent<CommissioningCandidate>(
            "POST",
            API.candidates,
            `commissioning-${profile.profile_id}-${profile.version}`,
            { profile_id: profile.profile_id, profile_version: profile.version },
          );
          void message.success("SIMULATION_APPROVED commissioning candidate created.");
          await refreshRuntime();
        } catch (error) {
          void message.error(errorMessage(error));
        }
      },
    });
  }

  async function controlSimulation(
    run: WorkflowSimulationRun,
    action: WorkflowSimulationControl["action"],
  ) {
    const submit = async () => {
      try {
        await requestIdempotent<WorkflowSimulationControl>(
          "POST",
          `${API.runs}/${encodeURIComponent(run.run_id)}/controls`,
          `simulation-${run.run_id}-${action.toLowerCase()}`,
          { action },
        );
        void message.success(`${action} was accepted for ${run.display_name}.`);
        await refreshRuntime();
      } catch (error) {
        void message.error(errorMessage(error));
      }
    };
    if (action === "ABORT" || action === "RESET") {
      modal.confirm({
        cancelText: "Cancel",
        content: action === "ABORT"
          ? "This ends the Simulation Run and blocks every later Workflow step."
          : adapters.find((item) => item.adapter_id === run.adapter_id)?.execution_runtime === "isaac"
            ? "Reset clears the terminal Isaac simulation state without moving any joints."
            : "Reset reloads the terminal Unity scenario to ReadyDelivery.",
        okButtonProps: { danger: action === "ABORT" },
        okText: action === "ABORT" ? "Abort Run" : "Reset scenario",
        onOk: submit,
        title: action === "ABORT" ? "Abort this Simulation Run?" : "Reset the simulation scenario?",
      });
      return;
    }
    await submit();
  }

  const publicationColumns: TableColumnsType<WorkflowPublication> = [
    { title: "Task", key: "task", render: (_, row) => <span className="table-identity"><strong>{row.display_name}</strong><small>{row.task_definition_id}@{row.task_version}</small></span> },
    { title: "Workflow", key: "workflow", render: (_, row) => <span className="table-identity"><strong>{row.workflow_id}@{row.workflow_version}</strong><small className="digest-text">{row.workflow_sha256}</small></span> },
    { title: "Steps", key: "steps", width: 80, render: (_, row) => row.workflow.steps.length },
    { title: "Simulation adapter", key: "simulation-adapter", width: 280, render: (_, row) => {
      const { runnable, compatibility } = publicationSimulation(row);
      const options = runnable.map((item) => {
        const adapter = adapters.find((candidate) => candidate.adapter_id === item.adapter_id
          && candidate.profile_sha256 === item.profile_sha256);
        const runtimeLabel = adapter ? adapter.execution_runtime === "isaac" ? "Isaac" : "Unity" : "Simulation";
        return { label: `${runtimeLabel} · ${item.adapter_id}`, value: simulationAdapterKey(item) };
      });
      if (runnable.length < 2 && compatibility) return options[0]?.label;
      if (!runnable.length) return <Text type="secondary">No runnable adapter</Text>;
      return <Select
        allowClear
        aria-label={`Simulation adapter for ${row.display_name} v${row.task_version}`}
        disabled={runtimeBusy}
        onChange={(value: string | undefined) => setSimulationSelections((current) => ({
          ...current, [`${row.task_definition_id}@${row.task_version}`]: value ?? null,
        }))}
        options={options}
        placeholder="Select simulation adapter"
        size="small"
        style={{ width: "100%" }}
        value={compatibility ? simulationAdapterKey(compatibility) : undefined}
      />;
    } },
    { title: "Validation gate", key: "validation", width: 170, render: (_, row) => {
      const { compatibility, profile, runnable } = publicationSimulation(row);
      if (!compatibility && runnable.length) return <Tag>SELECT ADAPTER</Tag>;
      if (!profile) return <Tag>NOT CONFIGURED</Tag>;
      if (profile.commissioning_candidate) return <Tag color="success">SIMULATION APPROVED</Tag>;
      if (profile.qualified) return <Tag color="success">READY TO PROMOTE</Tag>;
      return <Tag color="processing">{profile.passing_run_count}/{profile.required_successful_runs} PASS</Tag>;
    } },
    { title: "Published", dataIndex: "published_at", key: "published", width: 175, render: (value) => new Date(value).toLocaleString() },
    { title: "Actions", key: "actions", width: 480, render: (_, row) => {
      const { compatibility, profile, runnable } = publicationSimulation(row);
      const adapter = adapters.find((item) => item.adapter_id === compatibility?.adapter_id
        && item.profile_sha256 === compatibility?.profile_sha256);
      const isaac = adapter?.execution_runtime === "isaac";
      const runtimeAvailable = !!adapter && (isaac ? adapter.ready : runtime.configured);
      const reason = !compatibility
        ? runnable.length ? "Select a simulation adapter first"
          : (row.simulation_compatibility[0]?.issues.join(" · ") || "No simulation adapter configured")
        : !adapter
          ? "Refresh to load the current simulation adapter status"
          : !runtimeAvailable
          ? isaac ? "Start the isolated Isaac runtime and Worker outside the browser" : "Unity runtime supervisor is not configured"
          : !profile
            ? "Create a validation profile first"
            : hasActiveRun
              ? "Wait for the active Simulation Run to finish"
              : isaac ? "Queue on the ready, output-disconnected Isaac Worker" : "Unity starts automatically and remains output-disconnected";
      return <Space wrap>
        <Button icon={<SwapOutlined />} onClick={() => void branchPublication(row)} size="small">New version</Button>
        <Button icon={<CopyOutlined />} onClick={() => {
          cloneForm.setFieldsValue({ mode: "new_task", source_task: `${row.task_definition_id}@${row.task_version}` });
          setCloneOpen(true);
        }} size="small">Copy as Task</Button>
        <Button disabled={!compatibility} onClick={() => compatibility && openValidationProfile(row, compatibility)} size="small">
          {profile ? "New gate version" : "Configure gate"}
        </Button>
        <Tooltip title={reason}>
          <Button disabled={!compatibility || !profile || !runtimeAvailable || hasActiveRun || runtimeBusy} icon={<ExperimentOutlined />} onClick={() => compatibility && profile && void queueSimulation(row, compatibility, profile)} loading={runtimeBusy} size="small" type="primary">{adapter ? `Validate in ${isaac ? "Isaac" : "Unity"}` : "Validate simulation"}</Button>
        </Tooltip>
      </Space>;
    } },
  ];

  const runColumns: TableColumnsType<WorkflowSimulationRun> = [
    { title: "Simulation Run", key: "run", render: (_, row) => <span className="table-identity"><strong>{row.display_name}</strong><small>{row.run_id}</small></span> },
    { title: "Publication", key: "publication", render: (_, row) => <Text code>{row.task_definition_id}@{row.task_version} / {row.workflow_id}@{row.workflow_version}</Text> },
    { title: "Adapter", dataIndex: "adapter_id", key: "adapter" },
    { title: "State", key: "state", width: 175, render: (_, row) => <Space size={4} wrap><Tag color={runColor(row.state)}>{row.state}</Tag>{row.progress.lifecycle_state && row.progress.lifecycle_state !== row.state ? <Tag>{row.progress.lifecycle_state}</Tag> : null}{row.reconciliation_required ? <Tag color="warning">RECONCILING</Tag> : null}</Space> },
    { title: "Validation", key: "validation", width: 155, render: (_, row) => row.validation ? <Tag color={validationColor(row.validation.state)}>{row.validation.state}</Tag> : <Tag>LEGACY RUN</Tag> },
    { title: "Updated", dataIndex: "updated_at", key: "updated", width: 175, render: (value) => new Date(value).toLocaleString() },
    { title: "Operations", key: "operations", width: 310, render: (_, row) => {
      const pending = row.controls.some((control) => control.state === "REQUESTED" || control.state === "CLAIMED");
      const held = row.progress.lifecycle_state === "HELD";
      const terminal = ["COMPLETE", "FAILED", "INTERRUPTED"].includes(row.state);
      return <Space size={4} wrap>
        <Button disabled={pending || row.state !== "RUNNING" || held} icon={<PauseCircleOutlined />} onClick={() => void controlSimulation(row, "HOLD")} size="small">Hold</Button>
        <Button disabled={pending || row.state !== "RUNNING" || !held} icon={<PlayCircleOutlined />} onClick={() => void controlSimulation(row, "RESUME")} size="small">Resume</Button>
        <Button danger disabled={pending || !["QUEUED", "RUNNING"].includes(row.state)} icon={<StopOutlined />} onClick={() => void controlSimulation(row, "ABORT")} size="small">Abort</Button>
        <Button disabled={pending || !terminal} icon={<UndoOutlined />} onClick={() => void controlSimulation(row, "RESET")} size="small">Reset</Button>
      </Space>;
    } },
  ];

  const profileColumns: TableColumnsType<SimulationValidationProfile> = [
    { title: "Validation Profile", key: "profile", render: (_, row) => <span className="table-identity"><strong>{row.display_name}</strong><small>{row.profile_id}@{row.version}</small></span> },
    { title: "Published Task", key: "task", render: (_, row) => <Text code>{row.task_definition_id}@{row.task_version}</Text> },
    { title: "Acceptance", key: "acceptance", render: (_, row) => <Space wrap><Tag>All steps</Tag><Tag>Output isolated</Tag><Tag>{row.max_duration_s}s max</Tag>{row.require_reset_acknowledged ? <Tag>Reset required</Tag> : null}</Space> },
    { title: "Evidence", key: "evidence", width: 150, render: (_, row) => <Tag color={row.qualified ? "success" : "processing"}>{row.passing_run_count}/{row.required_successful_runs} PASS</Tag> },
    { title: "State", key: "state", width: 190, render: (_, row) => row.commissioning_candidate ? <Tag color="success">SIMULATION_APPROVED</Tag> : row.qualified ? <Tag color="success">PROMOTION READY</Tag> : <Tag>VALIDATING</Tag> },
    { title: "Action", key: "action", width: 210, render: (_, row) => <Button disabled={!row.qualified || Boolean(row.commissioning_candidate)} icon={<RocketOutlined />} onClick={() => void promoteProfile(row)} size="small" type="primary">Create candidate</Button> },
  ];

  const editableDrafts = drafts.filter((draft) => draft.state === "DRAFT");
  const robotOptions = Array.from(
    new Map(actions.map((action) => [
      action.robot_type,
      { label: action.robot_label, value: action.robot_type },
    ])).values(),
  );
  const currentActionRef = stepIndex === null || !working
    ? null
    : actionForStep(working.steps[stepIndex], actions)?.action_ref;
  const actionOptions = actions
    .filter((action) => action.robot_type === actionRobotType)
    .map((action) => ({
      disabled: action.action_ref !== currentActionRef && Boolean(
        working?.steps.some((step) => actionForStep(step, actions)?.action_ref === action.action_ref),
      ),
      label: `${action.title} · ${action.action_ref}`,
      value: action.action_ref,
    }));
  const draftTab = <Row gutter={[16, 16]}>
    <Col xs={24} xl={7}>
      <Card className="workflow-library" title="Draft library">
        <List
          dataSource={editableDrafts}
          locale={{ emptyText: <Empty description="Create or copy a Task to begin" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
          renderItem={(draft) => <List.Item className={working?.draft_id === draft.draft_id ? "workflow-draft-selected" : ""} onClick={() => { setWorking(copyDraft(draft)); setValidation(null); }}>
            <List.Item.Meta
              description={<Space size={4}><Tag>{draft.state}</Tag><Text type="secondary">rev {draft.revision}</Text></Space>}
              title={<span className="table-identity"><strong>{draft.display_name}</strong><small>{draft.task_definition_id}@{draft.task_version}</small></span>}
            />
          </List.Item>}
        />
      </Card>
    </Col>
    <Col xs={24} xl={17}>
      {working ? <DraftEditor
        actions={actions}
        draft={working}
        onAddAction={openNewAction}
        onEditStep={openStep}
        onMoveStep={moveStep}
        onRemoveStep={removeStep}
        onUpdate={(patch) => { setWorking({ ...working, ...patch }); setValidation(null); }}
        onSave={() => void saveDraft()}
        onValidate={() => void validateDraft()}
        onPublish={() => void publishDraft()}
        saving={saving}
        validation={validation}
      /> : <Card><Empty description="Select a Draft to edit its roles and steps" /></Card>}
    </Col>
  </Row>;

  const publishedTab = <Card>
    <Table columns={publicationColumns} dataSource={publications} pagination={{ pageSize: 10, hideOnSinglePage: true }} rowKey={(row) => `${row.task_definition_id}@${row.task_version}`} scroll={{ x: 1080 }} />
  </Card>;

  const runtimeStateLabel = runtime.ready
    ? "READY"
    : runtime.running && runtime.heartbeat?.lifecycle_state === "RUNNING"
      ? "RUN ACTIVE"
      : runtime.running
        ? "STARTING"
        : runtime.configured
          ? "STOPPED"
          : "NOT CONFIGURED";

  const validationTab = <Space className="workflow-run-stack" direction="vertical" size={16}>
    <Alert
      description="A PASS is simulation evidence, not permission to move a real robot. Promotion creates an immutable, non-actuating commissioning candidate only."
      icon={<SafetyCertificateOutlined />}
      message="Simulation-to-real promotion lock"
      showIcon
      type="warning"
    />
    <Card title="Simulation validation profiles">
      <Table
        columns={profileColumns}
        dataSource={profiles}
        pagination={{ pageSize: 10, hideOnSinglePage: true }}
        rowKey={(row) => `${row.profile_id}@${row.version}`}
        scroll={{ x: 1180 }}
      />
    </Card>
    <Card title="Commissioning candidates">
      <List
        dataSource={candidates}
        locale={{ emptyText: "No Task version has passed its configured simulation gate" }}
        renderItem={(candidate) => <List.Item>
          <List.Item.Meta
            description={`${candidate.evidence_run_ids.length} immutable Run evidence record(s) · no physical command path`}
            title={<Space wrap><Tag color="success">{candidate.state}</Tag><Text code>{candidate.task_definition_id}@{candidate.task_version}</Text><Text type="secondary">{candidate.candidate_id}</Text></Space>}
          />
          <Text type="secondary">{new Date(candidate.created_at).toLocaleString()}</Text>
        </List.Item>}
      />
    </Card>
  </Space>;

  const runsTab = <Space className="workflow-run-stack" direction="vertical" size={16}>
    <Card
      extra={<Space>
        <Button disabled={!runtime.configured || runtime.running} icon={<PlayCircleOutlined />} loading={runtimeBusy} onClick={() => void startRuntime().catch((error) => message.error(errorMessage(error)))}>Start Unity</Button>
        <Button danger disabled={!runtime.running || hasActiveRun} icon={<StopOutlined />} loading={runtimeBusy} onClick={() => void stopRuntime()}>Stop Unity</Button>
      </Space>}
      title={<Space><VideoCameraOutlined />Legacy Unity simulation runtime</Space>}
    >
      <Paragraph type="secondary">These controls manage only legacy Unity simulations, not the Isaac executor or its Unity observer.</Paragraph>
      <Row gutter={[20, 20]}>
        <Col xs={24} lg={9}>
          <Descriptions
            column={1}
            items={[
              { key: "runtime", label: "Player", children: <Badge status={runtime.ready ? "success" : runtime.running ? "processing" : "default"} text={runtimeStateLabel} /> },
              { key: "scenario", label: "Scenario", children: runtime.scenario_id || "-" },
              { key: "phase", label: "Unity phase", children: runtime.heartbeat?.observed_phase || "-" },
              { key: "preview", label: "Preview", children: runtime.preview ? `${runtime.preview.width}×${runtime.preview.height} · ${runtime.preview.target_rate_hz.toFixed(1)} fps` : "Waiting for frames" },
              { key: "isolation", label: "Physical outputs", children: <Tag color="success">DISCONNECTED</Tag> },
            ]}
            size="small"
          />
        </Col>
        <Col xs={24} lg={15}>
          <div className="workflow-unity-preview">
            {runtime.running ? <img alt="Live Unity Scenario 1 simulation" src={API.runtimePreview} /> : <Empty description="Start Unity to view the output-disconnected simulation" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
          </div>
        </Col>
      </Row>
    </Card>
    <Card title="Local simulation adapters">
      <List
        dataSource={adapters}
        locale={{ emptyText: "No adapter profile is configured" }}
        renderItem={(adapter) => <List.Item>
          <List.Item.Meta description={adapter.detail} title={<Space><Badge status={adapter.ready ? "success" : "default"} />{adapter.adapter_id}<Tag>{adapter.scenario_id}</Tag></Space>} />
          <Text type="secondary">{adapter.worker_id || "No Worker"}</Text>
        </List.Item>}
      />
    </Card>
    <Card title="Simulation Runs">
      <Table
        columns={runColumns}
        dataSource={runs}
        expandable={{ expandedRowRender: (run) => <div className="workflow-run-evidence">
          <div className="workflow-progress-list">{run.progress.steps.map((step) => <span key={step.step_id}><Tag color={stepColor(step.state)}>{step.state}</Tag><strong>{step.step_id}</strong>{step.updated_at ? <Text type="secondary">{new Date(step.updated_at).toLocaleTimeString()}</Text> : null}</span>)}{run.controls.map((control) => <span key={control.control_id}><Tag>{control.action}</Tag><strong>{control.state}</strong><Text type="secondary">{control.detail}</Text></span>)}<Paragraph type="secondary">{run.detail}</Paragraph></div>
          {run.validation ? <Card size="small" title={<Space><SafetyCertificateOutlined />Validation report <Tag color={validationColor(run.validation.state)}>{run.validation.state}</Tag></Space>}>
            <List
              dataSource={run.validation.checks}
              locale={{ emptyText: "Checks run after the simulation reaches a terminal state" }}
              renderItem={(check) => <List.Item><List.Item.Meta avatar={check.passed ? <CheckCircleOutlined className="workflow-check-pass" /> : <StopOutlined className="workflow-check-fail" />} description={check.detail} title={check.code} /></List.Item>}
            />
          </Card> : <Alert message="Legacy Run: no validation profile was pinned, so this Run can never be promoted." type="info" />}
        </div> }}
        pagination={{ pageSize: 10, hideOnSinglePage: true }}
        rowKey="run_id"
        scroll={{ x: 1280 }}
      />
    </Card>
  </Space>;

  return (
    <AppShell activeKey="workflow" connection={connection} contextTitle="Workflow Studio" scopeDetail="Output-disconnected simulation queue only" scopeTitle="Manager authoring boundary">
      <div className="page-stack workflow-page">
        <PageHeader
          actions={<Space wrap>
            <Button icon={<CopyOutlined />} onClick={() => { cloneForm.setFieldsValue({ mode: "new_task", source_task: initialSourceTask || undefined }); setCloneOpen(true); }}>Copy existing Task</Button>
            <Button icon={<PlusOutlined />} onClick={() => setNewOpen(true)} type="primary">New blank Task</Button>
            <Button icon={<ReloadOutlined spin={loading} />} loading={loading} onClick={() => void refresh()}>Reload</Button>
          </Space>}
          description="Arrange registered robot actions, publish reproducible versions, and test them in isolated Unity or Isaac simulation."
          eyebrow="MANAGER / VERSIONED EXECUTION CONTRACTS"
          title="Workflow Studio"
        />
        <Alert icon={<SafetyCertificateOutlined />} message="Published versions are immutable" description="Every change starts a new Draft. This page cannot send physical robot, welding, feeder, policy, XR, vendor, or raw joint commands." showIcon type="info" />

        <Card className="summary-card" styles={{ body: { padding: 0 } }}>
          <Row>
            <Col className="summary-cell" xs={12} lg={6}><Statistic prefix={<BranchesOutlined />} title="Drafts" value={drafts.filter((item) => item.state === "DRAFT").length} /></Col>
            <Col className="summary-cell" xs={12} lg={6}><Statistic prefix={<CheckCircleOutlined />} title="Published" value={publications.length} /></Col>
            <Col className="summary-cell" xs={12} lg={6}><Statistic prefix={<ExperimentOutlined />} title="Active simulations" value={runs.filter((run) => ["QUEUED", "RUNNING"].includes(run.state)).length} /></Col>
            <Col className="summary-cell" xs={12} lg={6}><Statistic prefix={<ApartmentOutlined />} title="Ready adapters" value={adapters.filter((item) => item.ready).length} /></Col>
          </Row>
        </Card>

        <Tabs items={[
          { key: "drafts", label: `Drafts (${editableDrafts.length})`, children: draftTab },
          { key: "published", label: `Published library (${publications.length})`, children: publishedTab },
          { key: "validation", label: `Validation gates (${profiles.length})`, children: validationTab },
          { key: "runs", label: `Simulation Runs (${runs.length})`, children: runsTab },
        ]} />
      </div>

      <Modal footer={null} onCancel={() => { setNewOpen(false); newForm.resetFields(); }} open={newOpen} title="Create blank Workflow Draft">
        <Form form={newForm} layout="vertical" onFinish={createDraft} requiredMark={false}>
          <Form.Item label="Task ID" name="task_definition_id" rules={[{ pattern: ID_PATTERN, required: true }]}><Input placeholder="coordinated-waam" /></Form.Item>
          <Form.Item extra="Defaults to the Task ID" label="Workflow ID" name="workflow_id" rules={[{ pattern: ID_PATTERN }]}><Input /></Form.Item>
          <Form.Item label="Display name" name="display_name" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item label="Description" name="description"><Input.TextArea rows={3} /></Form.Item>
          <Flex justify="end"><Button htmlType="submit" type="primary">Create Draft</Button></Flex>
        </Form>
      </Modal>

      <Modal footer={null} onCancel={() => { setCloneOpen(false); cloneForm.resetFields(); }} open={cloneOpen} title="Copy an existing Task">
        <Form form={cloneForm} initialValues={{ mode: "new_task" }} layout="vertical" onFinish={cloneDraft} requiredMark={false}>
          <Form.Item label="Source Task version" name="source_task" rules={[{ required: true }]}><Select options={configuration.task_definitions.map((task) => ({ label: `${task.display_name} · v${task.version}`, value: taskKey(task) }))} /></Form.Item>
          <Form.Item label="Copy mode" name="mode" rules={[{ required: true }]}><Select options={[{ label: "Copy as a new Task", value: "new_task" }, { label: "Create the next version", value: "new_version" }]} /></Form.Item>
          {cloneMode === "new_task" ? <>
            <Form.Item label="New Task ID" name="target_task_definition_id" rules={[{ pattern: ID_PATTERN, required: true }]}><Input /></Form.Item>
            <Form.Item extra="Defaults to the new Task ID" label="New Workflow ID" name="target_workflow_id" rules={[{ pattern: ID_PATTERN }]}><Input /></Form.Item>
          </> : <Alert className="workflow-modal-alert" message="The Task and Workflow IDs stay the same; both target their next available version." type="info" />}
          <Form.Item extra="Leave empty to copy the source name" label="Draft display name" name="display_name"><Input /></Form.Item>
          <Flex justify="end"><Button htmlType="submit" icon={<CopyOutlined />} type="primary">Create editable Draft</Button></Flex>
        </Form>
      </Modal>

      <Modal
        footer={null}
        onCancel={() => { setProfileOpen(false); setProfilePublication(null); setProfileAdapterId(null); profileForm.resetFields(); }}
        open={profileOpen}
        title="Configure simulation validation gate"
      >
        <Alert
          className="workflow-modal-alert"
          description="Saving creates a new immutable Profile version. It never changes a published Task or enables physical output."
          message={profilePublication ? `${profilePublication.task_definition_id}@${profilePublication.task_version} · ${profileAdapterId}` : "Published Task"}
          showIcon
          type="info"
        />
        <Form form={profileForm} layout="vertical" onFinish={createValidationProfile} requiredMark={false}>
          <Form.Item extra="Reuse this ID to create the next immutable Profile version." label="Profile ID" name="profile_id" rules={[{ pattern: ID_PATTERN, required: true }]}><Input /></Form.Item>
          <Form.Item label="Display name" name="display_name" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item label="Description" name="description"><Input.TextArea rows={2} /></Form.Item>
          <Row gutter={12}>
            <Col xs={24} sm={12}><Form.Item label="Required passing Runs" name="required_successful_runs" rules={[{ required: true }]}><InputNumber min={1} max={10} precision={0} /></Form.Item></Col>
            <Col xs={24} sm={12}><Form.Item label="Maximum Run duration" name="max_duration_s" rules={[{ required: true }]}><InputNumber addonAfter="seconds" min={1} max={86400} precision={0} /></Form.Item></Col>
          </Row>
          <Form.Item extra="Recommended: a terminal Run is not PASS until the simulation executor acknowledges Reset." label="Require Reset acknowledgement" name="require_reset_acknowledged" valuePropName="checked"><Switch /></Form.Item>
          <Descriptions
            bordered
            column={1}
            items={[
              { key: "steps", label: "All Workflow steps", children: <Tag color="success">MUST SUCCEED</Tag> },
              { key: "output", label: "Physical outputs", children: <Tag color="success">MUST BE DISCONNECTED</Tag> },
              { key: "digest", label: "Adapter build", children: "Exact digest pinned at Profile creation" },
            ]}
            size="small"
          />
          <Flex className="workflow-modal-submit" justify="end"><Button htmlType="submit" icon={<SafetyCertificateOutlined />} type="primary">Create immutable gate version</Button></Flex>
        </Form>
      </Modal>

      <Modal
        footer={null}
        onCancel={() => {
          setActionOpen(false);
          setStepIndex(null);
          actionForm.resetFields();
        }}
        open={actionOpen}
        title={stepIndex === null ? "Add robot action" : "Edit robot action"}
        width={760}
      >
        <Form form={actionForm} layout="vertical" onFinish={saveAction} requiredMark={false}>
          <Row gutter={12}>
            <Col xs={24} sm={10}>
              <Form.Item label="Robot" name="robot_type" rules={[{ required: true }]}>
                <Select onChange={selectActionRobot} options={robotOptions} />
              </Form.Item>
            </Col>
            <Col xs={24} sm={14}>
              <Form.Item label="Action" name="action_ref" rules={[{ required: true }]}>
                <Select onChange={selectAction} options={actionOptions} />
              </Form.Item>
            </Col>
          </Row>
          {selectedAction && selectedSkill ? <div className="workflow-action-inspector">
            <div className="workflow-action-summary">
              <Space wrap>
                <Tag color="blue">{selectedAction.robot_label}</Tag>
                <Tag color="green">{selectedAction.execution_mode}</Tag>
                <Tag>{selectedAction.script.runtime === "UNITY_CSHARP" ? "C# action" : "Python action"}</Tag>
              </Space>
              <Title level={4}>{selectedAction.title}</Title>
              <Paragraph>{selectedAction.description}</Paragraph>
              <Text type="secondary">
                Registered script: {selectedAction.script.entrypoint} · v{selectedAction.action_ref.split("@").at(-1)} · {selectedAction.script.sha256.slice(0, 10)}…
              </Text>
            </div>
            <ActionParameterFields action={selectedAction} definition={selectedSkill} />
            <Alert
              description="The action script, operation identity, role, timeout, and sequence dependency are filled automatically. This screen cannot upload or execute arbitrary code."
              message="Only safe action inputs are editable"
              showIcon
              type="info"
            />
          </div> : <Empty description="Choose a registered action for this robot" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
          <Flex justify="end">
            <Button disabled={!selectedAction} htmlType="submit" type="primary">
              {stepIndex === null ? "Add to sequence" : "Apply action"}
            </Button>
          </Flex>
        </Form>
      </Modal>
    </AppShell>
  );
}

function DraftEditor({
  actions, draft, onAddAction, onEditStep, onMoveStep, onPublish,
  onRemoveStep, onSave, onUpdate, onValidate, saving, validation,
}: {
  actions: RobotActionDefinition[];
  draft: WorkflowDraft;
  onAddAction: () => void;
  onEditStep: (index: number) => void;
  onMoveStep: (index: number, offset: number) => void;
  onPublish: () => void;
  onRemoveStep: (index: number) => void;
  onSave: () => void;
  onUpdate: (patch: Partial<WorkflowDraft>) => void;
  onValidate: () => void;
  saving: boolean;
  validation: WorkflowValidation | null;
}) {
  const editable = draft.state === "DRAFT";
  return <Space className="workflow-editor-stack" direction="vertical" size={16}>
    <Card extra={<Tag color={editable ? "processing" : "success"}>{draft.state}</Tag>} title={<span className="table-identity"><strong>{draft.display_name}</strong><small>{draft.draft_id} · rev {draft.revision}</small></span>}>
      <Descriptions column={{ xs: 1, sm: 2 }} items={[
        { key: "task", label: "Target Task", children: <Text code>{draft.task_definition_id}@{draft.task_version}</Text> },
        { key: "workflow", label: "Target Workflow", children: <Text code>{draft.workflow_id}@{draft.workflow_version}</Text> },
      ]} size="small" />
      <Row gutter={12}>
        <Col xs={24} sm={12}><label className="workflow-field-label">Display name</label><Input disabled={!editable} onChange={(event) => onUpdate({ display_name: event.target.value })} value={draft.display_name} /></Col>
        <Col xs={24} sm={12}><label className="workflow-field-label">Description</label><Input disabled={!editable} onChange={(event) => onUpdate({ description: event.target.value || null })} value={draft.description || ""} /></Col>
      </Row>
    </Card>

    <Card title="Robots in this Task">
      {draft.steps.length ? <Space wrap>
        {Array.from(new Map(draft.steps.map((step) => {
          const action = actionForStep(step, actions);
          return action ? [action.robot_type, action] : [step.role || step.step_id, null];
        })).values()).map((action, index) => action
          ? <Tag color="blue" key={action.robot_type}>{action.robot_label}</Tag>
          : <Tag color="warning" key={`legacy-${index}`}>Legacy step</Tag>)}
      </Space> : <Text type="secondary">Robots are added automatically with their first action.</Text>}
    </Card>

    <Card
      extra={editable ? <Button icon={<PlusOutlined />} onClick={onAddAction} type="primary">Add robot action</Button> : null}
      title="Action sequence"
    >
      {draft.steps.length ? <div className="workflow-step-list">
        {draft.steps.map((step, index) => {
          const action = actionForStep(step, actions);
          const supported = Boolean(action);
          const speed = step.parameters?.speed_scale;
          return <Card className="workflow-step-card" key={`${step.step_id}-${index}`} size="small">
            <Flex align="flex-start" gap={12} justify="space-between" wrap>
              <Space align="start">
                <span className="workflow-step-number">{index + 1}</span>
                <span className="table-identity">
                  <strong>{action?.title || "Unregistered legacy step"}</strong>
                  <small>{action ? `${action.robot_label} · ${action.action_ref}` : step.step_id}</small>
                </span>
              </Space>
              <Space wrap>
                {action ? <Tag>{action.script.runtime === "UNITY_CSHARP" ? "C#" : "Python"}</Tag> : null}
                {typeof speed === "number" ? <Tag color="blue">{speed.toFixed(2)}x</Tag> : null}
                <Button disabled={!editable || !supported || index === 0} onClick={() => onMoveStep(index, -1)} size="small">Up</Button>
                <Button disabled={!editable || !supported || index === draft.steps.length - 1} onClick={() => onMoveStep(index, 1)} size="small">Down</Button>
                <Button disabled={!editable || !supported} onClick={() => onEditStep(index)} size="small">Edit</Button>
                <Button danger disabled={!editable || !supported} onClick={() => onRemoveStep(index)} size="small">Remove</Button>
              </Space>
            </Flex>
            {action ? <div className="workflow-action-script">Registered action: {action.script.entrypoint} · {action.script.sha256.slice(0, 10)}…</div> : null}
            {!supported ? <Alert message={`${step.kind} is preserved read-only because it has no registered action script.`} type="warning" /> : null}
          </Card>;
        })}
      </div> : <Empty description="Add robot actions in the order they should run" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
      <Alert
        className="workflow-sequence-note"
        description="The system creates Step IDs, roles, fixed operations, and dependencies automatically. Actions run from top to bottom."
        message="Simple linear sequence"
        showIcon
        type="info"
      />
    </Card>

    {validation ? <Alert
      message={validation.valid ? "Ready to publish" : `${validation.issues.length} validation issue(s)`}
      description={validation.valid ? `Workflow digest: ${validation.workflow_sha256}` : validation.issues.map((issue) => issue.message).join(" · ")}
      showIcon
      type={validation.valid ? "success" : "error"}
    /> : null}

    {editable ? <Flex gap={8} justify="end" wrap>
      <Button icon={<SaveOutlined />} loading={saving} onClick={onSave}>Save Draft</Button>
      <Button icon={<CheckCircleOutlined />} onClick={onValidate}>Validate</Button>
      <Button icon={<SendOutlined />} onClick={onPublish} type="primary">Publish version</Button>
    </Flex> : <Alert message="Published Drafts are read-only. Use New version or Copy as Task from the published library." type="success" />}
  </Space>;
}
