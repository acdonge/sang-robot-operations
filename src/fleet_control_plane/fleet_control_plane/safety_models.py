"""Versioned, command-free contract for Unity safety Shadow Mode."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

from fleet_control_plane.models import FleetModel, SafeId


SafetyState = Literal["SAFE", "WARNING", "UNSAFE", "UNKNOWN"]
SafetyReasonSeverity = Literal["WARNING", "UNSAFE", "UNKNOWN"]
SafetySourceKind = Literal["live", "replay", "synthetic"]
ClockQuality = Literal["synchronized", "degraded", "unknown"]
RecommendedResponse = Literal["none", "operator_review", "task_pause_candidate"]
BoundedText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=512, strip_whitespace=True),
]
EvidenceReference = Annotated[
    str,
    StringConstraints(min_length=1, max_length=2048, strip_whitespace=True),
]


class SafetySignalV1(FleetModel):
    signal_id: SafeId
    sampled_at: AwareDatetime
    asset_id: SafeId | None = None
    required: bool = True
    valid: bool = True
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ForbiddenZoneOccupancyV1(FleetModel):
    zone_id: SafeId
    entity_id: SafeId
    asset_ids: list[SafeId] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def asset_ids_are_unique(self) -> "ForbiddenZoneOccupancyV1":
        if len(self.asset_ids) != len(set(self.asset_ids)):
            raise ValueError("zone occupancy asset_ids must be unique")
        return self


class TaskProgressV1(FleetModel):
    expected_step: SafeId
    observed_step: SafeId


class SafetySnapshotV1(FleetModel):
    schema_version: Literal[1] = 1
    snapshot_id: SafeId
    source_kind: SafetySourceKind
    agent_id: SafeId
    run_id: SafeId
    captured_at: AwareDatetime
    ttl_ms: int = Field(default=500, ge=10, le=10_000)
    coordinate_frame: SafeId
    clock_quality: ClockQuality
    asset_ids: list[SafeId] = Field(min_length=1, max_length=256)
    signals: list[SafetySignalV1] = Field(min_length=1, max_length=256)
    human_robot_distance_m: float | None = Field(default=None, ge=0.0)
    tracking_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    forbidden_zone_occupancies: list[ForbiddenZoneOccupancyV1] = Field(
        default_factory=list,
        max_length=128,
    )
    task_progress: TaskProgressV1 | None = None
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def references_are_coherent(self) -> "SafetySnapshotV1":
        if len(self.asset_ids) != len(set(self.asset_ids)):
            raise ValueError("asset_ids must be unique")
        signal_ids = [signal.signal_id for signal in self.signals]
        if len(signal_ids) != len(set(signal_ids)):
            raise ValueError("signal_ids must be unique")
        known_assets = set(self.asset_ids)
        referenced_assets = {
            signal.asset_id for signal in self.signals if signal.asset_id is not None
        }
        for occupancy in self.forbidden_zone_occupancies:
            referenced_assets.update(occupancy.asset_ids)
        missing = sorted(referenced_assets - known_assets)
        if missing:
            raise ValueError(f"snapshot references undeclared asset_ids: {missing}")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence_refs must be unique")
        return self


class SafetyPolicyV1(FleetModel):
    schema_version: Literal[1] = 1
    policy_id: SafeId
    version: int = Field(ge=1)
    warning_distance_m: float = Field(default=1.0, gt=0.0)
    unsafe_distance_m: float = Field(default=0.5, ge=0.0)
    max_signal_age_ms: int = Field(default=250, ge=1, le=10_000)
    future_tolerance_ms: int = Field(default=2_000, ge=0, le=10_000)
    minimum_tracking_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    require_human_distance: bool = True
    task_sequence_mismatch_state: Literal["WARNING", "UNSAFE"] = "UNSAFE"
    degraded_clock_state: Literal["WARNING", "UNKNOWN"] = "WARNING"

    @model_validator(mode="after")
    def distance_thresholds_are_ordered(self) -> "SafetyPolicyV1":
        if self.unsafe_distance_m >= self.warning_distance_m:
            raise ValueError("unsafe_distance_m must be below warning_distance_m")
        return self


class SafetyReasonV1(FleetModel):
    code: SafeId
    severity: SafetyReasonSeverity
    message: BoundedText
    asset_ids: list[SafeId] = Field(default_factory=list, max_length=64)
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def assets_are_unique(self) -> "SafetyReasonV1":
        if len(self.asset_ids) != len(set(self.asset_ids)):
            raise ValueError("reason asset_ids must be unique")
        return self


class SafetyAssessmentV1(FleetModel):
    schema_version: Literal[1] = 1
    assessment_id: SafeId
    snapshot_id: SafeId
    source_kind: SafetySourceKind
    agent_id: SafeId
    run_id: SafeId
    evaluator_id: SafeId
    evaluator_version: SafeId
    policy_id: SafeId
    policy_version: int = Field(ge=1)
    state: SafetyState
    assessed_at: AwareDatetime
    input_captured_at: AwareDatetime
    valid_until: AwareDatetime
    confidence: float = Field(ge=0.0, le=1.0)
    asset_ids: list[SafeId] = Field(min_length=1, max_length=256)
    reasons: list[SafetyReasonV1] = Field(default_factory=list, max_length=128)
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=64)
    recommended_response: RecommendedResponse
    shadow_mode: Literal[True] = True
    command_emitted: Literal[False] = False

    @model_validator(mode="after")
    def result_is_coherent(self) -> "SafetyAssessmentV1":
        if self.valid_until < self.input_captured_at:
            raise ValueError("valid_until cannot precede input_captured_at")
        validity_ms = (
            self.valid_until - self.input_captured_at
        ).total_seconds() * 1_000.0
        if not 10.0 <= validity_ms <= 10_000.0:
            raise ValueError("assessment validity must be between 10 and 10000 ms")
        if len(self.asset_ids) != len(set(self.asset_ids)):
            raise ValueError("asset_ids must be unique")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence_refs must be unique")

        expected_response = {
            "SAFE": "none",
            "WARNING": "operator_review",
            "UNSAFE": "task_pause_candidate",
            "UNKNOWN": "task_pause_candidate",
        }[self.state]
        if self.recommended_response != expected_response:
            raise ValueError(
                f"{self.state} assessments require {expected_response}"
            )

        severities = {reason.severity for reason in self.reasons}
        if self.state == "SAFE" and self.reasons:
            raise ValueError("SAFE assessments cannot contain reasons")
        if self.state != "SAFE" and not self.reasons:
            raise ValueError(f"{self.state} assessments require at least one reason")
        if self.state == "WARNING" and severities - {"WARNING"}:
            raise ValueError("WARNING assessments may contain only WARNING reasons")
        if self.state == "UNKNOWN" and (
            "UNKNOWN" not in severities or severities - {"WARNING", "UNKNOWN"}
        ):
            raise ValueError(
                "UNKNOWN assessments require UNKNOWN and may include WARNING reasons"
            )
        if self.state == "UNSAFE" and "UNSAFE" not in severities:
            raise ValueError("UNSAFE assessments require an UNSAFE reason")
        return self


class SafetyContractV1(FleetModel):
    """Schema-export bundle; runtime messages use the three nested models separately."""

    snapshot: SafetySnapshotV1
    policy: SafetyPolicyV1
    assessment: SafetyAssessmentV1


def safety_contract_schema() -> dict[str, object]:
    schema = SafetyContractV1.model_json_schema(
        ref_template="#/$defs/{model}",
        mode="validation",
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://sang.local/contracts/safety/shadow-v1.schema.json"
    schema["description"] = (
        "SANG command-free Unity safety Shadow Mode contract. "
        "The bundle documents three independently transported message types."
    )
    return schema
