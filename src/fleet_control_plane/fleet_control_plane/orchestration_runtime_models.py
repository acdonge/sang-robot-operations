"""Runtime-only models layered on the frozen orchestration wire contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from fleet_control_plane.models import FleetModel, SafeId, ShortText
from fleet_control_plane.orchestration_models import (
    AccessMode,
    ApprovalV1,
    ExecutionMode,
    RegistryRefV1,
    RuntimeBindingV1,
    WorkflowDefinitionV2,
)


RegistryKind = Literal[
    "calibration",
    "capability_profile",
    "connection_snapshot",
    "control_envelope",
    "coordinated_group",
    "device_manifest",
    "output_isolation_evidence",
    "plan_artifact",
    "policy_artifact",
    "safety_policy",
    "runtime_binding",
    "unity_build_attestation",
    "workflow",
    "zone",
]
OrchestrationRunState = Literal[
    "DRAFT",
    "READY",
    "ACTIVE",
    "HELD",
    "ABORTING",
    "ABORTED",
    "RECONCILING",
    "COMPLETE",
    "FAILED",
]
ReservationResourceKind = Literal[
    "asset",
    "coordinated_group",
    "process_resource",
    "workcell",
    "zone",
]


class GenericRegistryObjectV1(FleetModel):
    """Strict wrapper for versioned objects without a dedicated G0 model."""

    schema_version: Literal[1] = 1
    object_id: SafeId
    version: int = Field(ge=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict, max_length=512)
    approval_state: Literal["draft", "approved", "revoked"] = "draft"


class SeparationRuleV2(FleetModel):
    human_id: SafeId
    asset_id: SafeId
    warning_distance_m: float = Field(gt=0.0)
    unsafe_distance_m: float = Field(ge=0.0)
    required: bool = True

    @model_validator(mode="after")
    def thresholds_are_ordered(self) -> Self:
        if self.unsafe_distance_m >= self.warning_distance_m:
            raise ValueError("unsafe distance must be below warning distance")
        return self


class SafetyPolicyV2(FleetModel):
    schema_version: Literal[2] = 2
    policy_id: SafeId
    version: int = Field(ge=1)
    execution_modes: list[ExecutionMode] = Field(min_length=1, max_length=8)
    max_snapshot_age_ms: int = Field(gt=0, le=60_000)
    max_signal_age_ms: int = Field(gt=0, le=60_000)
    max_clock_uncertainty_ms: float = Field(ge=0.0)
    minimum_required_confidence: float = Field(gt=0.0, le=1.0)
    separation_rules: list[SeparationRuleV2] = Field(default_factory=list, max_length=256)
    required_signal_types: list[SafeId] = Field(default_factory=list, max_length=256)
    required_calibration_refs: list[RegistryRefV1] = Field(
        default_factory=list, max_length=128
    )
    required_zone_refs: list[RegistryRefV1] = Field(default_factory=list, max_length=128)
    approval: ApprovalV1

    @model_validator(mode="after")
    def policy_entries_are_unique(self) -> Self:
        if len(self.execution_modes) != len(set(self.execution_modes)):
            raise ValueError("safety policy execution_modes must be unique")
        pair_ids = [
            (rule.human_id, rule.asset_id) for rule in self.separation_rules
        ]
        if len(pair_ids) != len(set(pair_ids)):
            raise ValueError("safety separation pairs must be unique")
        if len(self.required_signal_types) != len(set(self.required_signal_types)):
            raise ValueError("required signal types must be unique")
        calibration_ids = [
            (item.object_id, item.version, item.sha256)
            for item in self.required_calibration_refs
        ]
        if len(calibration_ids) != len(set(calibration_ids)):
            raise ValueError("required calibration refs must be unique")
        zone_ids = [
            (item.object_id, item.version, item.sha256)
            for item in self.required_zone_refs
        ]
        if len(zone_ids) != len(set(zone_ids)):
            raise ValueError("required zone refs must be unique")
        return self


class ResourceRequestV1(FleetModel):
    resource_kind: Literal["process_resource", "zone"]
    resource_id: SafeId
    access_mode: AccessMode = "exclusive"

    @model_validator(mode="after")
    def safety_and_process_resources_are_exclusive(self) -> Self:
        if self.access_mode != "exclusive":
            raise ValueError("zone and process resources require exclusive access")
        return self


class OrchestrationRunCreateV1(FleetModel):
    schema_version: Literal[1] = 1
    run_id: SafeId
    display_name: ShortText
    workcell_id: SafeId
    workflow_id: SafeId
    workflow_version: int = Field(ge=1)
    binding_id: SafeId
    execution_mode: ExecutionMode
    orchestrator_id: SafeId
    resources: list[ResourceRequestV1] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def resources_are_unique(self) -> Self:
        identities = [
            (resource.resource_kind, resource.resource_id)
            for resource in self.resources
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("run resources must be unique")
        if self.execution_mode in {
            "PHYSICAL_AUTO",
            "PHYSICAL_TELEOP",
            "MAINTENANCE",
        }:
            raise ValueError(
                "physical execution modes are unavailable before G7 commissioning"
            )
        return self


class OrchestrationRunTransitionV1(FleetModel):
    schema_version: Literal[1] = 1
    expected_revision: int = Field(ge=1)
    state: OrchestrationRunState
    reason: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def reason_matches_transition(self) -> Self:
        if self.state in {"HELD", "ABORTING", "ABORTED", "FAILED", "RECONCILING"}:
            if self.reason is None:
                raise ValueError(f"{self.state} transitions require a reason")
        elif self.reason is not None:
            raise ValueError("reason is accepted only for non-nominal transitions")
        return self


class ProducerBindingCreateV1(FleetModel):
    schema_version: Literal[1] = 1
    producer_id: SafeId
    asset_ids: list[SafeId] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def assets_are_unique(self) -> Self:
        if len(self.asset_ids) != len(set(self.asset_ids)):
            raise ValueError("producer asset_ids must be unique")
        return self


class ImmutableRunContractV1(FleetModel):
    run_id: SafeId
    workflow: WorkflowDefinitionV2
    binding: RuntimeBindingV1
    execution_mode: ExecutionMode
    workcell_id: SafeId
    orchestrator_id: SafeId


class PersistedReservationV1(FleetModel):
    run_id: SafeId
    resource_kind: ReservationResourceKind
    resource_id: SafeId
    access_mode: AccessMode
    source: str = Field(min_length=1, max_length=256)
    released_at: str | None = None


class DerivedProvenanceV1(FleetModel):
    source_kind: Literal["live", "replay", "synthetic"]
    execution_mode: ExecutionMode
    producer_id: SafeId
    run_id: SafeId
    binding_ref: RegistryRefV1
