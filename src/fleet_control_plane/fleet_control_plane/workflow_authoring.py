"""Revisioned Workflow drafts, immutable publication, and simulation queue state."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, JsonValue, ValidationError, field_validator, model_validator

from fleet_control_plane.action_library import ActionLibraryStore
from fleet_control_plane.isaac_simulation_backend import ISAAC_SCENARIO1_BINDINGS, ISAAC_SCENARIO1_STEPS
from fleet_control_plane.models import FleetModel, SafeId, ShortText
from fleet_control_plane.operations_models import (
    AccessMode,
    AssetType,
    TaskDefinitionCreate,
    TaskRoleCreate,
)
from fleet_control_plane.orchestration_models import (
    CapabilityRef,
    Sha256Digest,
    WorkflowDefinitionV2,
    WorkflowRoleV2,
)
from fleet_control_plane.orchestration_store import canonical_json, payload_digest
from fleet_control_plane.skill_catalog import (
    RobotActionDefinitionV1,
    SkillDefinitionV1,
    load_skill_catalog,
    validate_skill_parameters,
)
from fleet_control_plane.status import isoformat_z, parse_datetime
from fleet_control_plane.unity_dynamic_binding import UnityDynamicBindingV1, resolve_unity_workflow


MAX_AUTHORING_JSON_DEPTH = 12
WORKFLOW_AUTHORING_SCHEMA_VERSION = 19
WORKER_HEARTBEAT_MAX_AGE_S = 5.0
SIMULATION_QUEUE_TTL_S = 15.0

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "access_key",
        "authorization",
        "asset_id",
        "asset_ids",
        "connection",
        "credential",
        "client_key",
        "client_secret",
        "endpoint",
        "host",
        "hostname",
        "ip",
        "ip_address",
        "password",
        "port",
        "private_key",
        "ros_topic",
        "secret",
        "signing_key",
        "token",
        "topic",
        "vendor",
        "vendor_api",
    }
)

_SENSITIVE_KEY_PARTS = frozenset(
    {
        "asset",
        "address",
        "auth",
        "authorization",
        "connection",
        "credential",
        "endpoint",
        "host",
        "hostname",
        "password",
        "port",
        "network",
        "secret",
        "token",
        "topic",
        "vendor",
    }
)

_ALLOWED_REFERENCE_SCHEMES = frozenset(
    {"artifact", "contracts", "registry", "workcell"}
)
_URI_SCHEME_PATTERN = re.compile(
    r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]*)://"
)
_COMPACT_SENSITIVE_KEYS = frozenset(
    name.replace("_", "") for name in _SENSITIVE_KEYS if "_" in name
)


WORKFLOW_AUTHORING_MIGRATION = """
CREATE TABLE IF NOT EXISTS workflow_drafts (
    draft_id TEXT PRIMARY KEY,
    task_definition_id TEXT NOT NULL,
    task_version INTEGER NOT NULL CHECK (task_version >= 1),
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL CHECK (workflow_version >= 1),
    display_name TEXT NOT NULL,
    description TEXT,
    roles_json TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    source_task_definition_id TEXT,
    source_task_version INTEGER,
    source_workflow_id TEXT,
    source_workflow_version INTEGER,
    state TEXT NOT NULL CHECK (state IN ('DRAFT', 'PUBLISHED')),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT,
    CHECK (
        (source_task_definition_id IS NULL AND source_task_version IS NULL)
        OR
        (source_task_definition_id IS NOT NULL AND source_task_version IS NOT NULL)
    ),
    CHECK (
        (source_workflow_id IS NULL AND source_workflow_version IS NULL)
        OR
        (source_workflow_id IS NOT NULL AND source_workflow_version IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS task_workflow_publications (
    task_definition_id TEXT NOT NULL,
    task_version INTEGER NOT NULL CHECK (task_version >= 1),
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL CHECK (workflow_version >= 1),
    workflow_sha256 TEXT NOT NULL CHECK (length(workflow_sha256) = 64),
    source_draft_id TEXT NOT NULL REFERENCES workflow_drafts(draft_id) ON DELETE RESTRICT,
    published_at TEXT NOT NULL,
    PRIMARY KEY (task_definition_id, task_version),
    UNIQUE (workflow_id, workflow_version),
    FOREIGN KEY (task_definition_id, task_version)
        REFERENCES task_definitions(task_definition_id, version) ON DELETE RESTRICT
);

CREATE TRIGGER IF NOT EXISTS task_workflow_publications_no_update
BEFORE UPDATE ON task_workflow_publications
BEGIN
    SELECT RAISE(ABORT, 'Task-Workflow publications are immutable');
END;

CREATE TRIGGER IF NOT EXISTS task_workflow_publications_no_delete
BEFORE DELETE ON task_workflow_publications
BEGIN
    SELECT RAISE(ABORT, 'Task-Workflow publications are immutable');
END;

CREATE TRIGGER IF NOT EXISTS task_workflow_publications_validate_workflow
BEFORE INSERT ON task_workflow_publications
WHEN NOT EXISTS (
    SELECT 1 FROM orchestration_registry
    WHERE object_kind = 'workflow'
      AND object_id = NEW.workflow_id
      AND version = NEW.workflow_version
      AND payload_sha256 = NEW.workflow_sha256
)
BEGIN
    SELECT RAISE(ABORT, 'Task-Workflow publication requires its exact Workflow digest');
END;

CREATE TRIGGER IF NOT EXISTS task_definitions_no_update
BEFORE UPDATE ON task_definitions
BEGIN
    SELECT RAISE(ABORT, 'Task Definitions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS task_definitions_no_delete
BEFORE DELETE ON task_definitions
BEGIN
    SELECT RAISE(ABORT, 'Task Definitions are immutable');
END;

CREATE TABLE IF NOT EXISTS workflow_authoring_requests (
    principal_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, request_id)
);

CREATE TABLE IF NOT EXISTS workflow_simulation_workers (
    worker_id TEXT PRIMARY KEY,
    adapter_id TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    profile_sha256 TEXT NOT NULL CHECK (length(profile_sha256) = 64),
    ready INTEGER NOT NULL CHECK (ready IN (0, 1)),
    physical_output_connected INTEGER NOT NULL CHECK (physical_output_connected = 0),
    detail TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS workflow_simulation_workers_adapter
    ON workflow_simulation_workers(adapter_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS workflow_simulation_runs (
    run_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    profile_sha256 TEXT NOT NULL CHECK (length(profile_sha256) = 64),
    task_definition_id TEXT NOT NULL,
    task_version INTEGER NOT NULL CHECK (task_version >= 1),
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL CHECK (workflow_version >= 1),
    workflow_sha256 TEXT NOT NULL CHECK (length(workflow_sha256) = 64),
    state TEXT NOT NULL CHECK (
        state IN ('QUEUED', 'RUNNING', 'COMPLETE', 'FAILED', 'INTERRUPTED')
    ),
    claimed_worker_id TEXT REFERENCES workflow_simulation_workers(worker_id),
    lease_expires_at TEXT,
    queue_expires_at TEXT NOT NULL,
    detail TEXT NOT NULL,
    progress_json TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE (principal_id, request_id),
    FOREIGN KEY (task_definition_id, task_version)
        REFERENCES task_workflow_publications(task_definition_id, task_version)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS workflow_simulation_one_active_adapter
    ON workflow_simulation_runs(adapter_id)
    WHERE state IN ('QUEUED', 'RUNNING');

CREATE TABLE IF NOT EXISTS workflow_simulation_run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES workflow_simulation_runs(run_id) ON DELETE RESTRICT,
    occurred_at TEXT NOT NULL,
    state TEXT NOT NULL,
    detail TEXT NOT NULL,
    progress_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS workflow_simulation_run_events_run
    ON workflow_simulation_run_events(run_id, id);

CREATE TRIGGER IF NOT EXISTS workflow_simulation_run_events_no_update
BEFORE UPDATE ON workflow_simulation_run_events
BEGIN
    SELECT RAISE(ABORT, 'Simulation Run events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS workflow_simulation_run_events_no_delete
BEFORE DELETE ON workflow_simulation_run_events
BEGIN
    SELECT RAISE(ABORT, 'Simulation Run events are append-only');
END;
"""


WORKFLOW_SIMULATION_RECONCILIATION_COLUMN_MIGRATION = """
ALTER TABLE workflow_simulation_runs
    ADD COLUMN reconciliation_required INTEGER NOT NULL DEFAULT 0
        CHECK (reconciliation_required IN (0, 1));
"""


WORKFLOW_SIMULATION_OPERATIONS_MIGRATION = (
    """
CREATE TABLE IF NOT EXISTS workflow_simulation_run_controls (
    control_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_simulation_runs(run_id) ON DELETE RESTRICT,
    principal_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('HOLD', 'RESUME', 'ABORT', 'RESET')),
    state TEXT NOT NULL CHECK (
        state IN ('REQUESTED', 'CLAIMED', 'ACKNOWLEDGED', 'REJECTED')
    ),
    claimed_worker_id TEXT REFERENCES workflow_simulation_workers(worker_id),
    claim_expires_at TEXT,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (principal_id, request_id)
)
""",
    """

CREATE UNIQUE INDEX IF NOT EXISTS workflow_simulation_one_pending_control
    ON workflow_simulation_run_controls(run_id)
    WHERE state IN ('REQUESTED', 'CLAIMED')
""",
    """

CREATE INDEX IF NOT EXISTS workflow_simulation_controls_run
    ON workflow_simulation_run_controls(run_id, created_at, control_id)
""",
)


WORKFLOW_SIMULATION_VALIDATION_MIGRATION = """
CREATE TABLE IF NOT EXISTS workflow_simulation_validation_profiles (
    profile_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    display_name TEXT NOT NULL,
    description TEXT,
    task_definition_id TEXT NOT NULL,
    task_version INTEGER NOT NULL CHECK (task_version >= 1),
    adapter_id TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    adapter_profile_sha256 TEXT NOT NULL CHECK (length(adapter_profile_sha256) = 64),
    required_successful_runs INTEGER NOT NULL CHECK (
        required_successful_runs BETWEEN 1 AND 10
    ),
    max_duration_s REAL NOT NULL CHECK (max_duration_s > 0),
    require_all_steps_succeeded INTEGER NOT NULL CHECK (
        require_all_steps_succeeded = 1
    ),
    require_output_isolated INTEGER NOT NULL CHECK (require_output_isolated = 1),
    require_reset_acknowledged INTEGER NOT NULL CHECK (
        require_reset_acknowledged IN (0, 1)
    ),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, version),
    FOREIGN KEY (task_definition_id, task_version)
        REFERENCES task_workflow_publications(task_definition_id, task_version)
        ON DELETE RESTRICT
);

CREATE TRIGGER IF NOT EXISTS workflow_simulation_validation_profiles_no_update
BEFORE UPDATE ON workflow_simulation_validation_profiles
BEGIN
    SELECT RAISE(ABORT, 'Simulation Validation Profiles are immutable');
END;

CREATE TRIGGER IF NOT EXISTS workflow_simulation_validation_profiles_no_delete
BEFORE DELETE ON workflow_simulation_validation_profiles
BEGIN
    SELECT RAISE(ABORT, 'Simulation Validation Profiles are immutable');
END;

CREATE INDEX IF NOT EXISTS workflow_simulation_validation_profiles_task
    ON workflow_simulation_validation_profiles(
        task_definition_id, task_version, created_at DESC
    );

CREATE TABLE IF NOT EXISTS workflow_simulation_run_validations (
    run_id TEXT PRIMARY KEY REFERENCES workflow_simulation_runs(run_id)
        ON DELETE RESTRICT,
    profile_id TEXT NOT NULL,
    profile_version INTEGER NOT NULL CHECK (profile_version >= 1),
    state TEXT NOT NULL CHECK (
        state IN ('PENDING', 'AWAITING_RESET', 'PASS', 'FAIL')
    ),
    checks_json TEXT NOT NULL,
    evaluated_at TEXT,
    FOREIGN KEY (profile_id, profile_version)
        REFERENCES workflow_simulation_validation_profiles(profile_id, version)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS workflow_simulation_run_validations_profile
    ON workflow_simulation_run_validations(profile_id, profile_version, state);

CREATE TABLE IF NOT EXISTS workflow_commissioning_candidates (
    candidate_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    profile_version INTEGER NOT NULL CHECK (profile_version >= 1),
    task_definition_id TEXT NOT NULL,
    task_version INTEGER NOT NULL CHECK (task_version >= 1),
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL CHECK (workflow_version >= 1),
    workflow_sha256 TEXT NOT NULL CHECK (length(workflow_sha256) = 64),
    evidence_run_ids_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state = 'SIMULATION_APPROVED'),
    created_at TEXT NOT NULL,
    UNIQUE (principal_id, request_id),
    UNIQUE (profile_id, profile_version),
    FOREIGN KEY (profile_id, profile_version)
        REFERENCES workflow_simulation_validation_profiles(profile_id, version)
        ON DELETE RESTRICT,
    FOREIGN KEY (task_definition_id, task_version)
        REFERENCES task_workflow_publications(task_definition_id, task_version)
        ON DELETE RESTRICT
);

CREATE TRIGGER IF NOT EXISTS workflow_commissioning_candidates_no_update
BEFORE UPDATE ON workflow_commissioning_candidates
BEGIN
    SELECT RAISE(ABORT, 'Commissioning candidates are immutable');
END;

CREATE TRIGGER IF NOT EXISTS workflow_commissioning_candidates_no_delete
BEFORE DELETE ON workflow_commissioning_candidates
BEGIN
    SELECT RAISE(ABORT, 'Commissioning candidates are immutable');
END;
"""


class WorkflowAuthoringConflict(ValueError):
    """A revision, immutable identity, request, or active adapter conflicts."""


class WorkflowAuthoringNotFound(ValueError):
    """A requested Draft, Task, publication, Worker, or Run does not exist."""


class WorkflowAuthoringInvalid(ValueError):
    """An authoring payload cannot compile to strict Task/Workflow contracts."""


class WorkflowAuthoringUnavailable(ValueError):
    """A configured, fresh, output-isolated simulation Worker is unavailable."""


class WorkflowDraftRoleV1(FleetModel):
    role: SafeId
    asset_type: AssetType
    access_mode: AccessMode = "exclusive"
    cardinality: Literal["1"] = "1"
    requires: list[CapabilityRef] = Field(default_factory=list, max_length=256)

    @field_validator("requires")
    @classmethod
    def capabilities_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("role capabilities must be unique")
        return value


class WorkflowDraftCreateV1(FleetModel):
    request_id: SafeId
    task_definition_id: SafeId
    workflow_id: SafeId | None = None
    display_name: ShortText
    description: str | None = Field(default=None, max_length=4096)


class WorkflowDraftReplaceV1(FleetModel):
    request_id: SafeId
    expected_revision: int = Field(ge=1)
    display_name: ShortText
    description: str | None = Field(default=None, max_length=4096)
    roles: list[WorkflowDraftRoleV1] = Field(default_factory=list, max_length=64)
    steps: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=512)

    @model_validator(mode="after")
    def role_names_are_unique(self) -> "WorkflowDraftReplaceV1":
        names = [role.role for role in self.roles]
        if len(names) != len(set(names)):
            raise ValueError("Draft role names must be unique")
        _validate_draft_steps(self.steps)
        return self


class WorkflowDraftCloneV1(FleetModel):
    request_id: SafeId
    source_task_definition_id: SafeId
    source_task_version: int = Field(ge=1)
    mode: Literal["new_task", "new_version"]
    target_task_definition_id: SafeId | None = None
    target_workflow_id: SafeId | None = None
    display_name: ShortText | None = None

    @model_validator(mode="after")
    def target_matches_mode(self) -> "WorkflowDraftCloneV1":
        if self.mode == "new_task" and self.target_task_definition_id is None:
            raise ValueError("new_task clone requires target_task_definition_id")
        if self.mode == "new_version" and (
            self.target_task_definition_id is not None
            or self.target_workflow_id is not None
        ):
            raise ValueError("new_version clone keeps the source Task and Workflow IDs")
        return self


class WorkflowPublishV1(FleetModel):
    request_id: SafeId
    expected_revision: int = Field(ge=1)


class WorkflowSimulationQueueV1(FleetModel):
    request_id: SafeId
    task_definition_id: SafeId
    task_version: int = Field(ge=1)
    adapter_id: SafeId
    display_name: ShortText
    validation_profile_id: SafeId | None = None
    validation_profile_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validation_profile_identity_is_complete(self) -> "WorkflowSimulationQueueV1":
        if (self.validation_profile_id is None) != (
            self.validation_profile_version is None
        ):
            raise ValueError(
                "validation profile ID and version must be supplied together"
            )
        return self


class WorkflowSimulationControlV1(FleetModel):
    request_id: SafeId
    action: Literal["HOLD", "RESUME", "ABORT", "RESET"]


class SimulationValidationProfileCreateV1(FleetModel):
    request_id: SafeId
    profile_id: SafeId
    display_name: ShortText
    description: str | None = Field(default=None, max_length=4096)
    task_definition_id: SafeId
    task_version: int = Field(ge=1)
    adapter_id: SafeId
    required_successful_runs: int = Field(default=1, ge=1, le=10)
    max_duration_s: float = Field(default=600.0, gt=0.0, le=86_400.0)
    require_all_steps_succeeded: Literal[True] = True
    require_output_isolated: Literal[True] = True
    require_reset_acknowledged: bool = True


class CommissioningCandidateCreateV1(FleetModel):
    request_id: SafeId
    profile_id: SafeId
    profile_version: int = Field(ge=1)


class UnityAdapterRoleV1(FleetModel):
    role: SafeId
    access: AccessMode
    requires: list[CapabilityRef] = Field(min_length=1, max_length=256)


class UnityAdapterStepV1(FleetModel):
    step_id: SafeId
    role: SafeId
    capability: CapabilityRef
    depends_on: list[SafeId] = Field(default_factory=list, max_length=512)
    parameters: dict[str, JsonValue] = Field(default_factory=dict, max_length=256)


class UnityAdapterProfileV1(FleetModel):
    schema_version: Literal[1] = 1
    adapter_id: SafeId
    scenario_id: SafeId
    bridge_root: str = Field(min_length=1, max_length=2048)
    poll_interval_seconds: float = Field(gt=0.0, le=10.0)
    roles: list[UnityAdapterRoleV1] = Field(min_length=1, max_length=64)
    steps: list[UnityAdapterStepV1] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def identities_are_unique(self) -> "UnityAdapterProfileV1":
        role_ids = [role.role for role in self.roles]
        step_ids = [step.step_id for step in self.steps]
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("adapter role IDs must be unique")
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("adapter step IDs must be unique")
        if any(step.role not in set(role_ids) for step in self.steps):
            raise ValueError("adapter step references an unknown role")
        return self


class UnityAdapterProfileV2(UnityAdapterProfileV1):
    schema_version: Literal[2] = 2
    dynamic_binding: UnityDynamicBindingV1


class IsaacAdapterProfileV3(UnityAdapterProfileV1):
    """Fixed Isaac workflows; existing profile serialization stays unchanged."""

    schema_version: Literal[3] = 3
    execution_runtime: Literal["isaac"]
    profile_id: Literal["rb5", "omy", "scenario1"]
    scene_sha256: Sha256Digest

    @model_validator(mode="after")
    def bounded_robot_pilot(self) -> "IsaacAdapterProfileV3":
        root = Path(self.bridge_root)
        if (not root.is_absolute() or root.resolve().parent != Path("/tmp")
                or not root.resolve().name.startswith("sang-isaac-")):
            raise ValueError("Isaac bridge root must remain below /tmp/sang-isaac-*")
        if self.profile_id == "scenario1":
            if (self.adapter_id != "isaac-scenario1-v1" or self.scenario_id != "isaac-scenario1"
                    or self.bridge_root != "/tmp/sang-isaac-scenario1"
                    or root.resolve() != root):
                raise ValueError("Isaac Scenario 1 requires its fixed adapter/scenario/root identity")
            expected_roles = [
                {"role": role, "access": "exclusive",
                 "requires": list(dict.fromkeys(capability for _, step_role, capability, _
                                                in ISAAC_SCENARIO1_STEPS if step_role == role))}
                for role in ISAAC_SCENARIO1_BINDINGS
            ]
            expected_steps = [
                {"step_id": step, "role": role, "capability": capability,
                 "depends_on": [] if index == 0 else [ISAAC_SCENARIO1_STEPS[index - 1][0]],
                 "parameters": {"operation": operation}}
                for index, (step, role, capability, operation) in enumerate(ISAAC_SCENARIO1_STEPS)
            ]
            if ([role.model_dump(mode="json") for role in self.roles] != expected_roles
                    or [step.model_dump(mode="json") for step in self.steps] != expected_steps):
                raise ValueError("Isaac Scenario 1 requires its five fixed operations; no editable inputs")
            return self
        expected_roles = [{
            "role": "virtual-robot", "access": "exclusive",
            "requires": ["simulate-bounded-joint@1"],
        }]
        expected_steps = [
            {"step_id": "move_offset", "role": "virtual-robot",
             "capability": "simulate-bounded-joint@1", "depends_on": [],
             "parameters": {"operation": "move-offset"}},
            {"step_id": "return_ready", "role": "virtual-robot",
             "capability": "simulate-bounded-joint@1", "depends_on": ["move_offset"],
             "parameters": {"operation": "return-ready"}},
        ]
        if ([role.model_dump(mode="json") for role in self.roles] != expected_roles
                or [step.model_dump(mode="json") for step in self.steps] != expected_steps):
            raise ValueError("Isaac pilot requires its two fixed robot operations; no editable inputs")
        return self


def load_unity_adapter_profile(path: str | Path) -> tuple[UnityAdapterProfileV1, str]:
    """Load installed simulation profiles; the legacy entry point remains compatible."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    model = {2: UnityAdapterProfileV2, 3: IsaacAdapterProfileV3}.get(
        raw.get("schema_version"), UnityAdapterProfileV1
    )
    profile = model.model_validate(raw)
    digest = payload_digest(profile.model_dump(mode="json"))
    return profile, digest


def _validate_draft_steps(steps: list[dict[str, JsonValue]]) -> None:
    identities: list[str] = []
    for index, step in enumerate(steps):
        step_id = step.get("step_id")
        kind = step.get("kind")
        if not isinstance(step_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", step_id
        ):
            raise ValueError(f"Draft step {index} requires a valid step_id")
        if not isinstance(kind, str):
            raise ValueError(f"Draft step {step_id} requires kind")
        identities.append(step_id)
        _scan_authoring_value(step)
    if len(identities) != len(set(identities)):
        raise ValueError("Draft step IDs must be unique")


def _singular_key_part(part: str) -> str:
    if part.endswith("ies") and len(part) > 3:
        return f"{part[:-3]}y"
    if part.endswith(("sses", "xes", "zes", "ches", "shes")):
        return part[:-2]
    if part.endswith("s") and not part.endswith("ss"):
        return part[:-1]
    return part


def _scan_authoring_value(value: JsonValue, *, depth: int = 0, path: str = "steps") -> None:
    if depth > MAX_AUTHORING_JSON_DEPTH:
        raise WorkflowAuthoringInvalid(
            f"{path} exceeds maximum JSON depth {MAX_AUTHORING_JSON_DEPTH}"
        )
    if isinstance(value, dict):
        for key, child in value.items():
            acronym_split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)
            camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", acronym_split)
            normalized = re.sub(r"[^A-Za-z0-9]+", "_", camel_split).strip("_").lower()
            parts = normalized.split("_")
            singular_parts = {_singular_key_part(part) for part in parts}
            normalized_variants = {
                normalized,
                "_".join(_singular_key_part(part) for part in parts),
            }
            if (
                not key.isascii()
                or not normalized
                or _SENSITIVE_KEY_PARTS.intersection(singular_parts)
                or any(
                    candidate in _SENSITIVE_KEYS
                    or any(
                        f"_{name}_" in f"_{candidate}_"
                        for name in _SENSITIVE_KEYS
                    )
                    or any(
                        compact in candidate.replace("_", "")
                        for compact in _COMPACT_SENSITIVE_KEYS
                    )
                    for candidate in normalized_variants
                )
            ):
                raise WorkflowAuthoringInvalid(
                    f"{path} contains forbidden field {key!r}"
                )
            _scan_authoring_value(child, depth=depth + 1, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_authoring_value(child, depth=depth + 1, path=f"{path}[{index}]")
    elif isinstance(value, str):
        if re.fullmatch(r"[\\/]+", value.strip()):
            raise WorkflowAuthoringInvalid(
                f"{path} contains an absolute topic or path"
            )
        if re.search(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])", value):
            raise WorkflowAuthoringInvalid(f"{path} contains a network address")
        candidates = re.split(r"[\s,;=]+", value)
        candidates.extend(
            match.group()
            for match in re.finditer(r"[0-9A-Fa-f:]{3,}", value)
            if match.group().count(":") >= 2
        )
        for candidate in candidates:
            candidate = candidate.strip("[](){}<>.,!?\"'")
            variants = [candidate]
            if candidate.startswith(":") and not candidate.startswith("::"):
                variants.append(candidate[1:])
            if candidate.endswith(":") and not candidate.endswith("::"):
                variants.append(candidate[:-1])
            for variant in variants:
                try:
                    address = ipaddress.ip_address(variant)
                except ValueError:
                    continue
                if address.version == 6:
                    raise WorkflowAuthoringInvalid(
                        f"{path} contains a network address"
                    )
        for match in _URI_SCHEME_PATTERN.finditer(value):
            if match.group(1).lower() not in _ALLOWED_REFERENCE_SCHEMES:
                raise WorkflowAuthoringInvalid(f"{path} contains a deployment URI")
        reference_free = re.sub(
            r"(?i)\b(?:artifact|contracts|registry|workcell)://[^\s<>\"']+",
            " ",
            value,
        )
        if re.search(
            r"(?<![A-Za-z0-9._~-])/(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*/?)",
            reference_free,
        ) or re.search(
            r"(?:[A-Za-z]:[\\/]|\\\\[A-Za-z0-9._-]+[\\/])",
            reference_free,
        ):
            raise WorkflowAuthoringInvalid(
                f"{path} contains an absolute topic or path"
            )


def _validation_issues(error: ValidationError | ValueError) -> list[dict[str, Any]]:
    if isinstance(error, ValidationError):
        return [
            {
                "location": [str(item) for item in issue["loc"]],
                "message": issue["msg"],
                "type": issue["type"],
            }
            for issue in error.errors(include_url=False)
        ]
    return [{"location": [], "message": str(error), "type": "value_error"}]


class WorkflowAuthoringStore:
    def __init__(
        self,
        path: str | Path,
        *,
        adapter_profile_paths: list[str | Path] | None = None,
        skill_catalog_paths: list[str | Path] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.adapter_profiles: dict[str, tuple[UnityAdapterProfileV1, str]] = {}
        for profile_path in adapter_profile_paths or []:
            profile, digest = load_unity_adapter_profile(profile_path)
            if profile.adapter_id in self.adapter_profiles:
                raise ValueError(f"duplicate simulation adapter ID: {profile.adapter_id}")
            self.adapter_profiles[profile.adapter_id] = (profile, digest)
        self.skill_definitions: dict[str, SkillDefinitionV1] = {}
        self.action_definitions: dict[str, RobotActionDefinitionV1] = {}
        self.skill_catalog_digests: dict[str, str] = {}
        for catalog_path in skill_catalog_paths or []:
            catalog, digest = load_skill_catalog(catalog_path)
            catalog_ref = f"{catalog.catalog_id}@{catalog.version}"
            if catalog_ref in self.skill_catalog_digests:
                raise ValueError(f"duplicate Skill catalog: {catalog_ref}")
            self.skill_catalog_digests[catalog_ref] = digest
            for definition in catalog.skills:
                if definition.capability in self.skill_definitions:
                    raise ValueError(
                        f"duplicate Skill capability: {definition.capability}"
                    )
                self.skill_definitions[definition.capability] = definition
            for action in catalog.actions:
                if action.action_ref in self.action_definitions:
                    raise ValueError(
                        f"duplicate Robot action reference: {action.action_ref}"
                    )
                self.action_definitions[action.action_ref] = action
        with self._connect() as connection:
            migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (WORKFLOW_AUTHORING_SCHEMA_VERSION,),
            ).fetchone()
            if migration is None:
                raise RuntimeError(
                    "Workflow authoring requires fleet schema migration v19"
                )
            connection.executescript(WORKFLOW_AUTHORING_MIGRATION)
        self.action_library = ActionLibraryStore(
            self.path,
            skill_definitions=self.skill_definitions,
            builtin_actions=self.action_definitions,
            adapter_profiles=self.adapter_profiles,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _request_digest(operation: str, payload: dict[str, Any]) -> str:
        return payload_digest({"operation": operation, "payload": payload})

    def _replay(
        self,
        connection: sqlite3.Connection,
        *,
        principal_id: str,
        request_id: str,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        digest = self._request_digest(operation, payload)
        row = connection.execute(
            """
            SELECT * FROM workflow_authoring_requests
            WHERE principal_id = ? AND request_id = ?
            """,
            (principal_id, request_id),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_sha256"] != digest:
            raise WorkflowAuthoringConflict(
                "request_id was already used with a different operation or payload"
            )
        return {**json.loads(row["response_json"]), "idempotent_replay": True}

    def _record_request(
        self,
        connection: sqlite3.Connection,
        *,
        principal_id: str,
        request_id: str,
        operation: str,
        payload: dict[str, Any],
        response: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO workflow_authoring_requests (
                principal_id, request_id, operation, request_sha256,
                response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                principal_id,
                request_id,
                operation,
                self._request_digest(operation, payload),
                canonical_json(response),
                isoformat_z(occurred_at),
            ),
        )

    @staticmethod
    def _draft(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "draft_id": row["draft_id"],
            "task_definition_id": row["task_definition_id"],
            "task_version": row["task_version"],
            "workflow_id": row["workflow_id"],
            "workflow_version": row["workflow_version"],
            "display_name": row["display_name"],
            "description": row["description"],
            "roles": json.loads(row["roles_json"]),
            "steps": json.loads(row["steps_json"]),
            "source_task_definition_id": row["source_task_definition_id"],
            "source_task_version": row["source_task_version"],
            "source_workflow_id": row["source_workflow_id"],
            "source_workflow_version": row["source_workflow_version"],
            "state": row["state"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "published_at": row["published_at"],
        }

    @staticmethod
    def _validation_profile(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "profile_id": row["profile_id"],
            "version": row["version"],
            "display_name": row["display_name"],
            "description": row["description"],
            "task_definition_id": row["task_definition_id"],
            "task_version": row["task_version"],
            "adapter_id": row["adapter_id"],
            "scenario_id": row["scenario_id"],
            "adapter_profile_sha256": row["adapter_profile_sha256"],
            "required_successful_runs": row["required_successful_runs"],
            "max_duration_s": row["max_duration_s"],
            "require_all_steps_succeeded": bool(
                row["require_all_steps_succeeded"]
            ),
            "require_output_isolated": bool(row["require_output_isolated"]),
            "require_reset_acknowledged": bool(
                row["require_reset_acknowledged"]
            ),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _validation_report(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "profile_id": row["profile_id"],
            "profile_version": row["profile_version"],
            "state": row["state"],
            "checks": json.loads(row["checks_json"]),
            "evaluated_at": row["evaluated_at"],
        }

    @staticmethod
    def _commissioning_candidate(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "candidate_id": row["candidate_id"],
            "profile_id": row["profile_id"],
            "profile_version": row["profile_version"],
            "task_definition_id": row["task_definition_id"],
            "task_version": row["task_version"],
            "workflow_id": row["workflow_id"],
            "workflow_version": row["workflow_version"],
            "workflow_sha256": row["workflow_sha256"],
            "evidence_run_ids": json.loads(row["evidence_run_ids_json"]),
            "state": row["state"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _latest_task_version(connection: sqlite3.Connection, task_id: str) -> int:
        row = connection.execute(
            "SELECT MAX(version) AS version FROM task_definitions WHERE task_definition_id = ?",
            (task_id,),
        ).fetchone()
        return 0 if row["version"] is None else int(row["version"])

    @staticmethod
    def _latest_workflow_version(connection: sqlite3.Connection, workflow_id: str) -> int:
        row = connection.execute(
            """
            SELECT MAX(version) AS version FROM orchestration_registry
            WHERE object_kind = 'workflow' AND object_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        return 0 if row["version"] is None else int(row["version"])

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        occurred_at: datetime,
        action: str,
        entity_type: str,
        entity_id: str,
        details: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO configuration_audit (
                occurred_at, action, entity_type, entity_id, details_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                isoformat_z(occurred_at),
                action,
                entity_type,
                entity_id,
                canonical_json(details),
            ),
        )

    def _insert_draft(
        self,
        connection: sqlite3.Connection,
        *,
        task_definition_id: str,
        task_version: int,
        workflow_id: str,
        workflow_version: int,
        display_name: str,
        description: str | None,
        roles: list[dict[str, Any]],
        steps: list[dict[str, Any]],
        source_task_definition_id: str | None,
        source_task_version: int | None,
        source_workflow_id: str | None,
        source_workflow_version: int | None,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        if description:
            _scan_authoring_value(description, path="description")
        if steps:
            _validate_draft_steps(steps)
        draft_id = f"draft-{uuid4().hex}"
        timestamp = isoformat_z(occurred_at)
        connection.execute(
            """
            INSERT INTO workflow_drafts (
                draft_id, task_definition_id, task_version, workflow_id,
                workflow_version, display_name, description, roles_json,
                steps_json, source_task_definition_id, source_task_version,
                source_workflow_id, source_workflow_version, state, revision,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DRAFT', 1, ?, ?)
            """,
            (
                draft_id,
                task_definition_id,
                task_version,
                workflow_id,
                workflow_version,
                display_name,
                description,
                canonical_json(roles),
                canonical_json(steps),
                source_task_definition_id,
                source_task_version,
                source_workflow_id,
                source_workflow_version,
                timestamp,
                timestamp,
            ),
        )
        row = connection.execute(
            "SELECT * FROM workflow_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        return self._draft(row)

    def create_draft(
        self,
        body: WorkflowDraftCreateV1,
        *,
        principal_id: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json", exclude={"request_id"})
        workflow_id = body.workflow_id or body.task_definition_id
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection,
                principal_id=principal_id,
                request_id=body.request_id,
                operation="create_draft",
                payload=payload,
            )
            if replay is not None:
                return replay
            draft = self._insert_draft(
                connection,
                task_definition_id=body.task_definition_id,
                task_version=self._latest_task_version(
                    connection, body.task_definition_id
                )
                + 1,
                workflow_id=workflow_id,
                workflow_version=self._latest_workflow_version(connection, workflow_id)
                + 1,
                display_name=body.display_name,
                description=body.description,
                roles=[],
                steps=[],
                source_task_definition_id=None,
                source_task_version=None,
                source_workflow_id=None,
                source_workflow_version=None,
                occurred_at=occurred_at,
            )
            response = {**draft, "idempotent_replay": False}
            self._audit(
                connection,
                occurred_at=occurred_at,
                action="create_draft",
                entity_type="workflow_draft",
                entity_id=draft["draft_id"],
                details={
                    "task_definition_id": draft["task_definition_id"],
                    "task_version": draft["task_version"],
                    "workflow_id": draft["workflow_id"],
                    "workflow_version": draft["workflow_version"],
                },
            )
            self._record_request(
                connection,
                principal_id=principal_id,
                request_id=body.request_id,
                operation="create_draft",
                payload=payload,
                response=response,
                occurred_at=occurred_at,
            )
            return response

    def update_draft(
        self,
        draft_id: str,
        body: WorkflowDraftReplaceV1,
        *,
        principal_id: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        if body.description:
            _scan_authoring_value(body.description, path="description")
        payload = {
            "draft_id": draft_id,
            **body.model_dump(mode="json", exclude={"request_id"}),
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection,
                principal_id=principal_id,
                request_id=body.request_id,
                operation="update_draft",
                payload=payload,
            )
            if replay is not None:
                return replay
            row = connection.execute(
                "SELECT * FROM workflow_drafts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                raise WorkflowAuthoringNotFound(f"unknown Workflow Draft: {draft_id}")
            if row["state"] != "DRAFT":
                raise WorkflowAuthoringConflict("published Drafts are immutable")
            if row["revision"] != body.expected_revision:
                raise WorkflowAuthoringConflict(
                    f"Draft revision is {row['revision']}, not {body.expected_revision}"
                )
            connection.execute(
                """
                UPDATE workflow_drafts
                SET display_name = ?, description = ?, roles_json = ?, steps_json = ?,
                    revision = revision + 1, updated_at = ?
                WHERE draft_id = ?
                """,
                (
                    body.display_name,
                    body.description,
                    canonical_json([role.model_dump(mode="json") for role in body.roles]),
                    canonical_json(body.steps),
                    isoformat_z(occurred_at),
                    draft_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM workflow_drafts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            response = {**self._draft(updated), "idempotent_replay": False}
            self._audit(
                connection,
                occurred_at=occurred_at,
                action="update_draft",
                entity_type="workflow_draft",
                entity_id=draft_id,
                details={"revision": response["revision"]},
            )
            self._record_request(
                connection,
                principal_id=principal_id,
                request_id=body.request_id,
                operation="update_draft",
                payload=payload,
                response=response,
                occurred_at=occurred_at,
            )
            return response

    def clone_draft(
        self,
        body: WorkflowDraftCloneV1,
        *,
        principal_id: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json", exclude={"request_id"})
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection,
                principal_id=principal_id,
                request_id=body.request_id,
                operation="clone_draft",
                payload=payload,
            )
            if replay is not None:
                return replay
            task = connection.execute(
                """
                SELECT * FROM task_definitions
                WHERE task_definition_id = ? AND version = ?
                """,
                (body.source_task_definition_id, body.source_task_version),
            ).fetchone()
            if task is None:
                raise WorkflowAuthoringNotFound(
                    "unknown source Task Definition: "
                    f"{body.source_task_definition_id}@{body.source_task_version}"
                )
            publication = connection.execute(
                """
                SELECT * FROM task_workflow_publications
                WHERE task_definition_id = ? AND task_version = ?
                """,
                (body.source_task_definition_id, body.source_task_version),
            ).fetchone()
            workflow_payload = None
            if publication is not None:
                workflow_row = connection.execute(
                    """
                    SELECT payload_json FROM orchestration_registry
                    WHERE object_kind = 'workflow' AND object_id = ? AND version = ?
                    """,
                    (publication["workflow_id"], publication["workflow_version"]),
                ).fetchone()
                if workflow_row is None:
                    raise WorkflowAuthoringInvalid(
                        "source publication references a missing Workflow"
                    )
                workflow_payload = json.loads(workflow_row["payload_json"])

            if body.mode == "new_version":
                target_task_id = body.source_task_definition_id
                target_workflow_id = (
                    publication["workflow_id"]
                    if publication is not None
                    else body.source_task_definition_id
                )
            else:
                target_task_id = body.target_task_definition_id
                target_workflow_id = body.target_workflow_id or target_task_id
                if target_task_id == body.source_task_definition_id:
                    raise WorkflowAuthoringInvalid(
                        "new_task clone requires a different target Task ID"
                    )
                if self._latest_task_version(connection, target_task_id) != 0:
                    raise WorkflowAuthoringConflict(
                        f"target Task ID already exists: {target_task_id}"
                    )
                if self._latest_workflow_version(connection, target_workflow_id) != 0:
                    raise WorkflowAuthoringConflict(
                        f"target Workflow ID already exists: {target_workflow_id}"
                    )

            task_roles = json.loads(task["roles_json"])
            workflow_roles = {
                role["role"]: role for role in (workflow_payload or {}).get("roles", [])
            }
            draft_roles = []
            for role in task_roles:
                executable = workflow_roles.get(role["role"])
                draft_roles.append(
                    WorkflowDraftRoleV1(
                        role=role["role"],
                        asset_type=role["asset_type"],
                        access_mode=role["access_mode"],
                        requires=(
                            []
                            if executable is None
                            else executable["selector"]["requires"]
                        ),
                    ).model_dump(mode="json")
                )
            draft = self._insert_draft(
                connection,
                task_definition_id=target_task_id,
                task_version=self._latest_task_version(connection, target_task_id) + 1,
                workflow_id=target_workflow_id,
                workflow_version=self._latest_workflow_version(
                    connection, target_workflow_id
                )
                + 1,
                display_name=body.display_name or task["display_name"],
                description=task["description"],
                roles=draft_roles,
                steps=[] if workflow_payload is None else workflow_payload["steps"],
                source_task_definition_id=body.source_task_definition_id,
                source_task_version=body.source_task_version,
                source_workflow_id=(
                    None if publication is None else publication["workflow_id"]
                ),
                source_workflow_version=(
                    None if publication is None else publication["workflow_version"]
                ),
                occurred_at=occurred_at,
            )
            response = {**draft, "idempotent_replay": False}
            self._audit(
                connection,
                occurred_at=occurred_at,
                action="clone_draft",
                entity_type="workflow_draft",
                entity_id=draft["draft_id"],
                details={
                    "mode": body.mode,
                    "source_task": (
                        f"{body.source_task_definition_id}@{body.source_task_version}"
                    ),
                },
            )
            self._record_request(
                connection,
                principal_id=principal_id,
                request_id=body.request_id,
                operation="clone_draft",
                payload=payload,
                response=response,
                occurred_at=occurred_at,
            )
            return response

    def _compile(self, draft: dict[str, Any]) -> tuple[TaskDefinitionCreate, WorkflowDefinitionV2]:
        roles = [WorkflowDraftRoleV1.model_validate(role) for role in draft["roles"]]
        if not roles:
            raise WorkflowAuthoringInvalid("Draft requires at least one role")
        for role in roles:
            if not role.requires:
                raise WorkflowAuthoringInvalid(
                    f"role {role.role} requires at least one capability"
                )
        if not draft["steps"]:
            raise WorkflowAuthoringInvalid("Draft requires at least one Workflow step")
        _validate_draft_steps(draft["steps"])
        task = TaskDefinitionCreate(
            task_definition_id=draft["task_definition_id"],
            version=draft["task_version"],
            display_name=draft["display_name"],
            description=draft["description"],
            roles=[
                TaskRoleCreate(
                    role=role.role,
                    asset_type=role.asset_type,
                    access_mode=role.access_mode,
                )
                for role in roles
            ],
        )
        workflow = WorkflowDefinitionV2.model_validate(
            {
                "schema_version": 2,
                "workflow_id": draft["workflow_id"],
                "version": draft["workflow_version"],
                "roles": [
                    WorkflowRoleV2(
                        role=role.role,
                        cardinality="1",
                        selector={"requires": role.requires},
                        access=role.access_mode,
                    ).model_dump(mode="json")
                    for role in roles
                ],
                "steps": draft["steps"],
            }
        )
        for step in workflow.steps:
            if step.kind != "action":
                continue
            definition = self.skill_definitions.get(step.capability)
            if definition is None:
                continue
            try:
                validate_skill_parameters(definition, step.parameters)
            except ValueError as exc:
                raise WorkflowAuthoringInvalid(
                    f"step {step.step_id}: {exc}"
                ) from exc
        return task, workflow

    def list_skill_definitions(self) -> list[dict[str, Any]]:
        return [
            definition.model_dump(mode="json", by_alias=True)
            for _, definition in sorted(self.skill_definitions.items())
        ]

    def list_action_definitions(self) -> list[dict[str, Any]]:
        definitions = {
            **self.action_definitions,
            **self.action_library.published_definitions(),
        }
        return [
            definition.model_dump(mode="json", by_alias=True)
            for _, definition in sorted(definitions.items())
        ]

    def validate_draft(self, draft_id: str) -> dict[str, Any]:
        draft = self.get_draft(draft_id)
        try:
            _, workflow = self._compile(draft)
        except (ValidationError, ValueError) as exc:
            return {"draft_id": draft_id, "valid": False, "issues": _validation_issues(exc)}
        return {
            "draft_id": draft_id,
            "valid": True,
            "issues": [],
            "workflow_sha256": payload_digest(workflow.model_dump(mode="json")),
        }

    def publish(
        self,
        draft_id: str,
        body: WorkflowPublishV1,
        *,
        principal_id: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        payload = {
            "draft_id": draft_id,
            **body.model_dump(mode="json", exclude={"request_id"}),
        }
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = self._replay(
                    connection,
                    principal_id=principal_id,
                    request_id=body.request_id,
                    operation="publish_draft",
                    payload=payload,
                )
                if replay is not None:
                    return replay
                row = connection.execute(
                    "SELECT * FROM workflow_drafts WHERE draft_id = ?", (draft_id,)
                ).fetchone()
                if row is None:
                    raise WorkflowAuthoringNotFound(
                        f"unknown Workflow Draft: {draft_id}"
                    )
                draft = self._draft(row)
                if row["state"] != "DRAFT":
                    raise WorkflowAuthoringConflict("published Drafts are immutable")
                if row["revision"] != body.expected_revision:
                    raise WorkflowAuthoringConflict(
                        f"Draft revision is {row['revision']}, not {body.expected_revision}"
                    )
                try:
                    task, workflow = self._compile(draft)
                except (ValidationError, ValueError) as exc:
                    issues = _validation_issues(exc)
                    raise WorkflowAuthoringInvalid(
                        "; ".join(issue["message"] for issue in issues)
                    ) from exc
                expected_task = self._latest_task_version(
                    connection, task.task_definition_id
                ) + 1
                expected_workflow = self._latest_workflow_version(
                    connection, workflow.workflow_id
                ) + 1
                if task.version != expected_task:
                    raise WorkflowAuthoringConflict(
                        f"next Task version for {task.task_definition_id} is {expected_task}"
                    )
                if workflow.version != expected_workflow:
                    raise WorkflowAuthoringConflict(
                        f"next Workflow version for {workflow.workflow_id} is {expected_workflow}"
                    )
                timestamp = isoformat_z(occurred_at)
                task_roles = [role.model_dump(mode="json") for role in task.roles]
                connection.execute(
                    """
                    INSERT INTO task_definitions (
                        task_definition_id, version, display_name, description,
                        roles_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_definition_id,
                        task.version,
                        task.display_name,
                        task.description,
                        canonical_json(task_roles),
                        timestamp,
                    ),
                )
                workflow_payload = workflow.model_dump(mode="json")
                workflow_json = canonical_json(workflow_payload)
                workflow_sha256 = payload_digest(workflow_payload)
                connection.execute(
                    """
                    INSERT INTO orchestration_registry (
                        object_kind, object_id, version, schema_version,
                        approval_state, payload_sha256, payload_json, created_at
                    ) VALUES ('workflow', ?, ?, 2, 'not_applicable', ?, ?, ?)
                    """,
                    (
                        workflow.workflow_id,
                        workflow.version,
                        workflow_sha256,
                        workflow_json,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO task_workflow_publications (
                        task_definition_id, task_version, workflow_id,
                        workflow_version, workflow_sha256, source_draft_id,
                        published_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_definition_id,
                        task.version,
                        workflow.workflow_id,
                        workflow.version,
                        workflow_sha256,
                        draft_id,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE workflow_drafts
                    SET state = 'PUBLISHED', revision = revision + 1,
                        updated_at = ?, published_at = ?
                    WHERE draft_id = ?
                    """,
                    (timestamp, timestamp, draft_id),
                )
                self._audit(
                    connection,
                    occurred_at=occurred_at,
                    action="publish",
                    entity_type="task_workflow_publication",
                    entity_id=f"{task.task_definition_id}@{task.version}",
                    details={
                        "workflow_id": workflow.workflow_id,
                        "workflow_version": workflow.version,
                        "workflow_sha256": workflow_sha256,
                        "source_draft_id": draft_id,
                    },
                )
                response = {
                    "task_definition_id": task.task_definition_id,
                    "task_version": task.version,
                    "workflow_id": workflow.workflow_id,
                    "workflow_version": workflow.version,
                    "workflow_sha256": workflow_sha256,
                    "source_draft_id": draft_id,
                    "published_at": timestamp,
                    "idempotent_replay": False,
                }
                self._record_request(
                    connection,
                    principal_id=principal_id,
                    request_id=body.request_id,
                    operation="publish_draft",
                    payload=payload,
                    response=response,
                    occurred_at=occurred_at,
                )
                return response
        except sqlite3.IntegrityError as exc:
            raise WorkflowAuthoringConflict(str(exc)) from exc

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_drafts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        if row is None:
            raise WorkflowAuthoringNotFound(f"unknown Workflow Draft: {draft_id}")
        return self._draft(row)

    def list_drafts(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_drafts ORDER BY updated_at DESC, draft_id"
            ).fetchall()
        return [self._draft(row) for row in rows]

    @staticmethod
    def _publication(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_definition_id": row["task_definition_id"],
            "task_version": row["task_version"],
            "display_name": row["display_name"],
            "description": row["description"],
            "roles": json.loads(row["roles_json"]),
            "workflow_id": row["workflow_id"],
            "workflow_version": row["workflow_version"],
            "workflow_sha256": row["workflow_sha256"],
            "workflow": json.loads(row["payload_json"]),
            "source_draft_id": row["source_draft_id"],
            "published_at": row["published_at"],
        }

    def list_publications(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT publications.*, tasks.display_name, tasks.description,
                       tasks.roles_json, registry.payload_json
                FROM task_workflow_publications AS publications
                JOIN task_definitions AS tasks
                  ON tasks.task_definition_id = publications.task_definition_id
                 AND tasks.version = publications.task_version
                JOIN orchestration_registry AS registry
                  ON registry.object_kind = 'workflow'
                 AND registry.object_id = publications.workflow_id
                 AND registry.version = publications.workflow_version
                ORDER BY publications.published_at DESC
                """
            ).fetchall()
        return [self._publication(row) for row in rows]

    def create_validation_profile(
        self,
        body: SimulationValidationProfileCreateV1,
        *,
        principal_id: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json", exclude={"request_id"})
        now = parse_datetime(occurred_at)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = self._replay(
                    connection,
                    principal_id=principal_id,
                    request_id=body.request_id,
                    operation="create_simulation_validation_profile",
                    payload=payload,
                )
                if replay is not None:
                    return replay
                publication = self._publication_row(
                    connection, body.task_definition_id, body.task_version
                )
                adapter_entry = self.adapter_profiles.get(body.adapter_id)
                if adapter_entry is None:
                    raise WorkflowAuthoringInvalid(
                        f"simulation adapter is not configured: {body.adapter_id}"
                    )
                adapter, adapter_digest = adapter_entry
                workflow = WorkflowDefinitionV2.model_validate_json(
                    publication["payload_json"]
                )
                issues = self.compatibility_issues(workflow, adapter)
                if issues:
                    raise WorkflowAuthoringInvalid("; ".join(issues))
                version_row = connection.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) + 1 AS next_version
                    FROM workflow_simulation_validation_profiles
                    WHERE profile_id = ?
                    """,
                    (body.profile_id,),
                ).fetchone()
                version = int(version_row["next_version"])
                timestamp = isoformat_z(now)
                connection.execute(
                    """
                    INSERT INTO workflow_simulation_validation_profiles (
                        profile_id, version, display_name, description,
                        task_definition_id, task_version, adapter_id, scenario_id,
                        adapter_profile_sha256, required_successful_runs,
                        max_duration_s, require_all_steps_succeeded,
                        require_output_isolated, require_reset_acknowledged,
                        created_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)
                    """,
                    (
                        body.profile_id,
                        version,
                        body.display_name,
                        body.description,
                        publication["task_definition_id"],
                        publication["task_version"],
                        adapter.adapter_id,
                        adapter.scenario_id,
                        adapter_digest,
                        body.required_successful_runs,
                        body.max_duration_s,
                        int(body.require_reset_acknowledged),
                        principal_id,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM workflow_simulation_validation_profiles
                    WHERE profile_id = ? AND version = ?
                    """,
                    (body.profile_id, version),
                ).fetchone()
                response = {
                    **self._validation_profile(row),
                    "passing_run_count": 0,
                    "qualified": False,
                    "commissioning_candidate": None,
                    "idempotent_replay": False,
                }
                self._record_request(
                    connection,
                    principal_id=principal_id,
                    request_id=body.request_id,
                    operation="create_simulation_validation_profile",
                    payload=payload,
                    response=response,
                    occurred_at=now,
                )
                self._audit(
                    connection,
                    occurred_at=now,
                    action="simulation_validation_profile_created",
                    entity_type="simulation_validation_profile",
                    entity_id=f"{body.profile_id}@{version}",
                    details={
                        "task_definition_id": body.task_definition_id,
                        "task_version": body.task_version,
                        "adapter_id": body.adapter_id,
                    },
                )
                return response
        except sqlite3.IntegrityError as exc:
            raise WorkflowAuthoringConflict(str(exc)) from exc

    def list_validation_profiles(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            profiles = connection.execute(
                """
                SELECT * FROM workflow_simulation_validation_profiles
                ORDER BY created_at DESC, profile_id, version DESC
                """
            ).fetchall()
            candidates = {
                (row["profile_id"], row["profile_version"]):
                    self._commissioning_candidate(row)
                for row in connection.execute(
                    "SELECT * FROM workflow_commissioning_candidates"
                ).fetchall()
            }
            results = []
            for row in profiles:
                pass_row = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM workflow_simulation_run_validations
                    WHERE profile_id = ? AND profile_version = ? AND state = 'PASS'
                    """,
                    (row["profile_id"], row["version"]),
                ).fetchone()
                passing = int(pass_row["count"])
                profile = self._validation_profile(row)
                results.append(
                    {
                        **profile,
                        "passing_run_count": passing,
                        "qualified": passing >= profile["required_successful_runs"],
                        "commissioning_candidate": candidates.get(
                            (row["profile_id"], row["version"])
                        ),
                    }
                )
            return results

    def list_commissioning_candidates(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_commissioning_candidates
                ORDER BY created_at DESC, candidate_id
                """
            ).fetchall()
        return [self._commissioning_candidate(row) for row in rows]

    def create_commissioning_candidate(
        self,
        body: CommissioningCandidateCreateV1,
        *,
        principal_id: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json", exclude={"request_id"})
        now = parse_datetime(occurred_at)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = self._replay(
                    connection,
                    principal_id=principal_id,
                    request_id=body.request_id,
                    operation="create_commissioning_candidate",
                    payload=payload,
                )
                if replay is not None:
                    return replay
                profile_row = connection.execute(
                    """
                    SELECT * FROM workflow_simulation_validation_profiles
                    WHERE profile_id = ? AND version = ?
                    """,
                    (body.profile_id, body.profile_version),
                ).fetchone()
                if profile_row is None:
                    raise WorkflowAuthoringNotFound(
                        "unknown Simulation Validation Profile: "
                        f"{body.profile_id}@{body.profile_version}"
                    )
                profile = self._validation_profile(profile_row)
                reports = connection.execute(
                    """
                    SELECT validations.run_id
                    FROM workflow_simulation_run_validations AS validations
                    JOIN workflow_simulation_runs AS runs
                      ON runs.run_id = validations.run_id
                    WHERE validations.profile_id = ?
                      AND validations.profile_version = ?
                      AND validations.state = 'PASS'
                      AND runs.task_definition_id = ?
                      AND runs.task_version = ?
                    ORDER BY validations.evaluated_at DESC, validations.run_id
                    LIMIT ?
                    """,
                    (
                        body.profile_id,
                        body.profile_version,
                        profile["task_definition_id"],
                        profile["task_version"],
                        profile["required_successful_runs"],
                    ),
                ).fetchall()
                if len(reports) < profile["required_successful_runs"]:
                    raise WorkflowAuthoringConflict(
                        "Simulation Validation Profile has not met its required "
                        "successful Run count"
                    )
                publication = self._publication_row(
                    connection,
                    profile["task_definition_id"],
                    profile["task_version"],
                )
                candidate_id = f"commissioning-{uuid4().hex}"
                timestamp = isoformat_z(now)
                evidence = [row["run_id"] for row in reports]
                connection.execute(
                    """
                    INSERT INTO workflow_commissioning_candidates (
                        candidate_id, principal_id, request_id, profile_id,
                        profile_version, task_definition_id, task_version,
                        workflow_id, workflow_version, workflow_sha256,
                        evidence_run_ids_json, state, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'SIMULATION_APPROVED', ?)
                    """,
                    (
                        candidate_id,
                        principal_id,
                        body.request_id,
                        body.profile_id,
                        body.profile_version,
                        publication["task_definition_id"],
                        publication["task_version"],
                        publication["workflow_id"],
                        publication["workflow_version"],
                        publication["workflow_sha256"],
                        canonical_json(evidence),
                        timestamp,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM workflow_commissioning_candidates
                    WHERE candidate_id = ?
                    """,
                    (candidate_id,),
                ).fetchone()
                response = {
                    **self._commissioning_candidate(row),
                    "idempotent_replay": False,
                }
                self._record_request(
                    connection,
                    principal_id=principal_id,
                    request_id=body.request_id,
                    operation="create_commissioning_candidate",
                    payload=payload,
                    response=response,
                    occurred_at=now,
                )
                self._audit(
                    connection,
                    occurred_at=now,
                    action="commissioning_candidate_created",
                    entity_type="commissioning_candidate",
                    entity_id=candidate_id,
                    details={
                        "profile_id": body.profile_id,
                        "profile_version": body.profile_version,
                        "evidence_run_ids": evidence,
                        "physical_command_count": 0,
                    },
                )
                return response
        except sqlite3.IntegrityError as exc:
            raise WorkflowAuthoringConflict(
                "this Simulation Validation Profile already has a commissioning candidate"
            ) from exc

    def _publication_row(
        self, connection: sqlite3.Connection, task_id: str, task_version: int
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT publications.*, tasks.display_name, tasks.description,
                   tasks.roles_json, registry.payload_json
            FROM task_workflow_publications AS publications
            JOIN task_definitions AS tasks
              ON tasks.task_definition_id = publications.task_definition_id
             AND tasks.version = publications.task_version
            JOIN orchestration_registry AS registry
              ON registry.object_kind = 'workflow'
             AND registry.object_id = publications.workflow_id
             AND registry.version = publications.workflow_version
            WHERE publications.task_definition_id = ?
              AND publications.task_version = ?
            """,
            (task_id, task_version),
        ).fetchone()
        if row is None:
            raise WorkflowAuthoringNotFound(
                f"Task has no Workflow publication: {task_id}@{task_version}"
            )
        return row

    def compatibility_issues(
        self,
        workflow: WorkflowDefinitionV2,
        profile: UnityAdapterProfileV1,
    ) -> list[str]:
        issues: list[str] = []
        if isinstance(profile, UnityAdapterProfileV2):
            try:
                _, resolved = resolve_unity_workflow(workflow, profile.dynamic_binding)
                for skill in profile.dynamic_binding.catalog.skills:
                    if self.skill_definitions.get(skill.capability) != skill:
                        issues.append(f"Skill inputs differ from installed profile: {skill.capability}")
                for selected in resolved.values():
                    published = self.action_definitions.get(selected.action.action_ref)
                    if published != selected.action:
                        issues.append(f"Action publication differs from installed profile: {selected.action.action_ref}")
            except ValueError as exc:
                issues.append(str(exc))
            return issues
        workflow_roles = [
            {
                "role": role.role,
                "access": role.access,
                "requires": role.selector.requires,
                "cardinality": role.cardinality,
            }
            for role in workflow.roles
        ]
        expected_roles = [
            {
                "role": role.role,
                "access": role.access,
                "requires": role.requires,
                "cardinality": "1",
            }
            for role in profile.roles
        ]
        if workflow_roles != expected_roles:
            issues.append("Workflow roles do not match the fixed adapter profile")
        if any(step.kind != "action" for step in workflow.steps):
            issues.append("The fixed adapter accepts only its action steps")
            return issues
        actual_steps = []
        for step in workflow.steps:
            if step.retry.max_total_attempts != 1:
                issues.append(f"step {step.step_id} retry must remain one attempt")
            if (
                step.compensation_step is not None
                or step.fan_out is not None
                or step.lifetime is not None
            ):
                issues.append(f"step {step.step_id} uses unsupported execution controls")
            actual_steps.append(step)
        if [step.step_id for step in actual_steps] != [
            step.step_id for step in profile.steps
        ]:
            issues.append("Workflow steps differ from the fixed adapter contract")
            return issues
        for actual, expected in zip(actual_steps, profile.steps, strict=True):
            if (
                actual.role != expected.role
                or actual.capability != expected.capability
                or actual.depends_on != expected.depends_on
            ):
                issues.append(
                    f"step {actual.step_id} role, capability, or dependencies differ "
                    "from the fixed adapter contract"
                )
                continue
            for name, expected_value in expected.parameters.items():
                if actual.parameters.get(name) != expected_value:
                    issues.append(
                        f"step {actual.step_id} parameter {name} differs from the "
                        "fixed adapter operation"
                    )
            if isinstance(profile, IsaacAdapterProfileV3):
                if actual.parameters != expected.parameters:
                    issues.append(f"step {actual.step_id} differs from fixed Isaac parameters")
                continue
            definition = self.skill_definitions.get(actual.capability)
            if definition is None:
                if actual.parameters != expected.parameters:
                    issues.append(
                        f"step {actual.step_id} parameters differ from the fixed "
                        "adapter contract"
                    )
                continue
            if "SIMULATION" not in definition.execution_modes:
                issues.append(
                    f"step {actual.step_id} Skill does not allow SIMULATION"
                )
                continue
            try:
                validate_skill_parameters(definition, actual.parameters)
            except ValueError as exc:
                issues.append(f"step {actual.step_id}: {exc}")
        return issues

    def list_adapter_status(self, *, occurred_at: datetime) -> list[dict[str, Any]]:
        now = parse_datetime(occurred_at)
        cutoff = isoformat_z(now - timedelta(seconds=WORKER_HEARTBEAT_MAX_AGE_S))
        with self._connect() as connection:
            self._interrupt_expired(connection, now)
            results = []
            for adapter_id, (profile, digest) in self.adapter_profiles.items():
                worker = connection.execute(
                    """
                    SELECT * FROM workflow_simulation_workers
                    WHERE adapter_id = ? ORDER BY updated_at DESC LIMIT 1
                    """,
                    (adapter_id,),
                ).fetchone()
                fresh = worker is not None and worker["updated_at"] >= cutoff
                results.append(
                    {
                        "adapter_id": adapter_id,
                        "scenario_id": profile.scenario_id,
                        **({"execution_runtime": "isaac", "profile_id": profile.profile_id}
                           if isinstance(profile, IsaacAdapterProfileV3) else {}),
                        "profile_sha256": digest,
                        "ready": bool(
                            fresh
                            and worker["ready"]
                            and not worker["physical_output_connected"]
                            and worker["profile_sha256"] == digest
                        ),
                        "detail": (
                            "No Worker heartbeat"
                            if worker is None
                            else (
                                "Worker heartbeat is stale"
                                if not fresh
                                else worker["detail"]
                            )
                        ),
                        "worker_id": None if worker is None else worker["worker_id"],
                        "updated_at": None if worker is None else worker["updated_at"],
                    }
                )
            return results

    def queue_simulation(
        self,
        body: WorkflowSimulationQueueV1,
        *,
        principal_id: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json", exclude={"request_id"})
        now = parse_datetime(occurred_at)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = self._replay(
                    connection,
                    principal_id=principal_id,
                    request_id=body.request_id,
                    operation="queue_simulation",
                    payload=payload,
                )
                if replay is not None:
                    return replay
                profile_entry = self.adapter_profiles.get(body.adapter_id)
                if profile_entry is None:
                    raise WorkflowAuthoringUnavailable(
                        f"simulation adapter is not configured: {body.adapter_id}"
                    )
                profile, profile_sha256 = profile_entry
                self._interrupt_expired(connection, now)
                publication_row = self._publication_row(
                    connection, body.task_definition_id, body.task_version
                )
                workflow = WorkflowDefinitionV2.model_validate_json(
                    publication_row["payload_json"]
                )
                issues = self.compatibility_issues(workflow, profile)
                if issues:
                    raise WorkflowAuthoringInvalid("; ".join(issues))
                validation_profile = None
                if body.validation_profile_id is not None:
                    validation_profile = connection.execute(
                        """
                        SELECT * FROM workflow_simulation_validation_profiles
                        WHERE profile_id = ? AND version = ?
                        """,
                        (
                            body.validation_profile_id,
                            body.validation_profile_version,
                        ),
                    ).fetchone()
                    if validation_profile is None:
                        raise WorkflowAuthoringNotFound(
                            "unknown Simulation Validation Profile: "
                            f"{body.validation_profile_id}@"
                            f"{body.validation_profile_version}"
                        )
                    if (
                        validation_profile["task_definition_id"]
                        != publication_row["task_definition_id"]
                        or validation_profile["task_version"]
                        != publication_row["task_version"]
                    ):
                        raise WorkflowAuthoringInvalid(
                            "Simulation Validation Profile targets a different Task version"
                        )
                    if validation_profile["adapter_id"] != body.adapter_id:
                        raise WorkflowAuthoringInvalid(
                            "Simulation Validation Profile targets a different adapter"
                        )
                    if (
                        validation_profile["scenario_id"] != profile.scenario_id
                        or validation_profile["adapter_profile_sha256"]
                        != profile_sha256
                    ):
                        raise WorkflowAuthoringInvalid(
                            "Simulation Validation Profile is stale for this adapter build"
                        )
                cutoff = isoformat_z(
                    now - timedelta(seconds=WORKER_HEARTBEAT_MAX_AGE_S)
                )
                worker = connection.execute(
                    """
                    SELECT * FROM workflow_simulation_workers
                    WHERE adapter_id = ? AND profile_sha256 = ? AND ready = 1
                      AND physical_output_connected = 0 AND updated_at >= ?
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (body.adapter_id, profile_sha256, cutoff),
                ).fetchone()
                if worker is None:
                    raise WorkflowAuthoringUnavailable(
                        "no fresh, ready, output-isolated Simulation Worker is available"
                    )
                run_id = f"sim-{uuid4().hex}"
                timestamp = isoformat_z(now)
                progress = {
                    "steps": [
                        {"step_id": step.step_id, "state": "PENDING"}
                        for step in workflow.steps
                    ]
                }
                connection.execute(
                    """
                    INSERT INTO workflow_simulation_runs (
                        run_id, principal_id, request_id, display_name, adapter_id,
                        scenario_id, profile_sha256, task_definition_id, task_version,
                        workflow_id, workflow_version, workflow_sha256, state,
                        queue_expires_at, detail, progress_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        principal_id,
                        body.request_id,
                        body.display_name,
                        body.adapter_id,
                        profile.scenario_id,
                        profile_sha256,
                        publication_row["task_definition_id"],
                        publication_row["task_version"],
                        publication_row["workflow_id"],
                        publication_row["workflow_version"],
                        publication_row["workflow_sha256"],
                        isoformat_z(now + timedelta(seconds=SIMULATION_QUEUE_TTL_S)),
                        "Waiting for the local Simulation Worker",
                        canonical_json(progress),
                        timestamp,
                        timestamp,
                    ),
                )
                if validation_profile is not None:
                    connection.execute(
                        """
                        INSERT INTO workflow_simulation_run_validations (
                            run_id, profile_id, profile_version, state,
                            checks_json, evaluated_at
                        ) VALUES (?, ?, ?, 'PENDING', '[]', NULL)
                        """,
                        (
                            run_id,
                            validation_profile["profile_id"],
                            validation_profile["version"],
                        ),
                    )
                self._insert_run_event(
                    connection,
                    run_id=run_id,
                    occurred_at=now,
                    state="QUEUED",
                    detail="Simulation Run queued",
                    progress=progress,
                )
                row = connection.execute(
                    "SELECT * FROM workflow_simulation_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                validation_row = connection.execute(
                    """
                    SELECT * FROM workflow_simulation_run_validations
                    WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
                response = {
                    **self._simulation_run(row, validation_row),
                    "idempotent_replay": False,
                }
                self._record_request(
                    connection,
                    principal_id=principal_id,
                    request_id=body.request_id,
                    operation="queue_simulation",
                    payload=payload,
                    response=response,
                    occurred_at=now,
                )
                return response
        except sqlite3.IntegrityError as exc:
            raise WorkflowAuthoringConflict(
                "the simulation adapter already has a queued or running Simulation Run"
            ) from exc

    @staticmethod
    def _simulation_run(
        row: sqlite3.Row,
        validation_row: sqlite3.Row | None = None,
    ) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "display_name": row["display_name"],
            "adapter_id": row["adapter_id"],
            "scenario_id": row["scenario_id"],
            "profile_sha256": row["profile_sha256"],
            "task_definition_id": row["task_definition_id"],
            "task_version": row["task_version"],
            "workflow_id": row["workflow_id"],
            "workflow_version": row["workflow_version"],
            "workflow_sha256": row["workflow_sha256"],
            "state": row["state"],
            "worker_id": row["claimed_worker_id"],
            "reconciliation_required": bool(row["reconciliation_required"]),
            "detail": row["detail"],
            "progress": json.loads(row["progress_json"]),
            "result": None if row["result_json"] is None else json.loads(row["result_json"]),
            "validation": WorkflowAuthoringStore._validation_report(validation_row),
            "controls": [],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    @staticmethod
    def _simulation_control(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "control_id": row["control_id"],
            "run_id": row["run_id"],
            "action": row["action"],
            "state": row["state"],
            "worker_id": row["claimed_worker_id"],
            "detail": row["detail"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
        }

    def _controls_for(
        self, connection: sqlite3.Connection, run_id: str
    ) -> list[dict[str, Any]]:
        return [
            self._simulation_control(row)
            for row in connection.execute(
                """
                SELECT * FROM workflow_simulation_run_controls
                WHERE run_id = ? ORDER BY created_at, control_id
                """,
                (run_id,),
            ).fetchall()
        ]

    def _evaluate_run_validation(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        occurred_at: datetime,
    ) -> dict[str, Any] | None:
        validation = connection.execute(
            """
            SELECT validations.*, profiles.max_duration_s,
                   profiles.require_all_steps_succeeded,
                   profiles.require_output_isolated,
                   profiles.require_reset_acknowledged,
                   profiles.adapter_profile_sha256
            FROM workflow_simulation_run_validations AS validations
            JOIN workflow_simulation_validation_profiles AS profiles
              ON profiles.profile_id = validations.profile_id
             AND profiles.version = validations.profile_version
            WHERE validations.run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if validation is None:
            return None
        run = connection.execute(
            "SELECT * FROM workflow_simulation_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise WorkflowAuthoringNotFound(f"unknown Simulation Run: {run_id}")
        terminal = run["state"] in {"COMPLETE", "FAILED", "INTERRUPTED"}
        if not terminal:
            return self._validation_report(validation)

        result = {} if run["result_json"] is None else json.loads(run["result_json"])
        progress = json.loads(run["progress_json"])
        raw_steps = progress.get("steps", [])
        steps = raw_steps if isinstance(raw_steps, list) else []
        reported_steps = [step for step in steps if isinstance(step, dict)]
        publication = self._publication_row(
            connection,
            run["task_definition_id"],
            run["task_version"],
        )
        expected_workflow = WorkflowDefinitionV2.model_validate_json(
            publication["payload_json"]
        )
        expected_step_ids = [step.step_id for step in expected_workflow.steps]
        reported_step_ids = [step.get("step_id") for step in reported_steps]
        exact_step_evidence = (
            len(reported_steps) == len(expected_step_ids)
            and len(reported_step_ids) == len(set(reported_step_ids))
            and set(reported_step_ids) == set(expected_step_ids)
        )
        succeeded_step_count = sum(
            step.get("state") == "SUCCEEDED" for step in reported_steps
        )
        reset_acknowledged = connection.execute(
            """
            SELECT 1 FROM workflow_simulation_run_controls
            WHERE run_id = ? AND action = 'RESET' AND state = 'ACKNOWLEDGED'
            LIMIT 1
            """,
            (run_id,),
        ).fetchone() is not None
        duration_s = None
        if run["started_at"] is not None and run["finished_at"] is not None:
            duration_s = max(
                0.0,
                (
                    parse_datetime(run["finished_at"])
                    - parse_datetime(run["started_at"])
                ).total_seconds(),
            )

        def check(code: str, passed: bool, detail: str) -> dict[str, Any]:
            return {"code": code, "passed": passed, "detail": detail}

        checks = [
            check(
                "run-complete",
                run["state"] == "COMPLETE",
                f"Run terminal state is {run['state']}",
            ),
            check(
                "adapter-profile-pinned",
                run["profile_sha256"] == validation["adapter_profile_sha256"],
                "Run used the exact adapter Profile digest",
            ),
            check(
                "all-steps-succeeded",
                exact_step_evidence
                and all(
                    step.get("state") == "SUCCEEDED" for step in reported_steps
                ),
                f"{succeeded_step_count} of {len(expected_step_ids)} expected steps "
                f"succeeded; exact Step evidence={exact_step_evidence}",
            ),
            check(
                "physical-output-isolated",
                result.get("physical_output_connected") is False,
                "Simulation result declares physical_output_connected=false",
            ),
            check(
                "duration-within-limit",
                duration_s is not None and duration_s <= validation["max_duration_s"],
                (
                    "Run duration is unavailable"
                    if duration_s is None
                    else f"Run duration {duration_s:.3f}s <= "
                    f"{validation['max_duration_s']:.3f}s"
                ),
            ),
            check(
                "reset-acknowledged",
                not validation["require_reset_acknowledged"] or reset_acknowledged,
                (
                    "Reset is not required by this Profile"
                    if not validation["require_reset_acknowledged"]
                    else (
                        "Simulation Reset was acknowledged"
                        if reset_acknowledged
                        else "Simulation Reset acknowledgement is still required"
                    )
                ),
            ),
        ]
        non_reset_passed = all(
            item["passed"] for item in checks if item["code"] != "reset-acknowledged"
        )
        if non_reset_passed and checks[-1]["passed"]:
            state = "PASS"
        elif non_reset_passed and validation["require_reset_acknowledged"]:
            state = "AWAITING_RESET"
        else:
            state = "FAIL"
        timestamp = isoformat_z(occurred_at)
        connection.execute(
            """
            UPDATE workflow_simulation_run_validations
            SET state = ?, checks_json = ?, evaluated_at = ?
            WHERE run_id = ?
            """,
            (state, canonical_json(checks), timestamp, run_id),
        )
        updated = connection.execute(
            """
            SELECT * FROM workflow_simulation_run_validations WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        return self._validation_report(updated)

    def _interrupt_expired(
        self, connection: sqlite3.Connection, occurred_at: datetime
    ) -> set[str]:
        timestamp = isoformat_z(occurred_at)
        rows = connection.execute(
            """
            SELECT * FROM workflow_simulation_runs
            WHERE (state = 'RUNNING' AND reconciliation_required = 0
                   AND lease_expires_at < ?)
               OR (state = 'QUEUED' AND queue_expires_at < ?)
            """,
            (timestamp, timestamp),
        ).fetchall()
        interrupted: set[str] = set()
        for row in rows:
            if row["state"] == "RUNNING":
                detail = "Simulation Worker lease expired; restart reconciliation is pending"
                connection.execute(
                    """
                    UPDATE workflow_simulation_runs
                    SET reconciliation_required = 1, claimed_worker_id = NULL,
                        lease_expires_at = NULL, detail = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (detail, timestamp, row["run_id"]),
                )
                event_state = "RUNNING"
            else:
                detail = "Simulation Worker did not claim the queued Run before its deadline"
                connection.execute(
                    """
                    UPDATE workflow_simulation_runs
                    SET state = 'INTERRUPTED', detail = ?, updated_at = ?, finished_at = ?
                    WHERE run_id = ?
                    """,
                    (detail, timestamp, timestamp, row["run_id"]),
                )
                interrupted.add(row["run_id"])
                event_state = "INTERRUPTED"
            self._insert_run_event(
                connection,
                run_id=row["run_id"],
                occurred_at=occurred_at,
                state=event_state,
                detail=detail,
                progress=json.loads(row["progress_json"]),
            )
            if event_state == "INTERRUPTED":
                self._evaluate_run_validation(
                    connection,
                    run_id=row["run_id"],
                    occurred_at=occurred_at,
                )
        return interrupted

    def _reconcile_pending_validations(
        self, connection: sqlite3.Connection, occurred_at: datetime
    ) -> None:
        rows = connection.execute(
            """
            SELECT validations.run_id
            FROM workflow_simulation_run_validations AS validations
            JOIN workflow_simulation_runs AS runs ON runs.run_id = validations.run_id
            WHERE validations.state = 'PENDING'
              AND runs.state IN ('COMPLETE', 'FAILED', 'INTERRUPTED')
            ORDER BY runs.finished_at, validations.run_id
            """
        ).fetchall()
        for row in rows:
            self._evaluate_run_validation(
                connection,
                run_id=row["run_id"],
                occurred_at=occurred_at,
            )

    @staticmethod
    def _insert_run_event(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        occurred_at: datetime,
        state: str,
        detail: str,
        progress: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO workflow_simulation_run_events (
                run_id, occurred_at, state, detail, progress_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                isoformat_z(occurred_at),
                state,
                detail[:2048],
                canonical_json(progress),
            ),
        )

    def get_simulation_run(
        self, run_id: str, *, occurred_at: datetime
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = parse_datetime(occurred_at)
            self._interrupt_expired(connection, now)
            self._reconcile_pending_validations(connection, now)
            row = connection.execute(
                "SELECT * FROM workflow_simulation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            controls = [] if row is None else self._controls_for(connection, run_id)
            validation = None if row is None else connection.execute(
                """
                SELECT * FROM workflow_simulation_run_validations WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            raise WorkflowAuthoringNotFound(f"unknown Simulation Run: {run_id}")
        return {**self._simulation_run(row, validation), "controls": controls}

    def list_simulation_runs(self, *, occurred_at: datetime) -> list[dict[str, Any]]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = parse_datetime(occurred_at)
            self._interrupt_expired(connection, now)
            self._reconcile_pending_validations(connection, now)
            rows = connection.execute(
                """
                SELECT * FROM workflow_simulation_runs
                ORDER BY created_at DESC, run_id LIMIT 100
                """
            ).fetchall()
            controls = {
                row["run_id"]: self._controls_for(connection, row["run_id"])
                for row in rows
            }
            validations = {
                row["run_id"]: connection.execute(
                    """
                    SELECT * FROM workflow_simulation_run_validations
                    WHERE run_id = ?
                    """,
                    (row["run_id"],),
                ).fetchone()
                for row in rows
            }
        return [
            {
                **self._simulation_run(row, validations[row["run_id"]]),
                "controls": controls[row["run_id"]],
            }
            for row in rows
        ]

    def request_simulation_control(
        self,
        run_id: str,
        body: WorkflowSimulationControlV1,
        *,
        principal_id: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        now = parse_datetime(occurred_at)
        payload = {"run_id": run_id, **body.model_dump(mode="json")}
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = self._replay(
                    connection,
                    principal_id=principal_id,
                    request_id=body.request_id,
                    operation="control_simulation",
                    payload=payload,
                )
                if replay is not None:
                    return replay
                self._interrupt_expired(connection, now)
                run = connection.execute(
                    "SELECT * FROM workflow_simulation_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if run is None:
                    raise WorkflowAuthoringNotFound(
                        f"unknown Simulation Run: {run_id}"
                    )
                progress = json.loads(run["progress_json"])
                lifecycle = progress.get("lifecycle_state")
                if body.action == "HOLD" and not (
                    run["state"] == "RUNNING" and lifecycle != "HELD"
                ):
                    raise WorkflowAuthoringConflict(
                        "HOLD requires a running, non-held Simulation Run"
                    )
                if body.action == "RESUME" and not (
                    run["state"] == "RUNNING" and lifecycle == "HELD"
                ):
                    raise WorkflowAuthoringConflict(
                        "RESUME requires a held Simulation Run"
                    )
                if body.action == "ABORT" and run["state"] not in {
                    "QUEUED",
                    "RUNNING",
                }:
                    raise WorkflowAuthoringConflict(
                        "ABORT requires a queued or running Simulation Run"
                    )
                if body.action == "RESET":
                    if run["state"] not in {"COMPLETE", "FAILED", "INTERRUPTED"}:
                        raise WorkflowAuthoringConflict(
                            "RESET is available only after a terminal Simulation Run"
                        )
                    active = connection.execute(
                        """
                        SELECT 1 FROM workflow_simulation_runs
                        WHERE adapter_id = ? AND run_id != ?
                          AND state IN ('QUEUED', 'RUNNING')
                        """,
                        (run["adapter_id"], run_id),
                    ).fetchone()
                    if active is not None:
                        raise WorkflowAuthoringConflict(
                            "RESET cannot interrupt another active adapter Run"
                        )

                control_id = f"simctl-{uuid4().hex}"
                timestamp = isoformat_z(now)
                immediate_abort = body.action == "ABORT" and run["state"] == "QUEUED"
                control_state = "ACKNOWLEDGED" if immediate_abort else "REQUESTED"
                detail = (
                    "Queued Simulation Run aborted before dispatch"
                    if immediate_abort
                    else f"{body.action} requested by {principal_id}"
                )
                connection.execute(
                    """
                    INSERT INTO workflow_simulation_run_controls (
                        control_id, run_id, principal_id, request_id, action,
                        state, detail, created_at, updated_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        control_id,
                        run_id,
                        principal_id,
                        body.request_id,
                        body.action,
                        control_state,
                        detail,
                        timestamp,
                        timestamp,
                        timestamp if immediate_abort else None,
                    ),
                )
                if immediate_abort:
                    progress["lifecycle_state"] = "ABORTED"
                    connection.execute(
                        """
                        UPDATE workflow_simulation_runs
                        SET state = 'INTERRUPTED', detail = ?, progress_json = ?,
                            updated_at = ?, finished_at = ?
                        WHERE run_id = ?
                        """,
                        (detail, canonical_json(progress), timestamp, timestamp, run_id),
                    )
                    self._evaluate_run_validation(
                        connection,
                        run_id=run_id,
                        occurred_at=now,
                    )
                self._insert_run_event(
                    connection,
                    run_id=run_id,
                    occurred_at=now,
                    state="INTERRUPTED" if immediate_abort else run["state"],
                    detail=detail,
                    progress=progress,
                )
                control_row = connection.execute(
                    """
                    SELECT * FROM workflow_simulation_run_controls
                    WHERE control_id = ?
                    """,
                    (control_id,),
                ).fetchone()
                response = {
                    **self._simulation_control(control_row),
                    "idempotent_replay": False,
                }
                self._record_request(
                    connection,
                    principal_id=principal_id,
                    request_id=body.request_id,
                    operation="control_simulation",
                    payload=payload,
                    response=response,
                    occurred_at=now,
                )
                self._audit(
                    connection,
                    occurred_at=now,
                    action="simulation_control_requested",
                    entity_type="workflow_simulation_run",
                    entity_id=run_id,
                    details={"control_id": control_id, "action": body.action},
                )
                return response
        except sqlite3.IntegrityError as exc:
            raise WorkflowAuthoringConflict(
                "the Simulation Run already has a pending control request"
            ) from exc

    def claim_simulation_control(
        self,
        *,
        worker_id: str,
        adapter_id: str,
        claim_ttl: timedelta,
        occurred_at: datetime,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        now = parse_datetime(occurred_at)
        timestamp = isoformat_z(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            worker = connection.execute(
                "SELECT * FROM workflow_simulation_workers WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            if worker is None or worker["adapter_id"] != adapter_id:
                raise WorkflowAuthoringNotFound("Worker must heartbeat before claiming controls")
            cutoff = isoformat_z(
                now - timedelta(seconds=WORKER_HEARTBEAT_MAX_AGE_S)
            )
            if worker["updated_at"] < cutoff or worker["physical_output_connected"]:
                raise WorkflowAuthoringUnavailable(
                    "Worker heartbeat is stale or not output-isolated"
                )
            parameters: list[Any] = [adapter_id, timestamp]
            run_filter = ""
            if run_id is not None:
                run_filter = " AND controls.run_id = ?"
                parameters.append(run_id)
            control = connection.execute(
                f"""
                SELECT controls.*
                FROM workflow_simulation_run_controls AS controls
                JOIN workflow_simulation_runs AS runs ON runs.run_id = controls.run_id
                WHERE runs.adapter_id = ?
                  AND (
                    controls.state = 'REQUESTED'
                    OR (controls.state = 'CLAIMED' AND controls.claim_expires_at < ?)
                  )
                  {run_filter}
                ORDER BY controls.created_at, controls.control_id LIMIT 1
                """,
                tuple(parameters),
            ).fetchone()
            if control is None:
                return None
            connection.execute(
                """
                UPDATE workflow_simulation_run_controls
                SET state = 'CLAIMED', claimed_worker_id = ?, claim_expires_at = ?,
                    detail = ?, updated_at = ?
                WHERE control_id = ?
                """,
                (
                    worker_id,
                    isoformat_z(now + claim_ttl),
                    f"Claimed by Simulation Worker {worker_id}",
                    timestamp,
                    control["control_id"],
                ),
            )
            claimed = connection.execute(
                """
                SELECT * FROM workflow_simulation_run_controls WHERE control_id = ?
                """,
                (control["control_id"],),
            ).fetchone()
            return self._simulation_control(claimed)

    def finish_simulation_control(
        self,
        control_id: str,
        *,
        worker_id: str,
        state: Literal["ACKNOWLEDGED", "REJECTED"],
        detail: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        now = parse_datetime(occurred_at)
        timestamp = isoformat_z(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            control = connection.execute(
                """
                SELECT * FROM workflow_simulation_run_controls WHERE control_id = ?
                """,
                (control_id,),
            ).fetchone()
            if control is None:
                raise WorkflowAuthoringNotFound(
                    f"unknown Simulation control: {control_id}"
                )
            if control["state"] in {"ACKNOWLEDGED", "REJECTED"}:
                return self._simulation_control(control)
            if control["state"] != "CLAIMED" or control["claimed_worker_id"] != worker_id:
                raise WorkflowAuthoringConflict(
                    "Worker no longer owns the Simulation control"
                )
            run = connection.execute(
                "SELECT * FROM workflow_simulation_runs WHERE run_id = ?",
                (control["run_id"],),
            ).fetchone()
            progress = json.loads(run["progress_json"])
            if state == "ACKNOWLEDGED":
                lifecycle = {
                    "HOLD": "HELD",
                    "RESUME": "RUNNING",
                    "ABORT": "ABORTED",
                    "RESET": "READY",
                }[control["action"]]
                progress["lifecycle_state"] = lifecycle
            connection.execute(
                """
                UPDATE workflow_simulation_run_controls
                SET state = ?, detail = ?, claim_expires_at = NULL,
                    updated_at = ?, finished_at = ?
                WHERE control_id = ?
                """,
                (state, detail[:2048], timestamp, timestamp, control_id),
            )
            run_state = run["state"]
            finished_at = run["finished_at"]
            lease_expires_at = run["lease_expires_at"]
            claimed_worker_id = run["claimed_worker_id"]
            reconciliation_required = run["reconciliation_required"]
            if state == "ACKNOWLEDGED" and control["action"] == "ABORT":
                run_state = "INTERRUPTED"
                finished_at = timestamp
                lease_expires_at = None
                claimed_worker_id = None
                reconciliation_required = 0
            elif state == "ACKNOWLEDGED" and control["action"] == "RESET":
                reconciliation_required = 0
            connection.execute(
                """
                UPDATE workflow_simulation_runs
                SET state = ?, progress_json = ?, detail = ?, lease_expires_at = ?,
                    claimed_worker_id = ?,
                    reconciliation_required = ?, updated_at = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (
                    run_state,
                    canonical_json(progress),
                    detail[:2048],
                    lease_expires_at,
                    claimed_worker_id,
                    reconciliation_required,
                    timestamp,
                    finished_at,
                    control["run_id"],
                ),
            )
            self._insert_run_event(
                connection,
                run_id=control["run_id"],
                occurred_at=now,
                state=run_state,
                detail=detail,
                progress=progress,
            )
            self._audit(
                connection,
                occurred_at=now,
                action="simulation_control_finished",
                entity_type="workflow_simulation_run",
                entity_id=control["run_id"],
                details={
                    "control_id": control_id,
                    "action": control["action"],
                    "state": state,
                },
            )
            if control["action"] in {"ABORT", "RESET"} and state == "ACKNOWLEDGED":
                self._evaluate_run_validation(
                    connection,
                    run_id=control["run_id"],
                    occurred_at=now,
                )
            updated = connection.execute(
                """
                SELECT * FROM workflow_simulation_run_controls WHERE control_id = ?
                """,
                (control_id,),
            ).fetchone()
            return self._simulation_control(updated)

    def worker_heartbeat(
        self,
        *,
        worker_id: str,
        adapter_id: str,
        profile_sha256: str,
        scenario_id: str,
        ready: bool,
        detail: str,
        physical_output_connected: bool,
        occurred_at: datetime,
    ) -> None:
        entry = self.adapter_profiles.get(adapter_id)
        if entry is None or entry[1] != profile_sha256:
            raise WorkflowAuthoringInvalid("Worker adapter profile digest is not configured")
        if entry[0].scenario_id != scenario_id:
            raise WorkflowAuthoringInvalid("Worker scenario does not match its adapter")
        if physical_output_connected:
            raise WorkflowAuthoringInvalid("physical outputs must remain disconnected")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workflow_simulation_workers (
                    worker_id, adapter_id, scenario_id, profile_sha256, ready,
                    physical_output_connected, detail, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    adapter_id = excluded.adapter_id,
                    scenario_id = excluded.scenario_id,
                    profile_sha256 = excluded.profile_sha256,
                    ready = excluded.ready,
                    physical_output_connected = 0,
                    detail = excluded.detail,
                    updated_at = excluded.updated_at
                """,
                (
                    worker_id,
                    adapter_id,
                    scenario_id,
                    profile_sha256,
                    int(ready),
                    detail[:2048],
                    isoformat_z(occurred_at),
                ),
            )

    def claim_simulation_run(
        self,
        *,
        worker_id: str,
        adapter_id: str,
        lease_ttl: timedelta,
        occurred_at: datetime,
    ) -> dict[str, Any] | None:
        now = parse_datetime(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._interrupt_expired(connection, now)
            profile_entry = self.adapter_profiles.get(adapter_id)
            if profile_entry is None:
                raise WorkflowAuthoringUnavailable(
                    f"simulation adapter is not configured: {adapter_id}"
                )
            profile, profile_sha256 = profile_entry
            worker = connection.execute(
                "SELECT * FROM workflow_simulation_workers WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            if worker is None or worker["adapter_id"] != adapter_id:
                raise WorkflowAuthoringNotFound("Worker must heartbeat before claiming")
            heartbeat_cutoff = isoformat_z(
                now - timedelta(seconds=WORKER_HEARTBEAT_MAX_AGE_S)
            )
            if not (
                not worker["physical_output_connected"]
                and worker["profile_sha256"] == profile_sha256
                and worker["scenario_id"] == profile.scenario_id
                and worker["updated_at"] >= heartbeat_cutoff
            ):
                raise WorkflowAuthoringUnavailable(
                    "Worker heartbeat is stale, profile-mismatched, or not output-isolated"
                )
            row = connection.execute(
                """
                SELECT * FROM workflow_simulation_runs
                WHERE adapter_id = ?
                  AND (
                    (
                      state = 'RUNNING'
                      AND (reconciliation_required = 1 OR claimed_worker_id IS NULL)
                    )
                    OR state = 'QUEUED'
                  )
                ORDER BY CASE WHEN state = 'RUNNING' THEN 0 ELSE 1 END, created_at
                LIMIT 1
                """,
                (adapter_id,),
            ).fetchone()
            if row is None:
                return None
            if (row["profile_sha256"] != profile_sha256
                    or row["scenario_id"] != profile.scenario_id):
                raise WorkflowAuthoringUnavailable(
                    "Simulation Run pins a different adapter profile; restore its deployment before claiming"
                )
            reconciling = row["state"] == "RUNNING"
            if not reconciling and not worker["ready"]:
                raise WorkflowAuthoringUnavailable(
                    "Worker is not ready to claim a new Simulation Run"
                )
            timestamp = isoformat_z(now)
            if reconciling:
                detail = "Reclaimed by the Simulation Worker for restart reconciliation"
                connection.execute(
                    """
                    UPDATE workflow_simulation_runs
                    SET claimed_worker_id = ?, lease_expires_at = ?,
                        reconciliation_required = 0, detail = ?, updated_at = ?
                    WHERE run_id = ? AND state = 'RUNNING'
                      AND (reconciliation_required = 1 OR claimed_worker_id IS NULL)
                    """,
                    (
                        worker_id,
                        isoformat_z(now + lease_ttl),
                        detail,
                        timestamp,
                        row["run_id"],
                    ),
                )
            else:
                detail = "Claimed by the local Simulation Worker"
                connection.execute(
                    """
                    UPDATE workflow_simulation_runs
                    SET state = 'RUNNING', claimed_worker_id = ?, lease_expires_at = ?,
                        detail = ?, started_at = ?, updated_at = ?
                    WHERE run_id = ? AND state = 'QUEUED'
                    """,
                    (
                        worker_id,
                        isoformat_z(now + lease_ttl),
                        detail,
                        timestamp,
                        timestamp,
                        row["run_id"],
                    ),
                )
            updated = connection.execute(
                "SELECT * FROM workflow_simulation_runs WHERE run_id = ?",
                (row["run_id"],),
            ).fetchone()
            progress = json.loads(updated["progress_json"])
            self._insert_run_event(
                connection,
                run_id=updated["run_id"],
                occurred_at=now,
                state="RUNNING",
                detail=updated["detail"],
                progress=progress,
            )
            publication = self._publication_row(
                connection, updated["task_definition_id"], updated["task_version"]
            )
            return {
                **self._simulation_run(updated),
                "workflow": json.loads(publication["payload_json"]),
                "reconciliation": reconciling,
            }

    @contextmanager
    def simulation_dispatch_claim(self, run_id: str, *, worker_id: str):
        """Fence the short local command-file write against concurrent reclaim."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workflow_simulation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if (row is None or row["state"] != "RUNNING"
                    or row["claimed_worker_id"] != worker_id
                    or row["reconciliation_required"]
                    or row["lease_expires_at"] is None
                    or parse_datetime(row["lease_expires_at"]) <= datetime.now(timezone.utc)):
                raise WorkflowAuthoringConflict("Simulation command emission no longer owns a live lease")
            yield

    def update_simulation_progress(
        self,
        run_id: str,
        *,
        worker_id: str,
        progress: dict[str, Any],
        detail: str,
        lease_ttl: timedelta,
        occurred_at: datetime,
    ) -> None:
        _scan_authoring_value(progress, path="progress")
        now = parse_datetime(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            interrupted = self._interrupt_expired(connection, now)
            if run_id in interrupted:
                connection.commit()
                raise WorkflowAuthoringConflict(
                    "Worker lease expired; the Simulation Run is INTERRUPTED"
                )
            row = connection.execute(
                "SELECT * FROM workflow_simulation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise WorkflowAuthoringNotFound(f"unknown Simulation Run: {run_id}")
            if row["state"] != "RUNNING" or row["claimed_worker_id"] != worker_id:
                raise WorkflowAuthoringConflict("Worker no longer owns the Simulation Run")
            connection.execute(
                """
                UPDATE workflow_simulation_runs
                SET progress_json = ?, detail = ?, lease_expires_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    canonical_json(progress),
                    detail[:2048],
                    isoformat_z(now + lease_ttl),
                    isoformat_z(now),
                    run_id,
                ),
            )
            self._insert_run_event(
                connection,
                run_id=run_id,
                occurred_at=now,
                state="RUNNING",
                detail=detail,
                progress=progress,
            )

    def finish_simulation_run(
        self,
        run_id: str,
        *,
        worker_id: str,
        state: Literal["COMPLETE", "FAILED"],
        detail: str,
        progress: dict[str, Any],
        result: dict[str, Any] | None,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        _scan_authoring_value(progress, path="progress")
        now = parse_datetime(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            interrupted = self._interrupt_expired(connection, now)
            if run_id in interrupted:
                connection.commit()
                raise WorkflowAuthoringConflict(
                    "Worker lease expired; the Simulation Run is INTERRUPTED"
                )
            row = connection.execute(
                "SELECT * FROM workflow_simulation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise WorkflowAuthoringNotFound(f"unknown Simulation Run: {run_id}")
            if row["state"] != "RUNNING" or row["claimed_worker_id"] != worker_id:
                raise WorkflowAuthoringConflict("Worker no longer owns the Simulation Run")
            timestamp = isoformat_z(now)
            connection.execute(
                """
                UPDATE workflow_simulation_runs
                SET state = ?, detail = ?, progress_json = ?, result_json = ?,
                    lease_expires_at = NULL, updated_at = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (
                    state,
                    detail[:2048],
                    canonical_json(progress),
                    None if result is None else canonical_json(result),
                    timestamp,
                    timestamp,
                    run_id,
                ),
            )
            self._insert_run_event(
                connection,
                run_id=run_id,
                occurred_at=now,
                state=state,
                detail=detail,
                progress=progress,
            )
            updated = connection.execute(
                "SELECT * FROM workflow_simulation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            validation = self._evaluate_run_validation(
                connection,
                run_id=run_id,
                occurred_at=now,
            )
            validation_row = None
            if validation is not None:
                validation_row = connection.execute(
                    """
                    SELECT * FROM workflow_simulation_run_validations
                    WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
            return self._simulation_run(updated, validation_row)
