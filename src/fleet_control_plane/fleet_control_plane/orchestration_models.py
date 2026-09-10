"""Strict, versioned contracts for generic multi-robot orchestration.

G0 defines payloads and deterministic validation only.  Nothing in this module
opens a command transport, talks to a vendor SDK, or grants physical authority.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from fleet_control_plane.models import FleetModel, SafeId


MAX_CAPABILITIES = 256
MAX_WORKFLOW_ROLES = 128
MAX_WORKFLOW_STEPS = 512
MAX_BOUND_ASSETS = 512

RuntimeClass = Literal["physical", "simulation", "replay"]
ExecutionMode = Literal[
    "OBSERVE_ONLY",
    "SIMULATION",
    "REPLAY",
    "SHADOW",
    "HIL",
    "PHYSICAL_AUTO",
    "PHYSICAL_TELEOP",
    "MAINTENANCE",
]
AccessMode = Literal["exclusive", "shared_read"]
CapabilityEffect = Literal["observation", "actuation", "process_output"]
LeaseExpiryBehavior = Literal[
    "controlled_hold",
    "controlled_cancel",
    "safe_state",
]
AgentRestartBehavior = Literal[
    "vendor_state_reconcile",
    "safe_state",
    "inhibit_until_reconciled",
]
OnHoldBehavior = Literal[
    "controlled_hold",
    "controlled_cancel",
    "finish_if_bounded",
    "capability_default",
]
JoinMode = Literal["all", "any"]
FanOutMode = Literal["all_bound_assets", "first_n", "explicit_binding_set"]

CapabilityRef = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=96,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*@[1-9][0-9]*$",
        strip_whitespace=True,
    ),
]
SchemaReference = Annotated[
    str,
    StringConstraints(min_length=3, max_length=2048, strip_whitespace=True),
]
Sha256Digest = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]
ScopedId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
        strip_whitespace=True,
    ),
]
BoundedText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=2048, strip_whitespace=True),
]
ResultPath = Annotated[
    str,
    StringConstraints(
        min_length=14,
        max_length=256,
        pattern=(
            r"^steps\.[A-Za-z0-9][A-Za-z0-9._-]*\.result"
            r"(?:\.[A-Za-z0-9][A-Za-z0-9._-]*)*$"
        ),
        strip_whitespace=True,
    ),
]
StepEventRef = Annotated[
    str,
    StringConstraints(
        min_length=11,
        max_length=96,
        pattern=(
            r"^[A-Za-z0-9][A-Za-z0-9._-]*:"
            r"(?:SUCCEEDED|FAILED|CANCELLED|TERMINAL)$"
        ),
        strip_whitespace=True,
    ),
]
RoleCardinality = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=16,
        pattern=r"^(?:[1-9][0-9]*|[1-9][0-9]*\.\.(?:[1-9][0-9]*|n))$",
        strip_whitespace=True,
    ),
]

_FORBIDDEN_WORKFLOW_KEYS = frozenset(
    {
        "ip",
        "ip_address",
        "connection",
        "connection_ref",
        "asset_id",
        "asset_ids",
        "binding_asset_ids",
        "endpoint",
        "host",
        "hostname",
        "make",
        "model",
        "namespace",
        "port",
        "robot_model",
        "ros_namespace",
        "ros_topic",
        "scene",
        "scene_name",
        "sdk",
        "simulator",
        "simulator_name",
        "topic",
        "unity_scene",
        "vendor",
        "vendor_api",
    }
)


def capability_ref(capability_id: str, version: int) -> str:
    """Return the canonical wire representation for a capability version."""

    return f"{capability_id}@{version}"


def parse_capability_ref(value: str) -> tuple[str, int]:
    capability_id, version = value.rsplit("@", 1)
    return capability_id, int(version)


def parse_role_cardinality(value: str) -> tuple[int, int | None]:
    """Parse `1`, `1..n`, or a finite range into inclusive bounds."""

    if ".." not in value:
        count = int(value)
        return count, count
    minimum_text, maximum_text = value.split("..", 1)
    minimum = int(minimum_text)
    maximum = None if maximum_text == "n" else int(maximum_text)
    if maximum is not None and maximum < minimum:
        raise ValueError("role cardinality maximum cannot be below its minimum")
    return minimum, maximum


def _ensure_unique(values: list[object], label: str) -> None:
    normalized = [str(value) for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} must be unique")


def _event_step_id(event_ref: str) -> str:
    return event_ref.rsplit(":", 1)[0]


def _reject_connection_details(value: JsonValue, path: str = "parameters") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[-\s]+", "_", key.lower())
            if normalized in _FORBIDDEN_WORKFLOW_KEYS:
                raise ValueError(
                    f"workflow {path} contains deployment-specific field {key!r}"
                )
            _reject_connection_details(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_connection_details(child, f"{path}[{index}]")
    elif isinstance(value, str):
        if re.search(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])", value):
            raise ValueError(f"workflow {path} contains a network address")
        if "://" in value and not value.startswith(
            ("artifact://", "contracts://", "registry://", "workcell://")
        ):
            raise ValueError(f"workflow {path} contains a deployment URI")
        if re.search(r"(?<!\S)/(?:[A-Za-z0-9._-]+/?)+", value):
            raise ValueError(f"workflow {path} contains an absolute topic or path")


class ProfileRefV1(FleetModel):
    profile_id: SafeId
    version: int = Field(ge=1)


class WorkflowRefV2(FleetModel):
    workflow_id: SafeId
    version: int = Field(ge=1)


class ManifestRefV1(FleetModel):
    manifest_id: SafeId
    version: int = Field(ge=1)
    sha256: Sha256Digest


class RegistryRefV1(FleetModel):
    object_id: SafeId
    version: int = Field(ge=1)
    sha256: Sha256Digest


class ApprovalV1(FleetModel):
    state: Literal["draft", "approved", "revoked"]
    approver_id: SafeId | None = None
    approved_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def approval_fields_are_coherent(self) -> Self:
        if self.state == "approved" and (
            self.approver_id is None or self.approved_at is None
        ):
            raise ValueError("approved objects require approver_id and approved_at")
        if self.state != "approved" and (
            self.approver_id is not None or self.approved_at is not None
        ):
            raise ValueError("only approved objects may contain approval identity")
        if (
            self.approved_at is not None
            and self.expires_at is not None
            and self.expires_at <= self.approved_at
        ):
            raise ValueError("approval expires_at must follow approved_at")
        return self


class CapabilityDefinitionV1(FleetModel):
    capability_id: SafeId
    version: int = Field(ge=1)
    effect: CapabilityEffect
    parameter_schema_ref: SchemaReference
    required_frames: list[SafeId] = Field(default_factory=list, max_length=64)
    required_units: list[SafeId] = Field(default_factory=list, max_length=64)
    required_limits: list[SafeId] = Field(default_factory=list, max_length=64)
    control_envelope_schema_ref: SchemaReference | None = None
    completion_contract_ref: SchemaReference | None = None
    cancellation_contract_ref: SchemaReference | None = None
    recovery_contract_ref: SchemaReference | None = None
    evidence_contract_ref: SchemaReference | None = None
    allowed_execution_modes: list[ExecutionMode] = Field(min_length=1, max_length=8)
    max_clock_uncertainty_ms: float | None = Field(default=None, ge=0.0)
    max_demand_age_ms: float | None = Field(default=None, ge=0.0)
    on_lease_expiry: LeaseExpiryBehavior | None = None
    on_agent_restart_with_motion_in_flight: AgentRestartBehavior | None = None

    @property
    def ref(self) -> str:
        return capability_ref(self.capability_id, self.version)

    @model_validator(mode="after")
    def physical_effect_contract_is_complete(self) -> Self:
        _ensure_unique(self.required_frames, "required_frames")
        _ensure_unique(self.required_units, "required_units")
        _ensure_unique(self.required_limits, "required_limits")
        _ensure_unique(self.allowed_execution_modes, "allowed_execution_modes")
        if self.effect == "observation":
            return self

        required = {
            "control_envelope_schema_ref": self.control_envelope_schema_ref,
            "completion_contract_ref": self.completion_contract_ref,
            "cancellation_contract_ref": self.cancellation_contract_ref,
            "recovery_contract_ref": self.recovery_contract_ref,
            "evidence_contract_ref": self.evidence_contract_ref,
            "max_clock_uncertainty_ms": self.max_clock_uncertainty_ms,
            "max_demand_age_ms": self.max_demand_age_ms,
            "on_lease_expiry": self.on_lease_expiry,
            "on_agent_restart_with_motion_in_flight": (
                self.on_agent_restart_with_motion_in_flight
            ),
        }
        missing = sorted(name for name, value in required.items() if value is None)
        if missing:
            raise ValueError(
                "effectful capabilities require complete execution contracts: "
                + ", ".join(missing)
            )
        return self


class CapabilityProfileV1(FleetModel):
    schema_version: Literal[1] = 1
    profile_id: SafeId
    version: int = Field(ge=1)
    kind_tags: list[SafeId] = Field(default_factory=list, max_length=64)
    capabilities: list[CapabilityDefinitionV1] = Field(
        min_length=1,
        max_length=MAX_CAPABILITIES,
    )

    @model_validator(mode="after")
    def profile_entries_are_unique(self) -> Self:
        _ensure_unique(self.kind_tags, "kind_tags")
        _ensure_unique([item.ref for item in self.capabilities], "capabilities")
        return self


class VendorIdentityV1(FleetModel):
    make: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    model: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class FrameContractV1(FleetModel):
    mappings: dict[SafeId, SafeId] = Field(min_length=1, max_length=128)
    world_calibration_required: bool = True


class UnitContractV1(FleetModel):
    mappings: dict[SafeId, SafeId] = Field(min_length=1, max_length=128)


class ObservationChannelV1(FleetModel):
    key: SafeId
    message_type: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    min_rate_hz: float = Field(gt=0.0)


class DeviceExecutionContractV1(FleetModel):
    agent_api: Annotated[str, StringConstraints(min_length=3, max_length=256)]
    supported_modes: list[ExecutionMode] = Field(min_length=1, max_length=8)
    command_sink_present: bool = False

    @field_validator("supported_modes")
    @classmethod
    def modes_are_unique(cls, value: list[ExecutionMode]) -> list[ExecutionMode]:
        _ensure_unique(value, "supported_modes")
        return value


class DeviceManifestV1(FleetModel):
    schema_version: Literal[1] = 1
    manifest_id: SafeId
    version: int = Field(ge=1)
    profile_ref: ProfileRefV1
    vendor: VendorIdentityV1
    capability_refs: list[CapabilityRef] = Field(
        min_length=1,
        max_length=MAX_CAPABILITIES,
    )
    frames: FrameContractV1
    units: UnitContractV1
    limits_ref: RegistryRefV1
    observation_contract: list[ObservationChannelV1] = Field(
        default_factory=list,
        max_length=256,
    )
    execution_contract: DeviceExecutionContractV1
    local_fail_behavior_ref: SchemaReference

    @model_validator(mode="after")
    def manifest_entries_are_unique(self) -> Self:
        _ensure_unique(self.capability_refs, "capability_refs")
        _ensure_unique(
            [channel.key for channel in self.observation_contract],
            "observation channel keys",
        )
        return self


class Vec2V1(FleetModel):
    x: float
    y: float


class Vec3V1(FleetModel):
    x: float
    y: float
    z: float


class BoxGeometryV1(FleetModel):
    kind: Literal["box"] = "box"
    center: Vec3V1
    size: Vec3V1

    @model_validator(mode="after")
    def size_is_positive(self) -> Self:
        if min(self.size.x, self.size.y, self.size.z) <= 0.0:
            raise ValueError("box size components must be positive")
        return self


class PolygonPrismGeometryV1(FleetModel):
    kind: Literal["polygon_prism"] = "polygon_prism"
    footprint: list[Vec2V1] = Field(min_length=3, max_length=256)
    min_z: float
    max_z: float

    @model_validator(mode="after")
    def height_is_positive(self) -> Self:
        if self.max_z <= self.min_z:
            raise ValueError("polygon prism max_z must exceed min_z")
        return self


class MeshGeometryV1(FleetModel):
    kind: Literal["mesh"] = "mesh"
    mesh_ref: RegistryRefV1


ZoneGeometryV1 = Annotated[
    BoxGeometryV1 | PolygonPrismGeometryV1 | MeshGeometryV1,
    Field(discriminator="kind"),
]


class ZoneV1(FleetModel):
    schema_version: Literal[1] = 1
    zone_id: SafeId
    version: int = Field(ge=1)
    workcell_id: SafeId
    frame_id: SafeId
    classification: Literal[
        "operational",
        "shared",
        "restricted",
        "forbidden",
    ]
    geometry: ZoneGeometryV1
    approval: ApprovalV1


class RigidTransformV1(FleetModel):
    translation_m: Vec3V1
    rotation_xyzw: Annotated[list[float], Field(min_length=4, max_length=4)]

    @field_validator("rotation_xyzw")
    @classmethod
    def quaternion_is_normalized(cls, value: list[float]) -> list[float]:
        norm_squared = sum(component * component for component in value)
        if abs(norm_squared - 1.0) > 1e-5:
            raise ValueError("rotation quaternion must be normalized")
        return value


class CalibrationV1(FleetModel):
    schema_version: Literal[1] = 1
    calibration_id: SafeId
    version: int = Field(ge=1)
    source_frame: SafeId
    target_frame: SafeId
    transform: RigidTransformV1
    method: SafeId
    residual_m: float = Field(ge=0.0)
    sample_count: int = Field(ge=1)
    artifact_sha256: Sha256Digest
    approval: ApprovalV1

    @model_validator(mode="after")
    def frames_are_distinct(self) -> Self:
        if self.source_frame == self.target_frame:
            raise ValueError("calibration source and target frames must differ")
        return self


class SpatialContractBundleV1(FleetModel):
    zone: ZoneV1
    calibration: CalibrationV1


class RoleSelectorV2(FleetModel):
    requires: list[CapabilityRef] = Field(min_length=1, max_length=MAX_CAPABILITIES)
    zone_id: SafeId | None = None

    @field_validator("requires")
    @classmethod
    def capabilities_are_unique(cls, value: list[str]) -> list[str]:
        _ensure_unique(value, "selector requires")
        return value


class WorkflowRoleV2(FleetModel):
    role: SafeId
    cardinality: RoleCardinality
    selector: RoleSelectorV2
    access: AccessMode

    @field_validator("cardinality")
    @classmethod
    def cardinality_range_is_ordered(cls, value: str) -> str:
        parse_role_cardinality(value)
        return value


class RetryPolicyV1(FleetModel):
    max_total_attempts: int = Field(default=1, ge=1, le=100)


class ActionLifetimeV1(FleetModel):
    start_on: StepEventRef
    stop_on: StepEventRef
    on_unreachable: Literal["cancel", "complete"]

    @model_validator(mode="after")
    def event_edges_are_distinct(self) -> Self:
        if self.start_on == self.stop_on:
            raise ValueError("lifetime start_on and stop_on must differ")
        return self


def _validate_fan_out_shape(
    fan_out: FanOutMode | None,
    join: JoinMode | None,
    fan_out_count: int | None,
    binding_ordinals: list[int],
) -> None:
    if (fan_out is None) != (join is None):
        raise ValueError("fan_out and join must be supplied together")
    if fan_out == "first_n":
        if fan_out_count is None or binding_ordinals:
            raise ValueError("first_n requires only fan_out_count")
    elif fan_out == "explicit_binding_set":
        if fan_out_count is not None or not binding_ordinals:
            raise ValueError("explicit_binding_set requires only binding_ordinals")
        if any(ordinal < 0 for ordinal in binding_ordinals):
            raise ValueError("binding_ordinals cannot be negative")
        _ensure_unique(binding_ordinals, "binding_ordinals")
    elif fan_out_count is not None or binding_ordinals:
        raise ValueError(
            "fan_out_count and binding_ordinals require their matching fan_out mode"
        )


class PredicateV1(FleetModel):
    path: ResultPath | None = None
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "default"] | None = None
    value: JsonValue = None
    all_of: list["PredicateV1"] = Field(
        default_factory=list,
        max_length=64,
        alias="all",
        serialization_alias="all",
    )
    any_of: list["PredicateV1"] = Field(
        default_factory=list,
        max_length=64,
        alias="any",
        serialization_alias="any",
    )
    not_: "PredicateV1 | None" = Field(
        default=None,
        alias="not",
        serialization_alias="not",
    )

    @model_validator(mode="after")
    def predicate_shape_is_closed(self) -> Self:
        composition_count = sum(
            (
                bool(self.all_of),
                bool(self.any_of),
                self.not_ is not None,
            )
        )
        if self.op is not None:
            if composition_count:
                raise ValueError("operator predicates cannot contain composition nodes")
            if self.op == "default":
                if self.path is not None or self.value is not None:
                    raise ValueError("default predicates accept only op=default")
            elif self.path is None:
                raise ValueError("comparison predicates require a declared result path")
            else:
                _reject_connection_details(self.value, "predicate.value")
            return self

        if self.path is not None or self.value is not None:
            raise ValueError("path and value require a comparison operator")
        if composition_count != 1:
            raise ValueError("predicate requires exactly one all, any, or not node")
        if self.all_of and len(self.all_of) < 2:
            raise ValueError("all predicates require at least two children")
        if self.any_of and len(self.any_of) < 2:
            raise ValueError("any predicates require at least two children")
        return self


class WorkflowStepBaseV2(FleetModel):
    step_id: SafeId
    depends_on: list[SafeId] = Field(default_factory=list, max_length=MAX_WORKFLOW_STEPS)

    @field_validator("depends_on")
    @classmethod
    def dependencies_are_unique(cls, value: list[str]) -> list[str]:
        _ensure_unique(value, "depends_on")
        return value


class ActionStepV2(WorkflowStepBaseV2):
    kind: Literal["action"] = "action"
    role: SafeId
    capability: CapabilityRef
    parameters: dict[str, JsonValue] = Field(default_factory=dict, max_length=256)
    timeout_s: float = Field(gt=0.0, le=86_400.0)
    retry: RetryPolicyV1 = Field(default_factory=RetryPolicyV1)
    on_hold: OnHoldBehavior = "capability_default"
    compensation_step: SafeId | None = None
    fan_out: FanOutMode | None = None
    join: JoinMode | None = None
    fan_out_count: int | None = Field(default=None, ge=1, le=MAX_BOUND_ASSETS)
    binding_ordinals: list[int] = Field(default_factory=list, max_length=MAX_BOUND_ASSETS)
    lifetime: ActionLifetimeV1 | None = None

    @model_validator(mode="after")
    def fan_out_is_explicit(self) -> Self:
        _reject_connection_details(self.parameters)
        _validate_fan_out_shape(
            self.fan_out,
            self.join,
            self.fan_out_count,
            self.binding_ordinals,
        )
        return self


class WaitForObservationStepV2(WorkflowStepBaseV2):
    kind: Literal["wait_for_observation"] = "wait_for_observation"
    role: SafeId
    capability: CapabilityRef
    predicate: PredicateV1
    timeout_s: float = Field(gt=0.0, le=86_400.0)
    on_timeout: Literal["HOLD", "FAIL"] = "HOLD"
    fan_out: FanOutMode | None = None
    join: JoinMode | None = None
    fan_out_count: int | None = Field(default=None, ge=1, le=MAX_BOUND_ASSETS)
    binding_ordinals: list[int] = Field(default_factory=list, max_length=MAX_BOUND_ASSETS)

    @model_validator(mode="after")
    def fan_out_is_explicit(self) -> Self:
        _validate_fan_out_shape(
            self.fan_out,
            self.join,
            self.fan_out_count,
            self.binding_ordinals,
        )
        return self


class BarrierStepV2(WorkflowStepBaseV2):
    kind: Literal["barrier"] = "barrier"
    join: JoinMode = "all"


class ConditionalCaseV2(FleetModel):
    when: PredicateV1
    next_step: SafeId = Field(alias="next", serialization_alias="next")


class ConditionalStepV2(WorkflowStepBaseV2):
    kind: Literal["conditional"] = "conditional"
    join: JoinMode = "all"
    cases: list[ConditionalCaseV2] = Field(min_length=2, max_length=64)

    @model_validator(mode="after")
    def cases_have_one_final_default(self) -> Self:
        defaults = [case for case in self.cases if case.when.op == "default"]
        if len(defaults) != 1 or self.cases[-1].when.op != "default":
            raise ValueError("conditional requires exactly one final default case")
        return self


class HumanApprovalStepV2(WorkflowStepBaseV2):
    kind: Literal["human_approval"] = "human_approval"
    authority: SafeId
    timeout_s: float = Field(gt=0.0, le=86_400.0)
    on_timeout: Literal["HOLD", "FAIL"] = "HOLD"


class HumanTaskStepV2(WorkflowStepBaseV2):
    kind: Literal["human_task"] = "human_task"
    authority: SafeId
    instruction: BoundedText
    timeout_s: float = Field(gt=0.0, le=86_400.0)
    completion_predicate: PredicateV1 | None = None
    on_timeout: Literal["HOLD", "FAIL"] = "HOLD"

    @field_validator("instruction")
    @classmethod
    def instruction_is_deployment_neutral(cls, value: str) -> str:
        _reject_connection_details(value, "human_task.instruction")
        return value


class CompensationStepV2(WorkflowStepBaseV2):
    kind: Literal["compensation"] = "compensation"
    role: SafeId
    capability: CapabilityRef
    parameters: dict[str, JsonValue] = Field(default_factory=dict, max_length=256)
    timeout_s: float = Field(gt=0.0, le=86_400.0)
    on_failure: Literal["HOLD", "FAIL"] = "HOLD"
    fan_out: FanOutMode | None = None
    join: JoinMode | None = None
    fan_out_count: int | None = Field(default=None, ge=1, le=MAX_BOUND_ASSETS)
    binding_ordinals: list[int] = Field(default_factory=list, max_length=MAX_BOUND_ASSETS)

    @model_validator(mode="after")
    def fan_out_is_explicit(self) -> Self:
        _reject_connection_details(self.parameters)
        _validate_fan_out_shape(
            self.fan_out,
            self.join,
            self.fan_out_count,
            self.binding_ordinals,
        )
        return self


WorkflowStepV2 = Annotated[
    ActionStepV2
    | WaitForObservationStepV2
    | BarrierStepV2
    | ConditionalStepV2
    | HumanApprovalStepV2
    | HumanTaskStepV2
    | CompensationStepV2,
    Field(discriminator="kind"),
]


class WorkflowDefinitionV2(FleetModel):
    schema_version: Literal[2] = 2
    workflow_id: SafeId
    version: int = Field(ge=1)
    roles: list[WorkflowRoleV2] = Field(min_length=1, max_length=MAX_WORKFLOW_ROLES)
    steps: list[WorkflowStepV2] = Field(min_length=1, max_length=MAX_WORKFLOW_STEPS)

    @model_validator(mode="after")
    def graph_and_references_are_coherent(self) -> Self:
        roles = {role.role: role for role in self.roles}
        if len(roles) != len(self.roles):
            raise ValueError("workflow role names must be unique")
        steps = {step.step_id: step for step in self.steps}
        if len(steps) != len(self.steps):
            raise ValueError("workflow step_ids must be unique")

        adjacency: dict[str, set[str]] = {step_id: set() for step_id in steps}
        for step in self.steps:
            for dependency in step.depends_on:
                if dependency not in steps:
                    raise ValueError(
                        f"step {step.step_id} references missing dependency {dependency}"
                    )
                if dependency == step.step_id:
                    raise ValueError(f"step {step.step_id} cannot depend on itself")
                adjacency[dependency].add(step.step_id)

            if isinstance(
                step,
                (ActionStepV2, WaitForObservationStepV2, CompensationStepV2),
            ):
                role = roles.get(step.role)
                if role is None:
                    raise ValueError(
                        f"step {step.step_id} references missing role {step.role}"
                    )
                if step.capability not in role.selector.requires:
                    raise ValueError(
                        f"step {step.step_id} capability is absent from role selector"
                    )
                minimum, maximum = parse_role_cardinality(role.cardinality)
                is_multi = maximum is None or maximum > 1 or minimum > 1
                if isinstance(
                    step,
                    (ActionStepV2, WaitForObservationStepV2, CompensationStepV2),
                ):
                    if is_multi and step.fan_out is None:
                        raise ValueError(
                            f"multi-asset step {step.step_id} requires fan_out and join"
                        )
                    selection_count = None
                    if step.fan_out == "first_n":
                        selection_count = step.fan_out_count
                    elif step.fan_out == "explicit_binding_set":
                        selection_count = len(step.binding_ordinals)
                    if selection_count is not None and (
                        selection_count < minimum
                        or (maximum is not None and selection_count > maximum)
                    ):
                        raise ValueError(
                            f"step {step.step_id} fan_out selects {selection_count} "
                            f"assets outside role cardinality {role.cardinality}"
                        )

            if isinstance(step, ActionStepV2):
                if step.compensation_step is not None:
                    target = steps.get(step.compensation_step)
                    if not isinstance(target, CompensationStepV2):
                        raise ValueError(
                            f"step {step.step_id} compensation must reference a "
                            "compensation step"
                        )
                if step.lifetime is not None:
                    for event_ref in (
                        step.lifetime.start_on,
                        step.lifetime.stop_on,
                    ):
                        referenced_step = _event_step_id(event_ref)
                        if referenced_step not in steps:
                            raise ValueError(
                                f"step {step.step_id} lifetime references missing "
                                f"step {referenced_step}"
                            )
                        if referenced_step == step.step_id:
                            raise ValueError(
                                f"step {step.step_id} lifetime cannot reference itself"
                            )

            if isinstance(step, ConditionalStepV2):
                for case in step.cases:
                    if case.next_step not in steps:
                        raise ValueError(
                            f"conditional {step.step_id} references missing next step "
                            f"{case.next_step}"
                        )
                    if case.next_step == step.step_id:
                        raise ValueError(
                            f"conditional {step.step_id} cannot branch to itself"
                        )
                    adjacency[step.step_id].add(case.next_step)

        indegree = {step_id: 0 for step_id in adjacency}
        for successors in adjacency.values():
            for successor in successors:
                indegree[successor] += 1
        ready = [step_id for step_id, degree in indegree.items() if degree == 0]
        visited = 0
        while ready:
            step_id = ready.pop()
            visited += 1
            for successor in adjacency[step_id]:
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    ready.append(successor)
        if visited != len(adjacency):
            raise ValueError("workflow graph contains a cycle")
        return self


class RuntimeAssetV1(FleetModel):
    asset_id: SafeId
    runtime_class: RuntimeClass
    manifest_ref: ManifestRefV1
    connection_ref: RegistryRefV1
    calibration_refs: list[RegistryRefV1] = Field(default_factory=list, max_length=64)
    capabilities: list[CapabilityRef] = Field(min_length=1, max_length=MAX_CAPABILITIES)
    supported_modes: list[ExecutionMode] = Field(min_length=1, max_length=8)
    command_sink_present: bool = False
    commissioned_capabilities: list[CapabilityRef] = Field(
        default_factory=list,
        max_length=MAX_CAPABILITIES,
    )
    output_isolation_evidence_ref: RegistryRefV1 | None = None
    coordinated_group_ref: RegistryRefV1 | None = None

    @model_validator(mode="after")
    def runtime_asset_is_coherent(self) -> Self:
        _ensure_unique(self.calibration_refs, "calibration_refs")
        _ensure_unique(self.capabilities, "capabilities")
        _ensure_unique(self.supported_modes, "supported_modes")
        _ensure_unique(self.commissioned_capabilities, "commissioned_capabilities")
        unknown = sorted(set(self.commissioned_capabilities) - set(self.capabilities))
        if unknown:
            raise ValueError(
                f"commissioned capabilities are not advertised: {unknown}"
            )
        if self.commissioned_capabilities and not self.command_sink_present:
            raise ValueError(
                "commissioned capabilities require command_sink_present"
            )
        return self


class RoleBindingV1(FleetModel):
    role: SafeId
    asset_ids: list[SafeId] = Field(min_length=1, max_length=MAX_BOUND_ASSETS)

    @field_validator("asset_ids")
    @classmethod
    def assets_are_unique(cls, value: list[str]) -> list[str]:
        _ensure_unique(value, "role binding asset_ids")
        return value


class RuntimeBindingV1(FleetModel):
    schema_version: Literal[1] = 1
    binding_id: SafeId
    run_id: SafeId
    workflow_ref: WorkflowRefV2
    execution_mode: ExecutionMode
    assets: list[RuntimeAssetV1] = Field(min_length=1, max_length=MAX_BOUND_ASSETS)
    role_bindings: list[RoleBindingV1] = Field(
        min_length=1,
        max_length=MAX_WORKFLOW_ROLES,
    )

    @model_validator(mode="after")
    def local_references_are_coherent(self) -> Self:
        asset_ids = [asset.asset_id for asset in self.assets]
        _ensure_unique(asset_ids, "runtime asset_ids")
        roles = [binding.role for binding in self.role_bindings]
        _ensure_unique(roles, "role bindings")
        known_assets = set(asset_ids)
        referenced = {
            asset_id
            for binding in self.role_bindings
            for asset_id in binding.asset_ids
        }
        missing = sorted(referenced - known_assets)
        if missing:
            raise ValueError(f"role bindings reference missing assets: {missing}")
        return self


def validate_capability_physical_readiness(
    definition: CapabilityDefinitionV1,
) -> None:
    """Reject uncommissioned zero/absent timing for a physical effect."""

    if definition.effect == "observation":
        return
    if not definition.max_clock_uncertainty_ms:
        raise ValueError(
            f"{definition.ref} max_clock_uncertainty_ms is not commissioned"
        )
    if not definition.max_demand_age_ms:
        raise ValueError(f"{definition.ref} max_demand_age_ms is not commissioned")


def validate_runtime_binding(
    workflow: WorkflowDefinitionV2,
    binding: RuntimeBindingV1,
    capability_definitions: list[CapabilityDefinitionV1],
    coordinated_groups: list["CoordinatedGroupV1"] | None = None,
) -> None:
    """Validate a Run binding without persisting or dispatching anything."""

    if binding.workflow_ref.workflow_id != workflow.workflow_id or (
        binding.workflow_ref.version != workflow.version
    ):
        raise ValueError("runtime binding pins a different workflow version")

    role_index = {role.role: role for role in workflow.roles}
    bound_role_index = {item.role: item for item in binding.role_bindings}
    if set(role_index) != set(bound_role_index):
        missing = sorted(set(role_index) - set(bound_role_index))
        extra = sorted(set(bound_role_index) - set(role_index))
        raise ValueError(f"runtime role bindings mismatch; missing={missing}, extra={extra}")

    asset_index = {asset.asset_id: asset for asset in binding.assets}
    capability_refs = [definition.ref for definition in capability_definitions]
    _ensure_unique(capability_refs, "capability definitions")
    capability_index = {definition.ref: definition for definition in capability_definitions}
    group_index = {
        group.group_asset_id: group for group in (coordinated_groups or [])
    }
    if len(group_index) != len(coordinated_groups or []):
        raise ValueError("coordinated group asset ids must be unique")

    allowed_runtime_classes: dict[ExecutionMode, set[RuntimeClass]] = {
        "OBSERVE_ONLY": {"physical", "simulation", "replay"},
        "SIMULATION": {"simulation"},
        "REPLAY": {"replay"},
        "SHADOW": {"physical", "simulation", "replay"},
        "HIL": {"physical", "simulation"},
        "PHYSICAL_AUTO": {"physical"},
        "PHYSICAL_TELEOP": {"physical"},
        "MAINTENANCE": {"physical"},
    }

    usage: dict[str, list[AccessMode]] = {}
    for role_name, role in role_index.items():
        role_binding = bound_role_index[role_name]
        minimum, maximum = parse_role_cardinality(role.cardinality)
        count = len(role_binding.asset_ids)
        if count < minimum or (maximum is not None and count > maximum):
            raise ValueError(
                f"role {role_name} binds {count} assets outside {role.cardinality}"
            )
        for asset_id in role_binding.asset_ids:
            asset = asset_index[asset_id]
            usage.setdefault(asset_id, []).append(role.access)
            missing_capabilities = sorted(
                set(role.selector.requires) - set(asset.capabilities)
            )
            if missing_capabilities:
                raise ValueError(
                    f"asset {asset_id} lacks role {role_name} capabilities: "
                    f"{missing_capabilities}"
                )
            if binding.execution_mode not in asset.supported_modes:
                raise ValueError(
                    f"asset {asset_id} does not support {binding.execution_mode}"
                )
            if asset.runtime_class not in allowed_runtime_classes[binding.execution_mode]:
                raise ValueError(
                    f"asset {asset_id} runtime class {asset.runtime_class} is "
                    f"incompatible with {binding.execution_mode}"
                )
            if (
                binding.execution_mode != "HIL"
                and asset.output_isolation_evidence_ref is not None
            ):
                raise ValueError(
                    "output isolation evidence is valid only for an HIL binding"
                )

    for asset_id, accesses in usage.items():
        if len(accesses) > 1 and any(access == "exclusive" for access in accesses):
            raise ValueError(f"exclusive asset {asset_id} is bound to multiple roles")

    unused_assets = sorted(set(asset_index) - set(usage))
    if unused_assets:
        raise ValueError(f"runtime binding contains unbound assets: {unused_assets}")

    for asset in binding.assets:
        if asset.coordinated_group_ref is None:
            continue
        group = group_index.get(asset.coordinated_group_ref.object_id)
        if group is None:
            raise ValueError(
                f"asset {asset.asset_id} references an unavailable coordinated group"
            )
        if asset.asset_id != group.group_asset_id or (
            asset.coordinated_group_ref.version != group.version
        ):
            raise ValueError("coordinated group reference does not match the bound asset")
        missing_group_capabilities = sorted(
            set(group.capabilities) - set(asset.capabilities)
        )
        if missing_group_capabilities:
            raise ValueError(
                "bound group asset lacks group capabilities: "
                f"{missing_group_capabilities}"
            )
        overlapping_members = sorted(set(group.member_asset_ids) & set(usage))
        if overlapping_members:
            raise ValueError(
                "coordinated group members cannot be bound independently: "
                f"{overlapping_members}"
            )

    effect_free_modes = {"OBSERVE_ONLY", "REPLAY", "SHADOW"}
    for step in workflow.steps:
        if not isinstance(
            step,
            (ActionStepV2, WaitForObservationStepV2, CompensationStepV2),
        ):
            continue
        definition = capability_index.get(step.capability)
        if definition is None:
            raise ValueError(f"missing capability definition {step.capability}")
        if binding.execution_mode not in definition.allowed_execution_modes:
            raise ValueError(
                f"capability {step.capability} does not allow {binding.execution_mode}"
            )
        if isinstance(
            step,
            (ActionStepV2, WaitForObservationStepV2, CompensationStepV2),
        ):
            bound_count = len(bound_role_index[step.role].asset_ids)
            if (
                step.fan_out == "first_n"
                and step.fan_out_count is not None
                and step.fan_out_count > bound_count
            ):
                raise ValueError(
                    f"step {step.step_id} fan_out_count exceeds its bound assets"
                )
            if step.fan_out == "explicit_binding_set" and (
                max(step.binding_ordinals) >= bound_count
            ):
                raise ValueError(
                    f"step {step.step_id} binding_ordinals exceed its bound assets"
                )
        if (
            binding.execution_mode in effect_free_modes
            and definition.effect != "observation"
        ):
            raise ValueError(
                f"{binding.execution_mode} forbids effectful capability {step.capability}"
            )
        if binding.execution_mode in {
            "PHYSICAL_AUTO",
            "PHYSICAL_TELEOP",
            "MAINTENANCE",
        } and definition.effect != "observation":
            validate_capability_physical_readiness(definition)
            for asset_id in bound_role_index[step.role].asset_ids:
                asset = asset_index[asset_id]
                if not asset.command_sink_present:
                    raise ValueError(
                        f"physical effect asset {asset_id} has no command sink"
                    )
                if step.capability not in asset.commissioned_capabilities:
                    raise ValueError(
                        f"physical capability {step.capability} is not commissioned "
                        f"for {asset_id}"
                    )
        if (
            binding.execution_mode == "HIL"
            and definition.effect != "observation"
        ):
            for asset_id in bound_role_index[step.role].asset_ids:
                asset = asset_index[asset_id]
                if asset.runtime_class == "physical" and (
                    not asset.command_sink_present
                    or asset.output_isolation_evidence_ref is None
                ):
                    raise ValueError(
                        f"physical HIL asset {asset_id} requires a controller sink "
                        "and output-isolation evidence"
                    )


class CoordinatedGroupV1(FleetModel):
    schema_version: Literal[1] = 1
    group_asset_id: SafeId
    version: int = Field(ge=1)
    member_asset_ids: list[SafeId] = Field(min_length=2, max_length=64)
    capabilities: list[CapabilityRef] = Field(min_length=1, max_length=MAX_CAPABILITIES)
    reservation_mode: Literal["transitive_exclusive"] = "transitive_exclusive"
    cancel_scope: Literal["all_or_nothing"] = "all_or_nothing"
    max_command_skew_ms: float = Field(gt=0.0)

    @model_validator(mode="after")
    def group_members_are_unique(self) -> Self:
        _ensure_unique(self.member_asset_ids, "group member_asset_ids")
        _ensure_unique(self.capabilities, "group capabilities")
        if self.group_asset_id in self.member_asset_ids:
            raise ValueError("group cannot contain its own asset identity")
        return self


class ControlEnvelopeV1(FleetModel):
    schema_version: Literal[1] = 1
    envelope_id: SafeId
    version: int = Field(ge=1)
    capabilities: list[CapabilityRef] = Field(min_length=1, max_length=MAX_CAPABILITIES)
    max_rate_hz: float = Field(gt=0.0)
    max_demand_age_ms: float = Field(gt=0.0)
    max_clock_uncertainty_ms: float = Field(gt=0.0)
    declared_time_to_safe_state_ms: float = Field(gt=0.0)
    max_delta_by_axis: dict[SafeId, float] = Field(min_length=1, max_length=128)
    workspace_ref: RegistryRefV1 | None = None
    approval: ApprovalV1

    @field_validator("max_delta_by_axis")
    @classmethod
    def deltas_are_positive(cls, value: dict[str, float]) -> dict[str, float]:
        if any(delta <= 0.0 for delta in value.values()):
            raise ValueError("control envelope deltas must be positive")
        return value

    @field_validator("capabilities")
    @classmethod
    def capabilities_are_unique(cls, value: list[str]) -> list[str]:
        _ensure_unique(value, "control envelope capabilities")
        return value


class DelegationGrantV1(FleetModel):
    schema_version: Literal[1] = 1
    grant_id: UUID
    run_id: SafeId
    orchestrator_id: ScopedId
    orchestrator_boot_id: UUID
    asset_id: SafeId
    execution_mode: ExecutionMode
    authorization_id: SafeId
    manifest_sha256: Sha256Digest
    control_envelope_id: SafeId
    control_envelope_sha256: Sha256Digest
    epoch: int = Field(ge=1)
    capabilities: list[CapabilityRef] = Field(min_length=1, max_length=MAX_CAPABILITIES)
    delegated_key_id: SafeId
    delegated_public_key_ref: SchemaReference
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    renewable_until: AwareDatetime
    max_sublease_ttl_ms: int = Field(gt=0, le=3_600_000)
    output_isolation_evidence_ref: RegistryRefV1 | None = None
    issuer_key_id: SafeId
    signature: Annotated[str, StringConstraints(min_length=16, max_length=8192)]

    @model_validator(mode="after")
    def grant_is_coherent(self) -> Self:
        _ensure_unique(self.capabilities, "grant capabilities")
        if not self.issued_at < self.expires_at <= self.renewable_until:
            raise ValueError(
                "grant times must satisfy issued_at < expires_at <= renewable_until"
            )
        if self.execution_mode not in {
            "SIMULATION",
            "HIL",
            "PHYSICAL_AUTO",
            "PHYSICAL_TELEOP",
            "MAINTENANCE",
        }:
            raise ValueError("delegation grants are not valid for effect-free modes")
        if self.execution_mode == "HIL":
            if self.output_isolation_evidence_ref is None:
                raise ValueError("HIL grants require output-isolation evidence")
        elif self.output_isolation_evidence_ref is not None:
            raise ValueError("output-isolation evidence is valid only for HIL grants")
        return self


class LeaseSubjectV1(FleetModel):
    kind: Literal["service", "human"]
    principal_id: ScopedId


class ControlSubleaseV1(FleetModel):
    schema_version: Literal[1] = 1
    lease_id: UUID
    lease_kind: Literal["auto", "teleop"]
    parent_grant_id: UUID
    run_id: SafeId
    orchestrator_id: ScopedId
    orchestrator_boot_id: UUID
    command_id: UUID
    subject: LeaseSubjectV1
    xr_session_id: UUID | None = None
    presence_attestation_ref: SchemaReference | None = None
    asset_id: SafeId
    capabilities: list[CapabilityRef] = Field(min_length=1, max_length=MAX_CAPABILITIES)
    execution_mode: ExecutionMode
    authorization_id: SafeId
    control_envelope_id: SafeId
    control_envelope_sha256: Sha256Digest
    epoch: int = Field(ge=1)
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    renewable_until: AwareDatetime
    output_isolation_evidence_ref: RegistryRefV1 | None = None
    issuer_key_id: SafeId
    signature: Annotated[str, StringConstraints(min_length=16, max_length=8192)]

    @model_validator(mode="after")
    def lease_is_coherent(self) -> Self:
        _ensure_unique(self.capabilities, "sublease capabilities")
        if not self.issued_at < self.expires_at <= self.renewable_until:
            raise ValueError(
                "sublease times must satisfy issued_at < expires_at <= renewable_until"
            )
        if self.lease_kind == "teleop":
            if self.execution_mode != "PHYSICAL_TELEOP":
                raise ValueError("teleop leases require PHYSICAL_TELEOP mode")
            if self.subject.kind != "human":
                raise ValueError("teleop leases require a human subject")
            if self.xr_session_id is None or self.presence_attestation_ref is None:
                raise ValueError(
                    "teleop leases require XR session and presence attestation"
                )
        else:
            if self.execution_mode == "PHYSICAL_TELEOP":
                raise ValueError("auto leases cannot use PHYSICAL_TELEOP mode")
            if self.xr_session_id is not None or self.presence_attestation_ref is not None:
                raise ValueError("auto leases cannot contain XR session fields")
        if self.execution_mode == "HIL":
            if self.output_isolation_evidence_ref is None:
                raise ValueError("HIL subleases require output-isolation evidence")
        elif self.output_isolation_evidence_ref is not None:
            raise ValueError("output-isolation evidence is valid only for HIL subleases")
        return self


def validate_control_sublease(
    grant: DelegationGrantV1,
    lease: ControlSubleaseV1,
    envelope: ControlEnvelopeV1,
    envelope_sha256: str,
) -> None:
    """Verify every non-cryptographic delegation fence shared by grant and lease."""

    comparisons = {
        "parent_grant_id": (lease.parent_grant_id, grant.grant_id),
        "run_id": (lease.run_id, grant.run_id),
        "orchestrator_id": (lease.orchestrator_id, grant.orchestrator_id),
        "orchestrator_boot_id": (
            lease.orchestrator_boot_id,
            grant.orchestrator_boot_id,
        ),
        "asset_id": (lease.asset_id, grant.asset_id),
        "execution_mode": (lease.execution_mode, grant.execution_mode),
        "authorization_id": (lease.authorization_id, grant.authorization_id),
        "control_envelope_id": (
            lease.control_envelope_id,
            grant.control_envelope_id,
        ),
        "control_envelope_sha256": (
            lease.control_envelope_sha256,
            grant.control_envelope_sha256,
        ),
        "epoch": (lease.epoch, grant.epoch),
        "issuer_key_id": (lease.issuer_key_id, grant.delegated_key_id),
        "output_isolation_evidence_ref": (
            lease.output_isolation_evidence_ref,
            grant.output_isolation_evidence_ref,
        ),
    }
    for label, (actual, expected) in comparisons.items():
        if actual != expected:
            raise ValueError(f"sublease {label} does not match parent grant")

    unauthorized = sorted(set(lease.capabilities) - set(grant.capabilities))
    if unauthorized:
        raise ValueError(f"sublease capabilities exceed parent grant: {unauthorized}")
    if envelope.envelope_id != grant.control_envelope_id or (
        envelope_sha256 != grant.control_envelope_sha256
    ):
        raise ValueError("resolved control envelope does not match parent grant")
    unenveloped = sorted(set(grant.capabilities) - set(envelope.capabilities))
    if unenveloped:
        raise ValueError(f"grant capabilities exceed control envelope: {unenveloped}")
    if envelope.approval.state != "approved":
        raise ValueError("control envelope is not approved")
    if (
        envelope.approval.expires_at is not None
        and lease.expires_at > envelope.approval.expires_at
    ):
        raise ValueError("sublease exceeds control-envelope approval expiry")
    if lease.issued_at < grant.issued_at:
        raise ValueError("sublease cannot be issued before its parent grant")
    if lease.expires_at > grant.expires_at:
        raise ValueError("sublease cannot expire after its parent grant")
    if lease.renewable_until > grant.renewable_until:
        raise ValueError("sublease renewable_until exceeds its parent grant")
    ttl_ms = (lease.expires_at - lease.issued_at).total_seconds() * 1000.0
    if ttl_ms > grant.max_sublease_ttl_ms:
        raise ValueError("sublease TTL exceeds parent max_sublease_ttl_ms")
    if ttl_ms < envelope.declared_time_to_safe_state_ms:
        raise ValueError("sublease TTL is shorter than declared time to safe state")


class ExecuteSkillGoalV1(FleetModel):
    schema_version: Literal[1] = 1
    command_id: UUID
    run_id: SafeId
    step_instance_id: SafeId
    attempt: int = Field(ge=1)
    asset_id: SafeId
    capability: CapabilityRef
    parameters: dict[str, JsonValue] = Field(default_factory=dict, max_length=256)
    artifact_id: SafeId | None = None
    artifact_sha256: Sha256Digest | None = None
    control_envelope_id: SafeId
    control_envelope_sha256: Sha256Digest
    authorization_id: SafeId
    parent_grant_id: UUID
    lease_id: UUID
    lease_epoch: int = Field(ge=1)
    orchestrator_boot_id: UUID
    execution_mode: ExecutionMode
    manifest_sha256: Sha256Digest
    calibration_refs: list[RegistryRefV1] = Field(default_factory=list, max_length=64)
    not_valid_after: AwareDatetime

    @model_validator(mode="after")
    def artifact_reference_is_complete(self) -> Self:
        if (self.artifact_id is None) != (self.artifact_sha256 is None):
            raise ValueError("artifact_id and artifact_sha256 must be supplied together")
        _ensure_unique(self.calibration_refs, "goal calibration_refs")
        return self


class ExecuteSkillResultV1(FleetModel):
    schema_version: Literal[1] = 1
    command_id: UUID
    outcome: Literal[
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "REJECTED",
        "UNKNOWN",
    ]
    reason_code: SafeId
    final_observation: dict[str, JsonValue] = Field(default_factory=dict, max_length=256)
    evidence_refs: list[SchemaReference] = Field(default_factory=list, max_length=128)
    completed_at: AwareDatetime


class ExecuteSkillFeedbackV1(FleetModel):
    schema_version: Literal[1] = 1
    command_id: UUID
    progress: float = Field(ge=0.0, le=1.0)
    phase: SafeId
    observed_state: dict[str, JsonValue] = Field(default_factory=dict, max_length=256)
    sequence: int = Field(ge=0)
    observed_at: AwareDatetime


class PolicyDemandV1(FleetModel):
    schema_version: Literal[1] = 1
    policy_session_id: UUID
    command_id: UUID
    sequence: int = Field(ge=0)
    sampled_at: AwareDatetime
    policy_sha256: Sha256Digest
    lease_id: UUID
    lease_epoch: int = Field(ge=1)
    frame_id: SafeId
    units: dict[SafeId, SafeId] = Field(min_length=1, max_length=64)
    demand: dict[str, JsonValue] = Field(min_length=1, max_length=256)


class ExecutionContractBundleV1(FleetModel):
    """Schema-export bundle; runtime messages are transported separately."""

    control_envelope: ControlEnvelopeV1
    coordinated_group: CoordinatedGroupV1
    delegation_grant: DelegationGrantV1
    control_sublease: ControlSubleaseV1
    execute_skill_goal: ExecuteSkillGoalV1
    execute_skill_feedback: ExecuteSkillFeedbackV1
    execute_skill_result: ExecuteSkillResultV1
    policy_demand: PolicyDemandV1


class UnityAssemblyAttestationV1(FleetModel):
    assembly_name: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    sha256: Sha256Digest


class UnityBuildAttestationV1(FleetModel):
    schema_version: Literal[1] = 1
    build_id: SafeId
    build_role: Literal["observer", "simulation_executor", "fixture"]
    source_repository: SchemaReference
    source_commit: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9a-f]{7,64}$"),
    ]
    unity_version: Annotated[str, StringConstraints(min_length=3, max_length=64)]
    scene_profile_sha256: Sha256Digest
    assemblies: list[UnityAssemblyAttestationV1] = Field(min_length=1, max_length=512)
    built_at: AwareDatetime
    signer_key_id: SafeId
    signature: Annotated[str, StringConstraints(min_length=16, max_length=8192)]

    @model_validator(mode="after")
    def assembly_names_are_unique(self) -> Self:
        _ensure_unique(
            [assembly.assembly_name for assembly in self.assemblies],
            "Unity assembly names",
        )
        return self


class ClockMeasurementV2(FleetModel):
    monitor_id: SafeId
    reference_id: SafeId
    sampled_at: AwareDatetime
    offset_ms: float
    uncertainty_ms: float = Field(ge=0.0)
    state: Literal["synchronized", "degraded", "unknown"]


class SeparationObservationV2(FleetModel):
    observation_id: SafeId
    human_id: SafeId
    asset_id: SafeId
    sampled_at: AwareDatetime
    frame_id: SafeId
    calibration_ref: RegistryRefV1
    distance_m: float | None = Field(default=None, ge=0.0)
    required: bool = True
    valid: bool
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def valid_observation_has_a_measurement(self) -> Self:
        if self.valid and self.distance_m is None:
            raise ValueError("valid separation observations require distance_m")
        return self


class ZoneOccupancyV2(FleetModel):
    observation_id: SafeId
    entity_id: SafeId
    asset_ids: list[SafeId] = Field(default_factory=list, max_length=64)
    sampled_at: AwareDatetime
    zone_ref: RegistryRefV1
    occupied: bool | None = None
    required: bool = True
    valid: bool
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("asset_ids")
    @classmethod
    def assets_are_unique(cls, value: list[str]) -> list[str]:
        _ensure_unique(value, "zone occupancy asset_ids")
        return value

    @model_validator(mode="after")
    def valid_observation_has_a_measurement(self) -> Self:
        if self.valid and self.occupied is None:
            raise ValueError("valid zone occupancy observations require occupied")
        return self


class SafetySignalV2(FleetModel):
    signal_id: SafeId
    signal_type: SafeId
    sampled_at: AwareDatetime
    asset_ids: list[SafeId] = Field(default_factory=list, max_length=64)
    value: JsonValue
    required: bool = True
    valid: bool = True
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("asset_ids")
    @classmethod
    def assets_are_unique(cls, value: list[str]) -> list[str]:
        _ensure_unique(value, "safety signal asset_ids")
        return value

    @model_validator(mode="after")
    def valid_signal_has_a_value(self) -> Self:
        if self.valid and self.value is None:
            raise ValueError("valid safety signals require a non-null value")
        return self


class WorkflowStateEvidenceV2(FleetModel):
    expected_step: SafeId
    expected_by_orchestrator_id: ScopedId
    observed_step: SafeId
    observed_by_producer_id: ScopedId
    executor_id: ScopedId
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def sources_are_independent(self) -> Self:
        if self.observed_by_producer_id in {
            self.expected_by_orchestrator_id,
            self.executor_id,
        }:
            raise ValueError(
                "observed workflow state requires an independent observer identity"
            )
        return self


class AssetManifestEvidenceRefV2(FleetModel):
    asset_id: SafeId
    manifest_ref: ManifestRefV1


class SafetySnapshotV2(FleetModel):
    schema_version: Literal[2] = 2
    snapshot_id: SafeId
    producer_id: ScopedId
    producer_boot_id: UUID
    sequence: int = Field(ge=0)
    run_id: SafeId
    captured_at: AwareDatetime
    received_at: AwareDatetime
    clock: ClockMeasurementV2
    asset_ids: list[SafeId] = Field(min_length=1, max_length=256)
    manifest_refs: list[AssetManifestEvidenceRefV2] = Field(
        min_length=1,
        max_length=256,
    )
    zone_refs: list[RegistryRefV1] = Field(default_factory=list, max_length=128)
    calibration_refs: list[RegistryRefV1] = Field(default_factory=list, max_length=128)
    policy_ref: RegistryRefV1
    separations: list[SeparationObservationV2] = Field(
        default_factory=list,
        max_length=512,
    )
    zone_occupancies: list[ZoneOccupancyV2] = Field(
        default_factory=list,
        max_length=512,
    )
    signals: list[SafetySignalV2] = Field(default_factory=list, max_length=512)
    workflow_state: WorkflowStateEvidenceV2 | None = None
    evidence_refs: list[SchemaReference] = Field(default_factory=list, max_length=256)
    command_emitted: Literal[False] = False

    @model_validator(mode="after")
    def safety_references_are_coherent(self) -> Self:
        _ensure_unique(self.asset_ids, "safety snapshot asset_ids")
        _ensure_unique(
            [item.asset_id for item in self.manifest_refs],
            "safety snapshot manifest asset_ids",
        )
        _ensure_unique(self.zone_refs, "safety snapshot zone_refs")
        _ensure_unique(self.calibration_refs, "safety snapshot calibration_refs")
        _ensure_unique(self.evidence_refs, "safety snapshot evidence_refs")
        _ensure_unique(
            [item.observation_id for item in self.separations],
            "separation observation_ids",
        )
        _ensure_unique(
            [item.observation_id for item in self.zone_occupancies],
            "zone occupancy observation_ids",
        )
        _ensure_unique(
            [item.signal_id for item in self.signals],
            "safety signal_ids",
        )

        known_assets = set(self.asset_ids)
        manifest_assets = {item.asset_id for item in self.manifest_refs}
        if manifest_assets != known_assets:
            missing_manifests = sorted(known_assets - manifest_assets)
            extra_manifests = sorted(manifest_assets - known_assets)
            raise ValueError(
                "safety snapshot manifest bindings mismatch; "
                f"missing={missing_manifests}, extra={extra_manifests}"
            )
        if self.workflow_state is not None and (
            self.workflow_state.observed_by_producer_id != self.producer_id
        ):
            raise ValueError(
                "workflow-state observer identity must match snapshot producer_id"
            )
        referenced_assets = {
            observation.asset_id for observation in self.separations
        }
        for occupancy in self.zone_occupancies:
            referenced_assets.update(occupancy.asset_ids)
        for signal in self.signals:
            referenced_assets.update(signal.asset_ids)
        missing = sorted(referenced_assets - known_assets)
        if missing:
            raise ValueError(f"safety snapshot references unknown assets: {missing}")
        return self
