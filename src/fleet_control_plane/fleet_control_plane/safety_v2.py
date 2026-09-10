"""Fail-closed Safety Evidence v2 evaluator and source-ordered persistence."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from fleet_control_plane.models import FleetModel, SafeId
from fleet_control_plane.orchestration_models import (
    AssetManifestEvidenceRefV2,
    ClockMeasurementV2,
    RegistryRefV1,
    SchemaReference,
    ScopedId,
    SafetySignalV2,
    SafetySnapshotV2,
    SeparationObservationV2,
    WorkflowStateEvidenceV2,
    ZoneOccupancyV2,
)
from fleet_control_plane.orchestration_runtime_models import (
    DerivedProvenanceV1,
    SafetyPolicyV2,
)
from fleet_control_plane.orchestration_models import ExecutionMode
from fleet_control_plane.orchestration_store import (
    OrchestrationInvalid,
    OrchestrationStore,
    canonical_json,
)
from fleet_control_plane.status import isoformat_z, parse_datetime


SAFETY_V2_MIGRATION = """
CREATE TABLE IF NOT EXISTS safety_v2_producer_state (
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id) ON DELETE RESTRICT,
    producer_id TEXT NOT NULL,
    current_boot_id TEXT NOT NULL,
    max_sequence INTEGER NOT NULL CHECK (max_sequence >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, producer_id)
);

CREATE TABLE IF NOT EXISTS safety_v2_boot_history (
    run_id TEXT NOT NULL,
    producer_id TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    first_sequence INTEGER NOT NULL CHECK (first_sequence >= 0),
    max_sequence INTEGER NOT NULL CHECK (max_sequence >= 0),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (run_id, producer_id, boot_id)
);

CREATE TABLE IF NOT EXISTS safety_v2_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id TEXT NOT NULL UNIQUE,
    snapshot_id TEXT NOT NULL UNIQUE,
    snapshot_sha256 TEXT NOT NULL CHECK (length(snapshot_sha256) = 64),
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id) ON DELETE RESTRICT,
    producer_id TEXT NOT NULL,
    producer_boot_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('live', 'replay', 'synthetic')),
    state TEXT NOT NULL CHECK (state IN ('SAFE', 'WARNING', 'UNSAFE', 'UNKNOWN')),
    captured_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS safety_v2_assessments_run
    ON safety_v2_assessments (run_id, id DESC);

CREATE TABLE IF NOT EXISTS safety_v2_latest (
    run_id TEXT NOT NULL,
    producer_id TEXT NOT NULL,
    assessment_id TEXT NOT NULL REFERENCES safety_v2_assessments(assessment_id)
        ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (state IN ('SAFE', 'WARNING', 'UNSAFE', 'UNKNOWN')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, producer_id)
);
"""


SafetyStateV2 = Literal["SAFE", "WARNING", "UNSAFE", "UNKNOWN"]


class SafetyEvidenceIngressV2(FleetModel):
    """Producer payload; provenance, receive time, and clock are intentionally absent."""

    schema_version: Literal[2] = 2
    snapshot_id: SafeId
    producer_id: ScopedId
    producer_boot_id: UUID
    sequence: int = Field(ge=0)
    run_id: SafeId
    captured_at: AwareDatetime
    asset_ids: list[SafeId] = Field(min_length=1, max_length=256)
    manifest_refs: list[AssetManifestEvidenceRefV2] = Field(
        min_length=1, max_length=256
    )
    zone_refs: list[RegistryRefV1] = Field(default_factory=list, max_length=128)
    calibration_refs: list[RegistryRefV1] = Field(default_factory=list, max_length=128)
    policy_ref: RegistryRefV1
    separations: list[SeparationObservationV2] = Field(default_factory=list, max_length=512)
    zone_occupancies: list[ZoneOccupancyV2] = Field(default_factory=list, max_length=512)
    signals: list[SafetySignalV2] = Field(default_factory=list, max_length=512)
    workflow_state: WorkflowStateEvidenceV2 | None = None
    evidence_refs: list[SchemaReference] = Field(default_factory=list, max_length=256)
    command_emitted: Literal[False] = False

    @model_validator(mode="after")
    def references_are_valid_snapshot(self) -> Self:
        synthetic_clock = ClockMeasurementV2(
            monitor_id="contract-validation",
            reference_id="contract-validation",
            sampled_at=self.captured_at,
            offset_ms=0.0,
            uncertainty_ms=0.0,
            state="unknown",
        )
        SafetySnapshotV2.model_validate(
            {
                **self.model_dump(mode="json"),
                "received_at": self.captured_at,
                "clock": synthetic_clock.model_dump(mode="json"),
            }
        )
        return self


class SafetyReasonV2(FleetModel):
    code: SafeId
    severity: Literal["WARNING", "UNSAFE", "UNKNOWN"]
    message: str = Field(min_length=1, max_length=512)
    details: dict[str, JsonValue] = Field(default_factory=dict, max_length=128)


class SafetyAssessmentV2(FleetModel):
    schema_version: Literal[2] = 2
    assessment_id: SafeId
    snapshot_id: SafeId
    run_id: SafeId
    producer_id: SafeId
    producer_boot_id: UUID
    sequence: int = Field(ge=0)
    source_kind: Literal["live", "replay", "synthetic"]
    execution_mode: ExecutionMode
    policy_ref: RegistryRefV1
    state: SafetyStateV2
    confidence: float = Field(ge=0.0, le=1.0)
    captured_at: AwareDatetime
    received_at: AwareDatetime
    valid_until: AwareDatetime
    reasons: list[SafetyReasonV2] = Field(default_factory=list, max_length=512)
    recommended_response: Literal["none", "operator_review", "task_pause_candidate"]
    software_hold_requested: bool
    physical_estop_claimed: Literal[False] = False
    command_emitted: Literal[False] = False

    @model_validator(mode="after")
    def state_matches_response(self) -> Self:
        expected = {
            "SAFE": "none",
            "WARNING": "operator_review",
            "UNSAFE": "task_pause_candidate",
            "UNKNOWN": "task_pause_candidate",
        }[self.state]
        if self.recommended_response != expected:
            raise ValueError("assessment response does not match its state")
        if self.software_hold_requested != (self.state in {"UNSAFE", "UNKNOWN"}):
            raise ValueError("software hold flag does not match assessment state")
        if self.state == "SAFE" and self.reasons:
            raise ValueError("SAFE assessment cannot contain reasons")
        return self


class SafetyV2Conflict(ValueError):
    pass


class SafetyV2Invalid(ValueError):
    pass


def _reason(
    code: str,
    severity: Literal["WARNING", "UNSAFE", "UNKNOWN"],
    message: str,
    **details: JsonValue,
) -> SafetyReasonV2:
    return SafetyReasonV2(
        code=code,
        severity=severity,
        message=message,
        details=details,
    )


def _contains_ref(values: list[RegistryRefV1], expected: RegistryRefV1) -> bool:
    return any(value == expected for value in values)


def evaluate_safety_v2(
    snapshot: SafetySnapshotV2,
    policy: SafetyPolicyV2,
    provenance: DerivedProvenanceV1,
    *,
    ordering_issue: str | None = None,
    registry: OrchestrationStore | None = None,
) -> SafetyAssessmentV2:
    received = parse_datetime(snapshot.received_at)
    captured = parse_datetime(snapshot.captured_at)
    reasons: list[SafetyReasonV2] = []
    confidence_values: list[float] = []

    if received < captured:
        reasons.append(
            _reason(
                "INPUT_TIME_INVALID",
                "UNKNOWN",
                "Central receive time precedes producer capture time.",
            )
        )
    age_ms = (received - captured).total_seconds() * 1000.0
    if age_ms > policy.max_snapshot_age_ms:
        reasons.append(
            _reason(
                "SNAPSHOT_STALE",
                "UNKNOWN",
                "Safety evidence exceeded the policy snapshot age.",
                age_ms=round(age_ms, 3),
                maximum_ms=policy.max_snapshot_age_ms,
            )
        )
    if provenance.execution_mode not in policy.execution_modes:
        reasons.append(
            _reason(
                "MODE_NOT_APPROVED",
                "UNKNOWN",
                "Safety policy does not approve this Run execution mode.",
                execution_mode=provenance.execution_mode,
            )
        )
    if ordering_issue is not None:
        reasons.append(
            _reason(
                "SOURCE_ORDER_INVALID",
                "UNKNOWN",
                "Producer boot/sequence ordering is invalid.",
                issue=ordering_issue,
            )
        )

    clock = snapshot.clock
    clock_age_ms = (received - parse_datetime(clock.sampled_at)).total_seconds() * 1000.0
    if clock.state != "synchronized":
        reasons.append(
            _reason(
                "CLOCK_NOT_SYNCHRONIZED",
                "UNKNOWN",
                "Trusted local clock monitor is not synchronized.",
                state=clock.state,
            )
        )
    if clock_age_ms < 0 or clock_age_ms > policy.max_signal_age_ms:
        reasons.append(
            _reason(
                "CLOCK_MEASUREMENT_STALE",
                "UNKNOWN",
                "Trusted local clock measurement is stale or future-dated.",
                age_ms=round(clock_age_ms, 3),
            )
        )
    if clock.uncertainty_ms > policy.max_clock_uncertainty_ms:
        reasons.append(
            _reason(
                "CLOCK_UNCERTAINTY_EXCEEDED",
                "UNKNOWN",
                "Measured clock uncertainty exceeds policy.",
                uncertainty_ms=clock.uncertainty_ms,
                maximum_ms=policy.max_clock_uncertainty_ms,
            )
        )

    missing_calibrations = [
        reference.object_id
        for reference in policy.required_calibration_refs
        if not _contains_ref(snapshot.calibration_refs, reference)
    ]
    if missing_calibrations:
        reasons.append(
            _reason(
                "CALIBRATION_MISSING",
                "UNKNOWN",
                "Required calibration evidence is missing.",
                object_ids=missing_calibrations,
            )
        )
    missing_zones = [
        reference.object_id
        for reference in policy.required_zone_refs
        if not _contains_ref(snapshot.zone_refs, reference)
    ]
    if missing_zones:
        reasons.append(
            _reason(
                "ZONE_CONTRACT_MISSING",
                "UNKNOWN",
                "Required zone contract is missing.",
                object_ids=missing_zones,
            )
        )

    if registry is not None:
        for kind, references in (
            ("calibration", snapshot.calibration_refs),
            ("zone", snapshot.zone_refs),
        ):
            for reference in references:
                record = registry.get_registry(
                    kind, reference.object_id, reference.version
                )
                if record is None or record["payload_sha256"] != reference.sha256:
                    reasons.append(
                        _reason(
                            "REGISTRY_REFERENCE_INVALID",
                            "UNKNOWN",
                            "Safety evidence references missing or mismatched registry content.",
                            kind=kind,
                            object_id=reference.object_id,
                        )
                    )
                elif record["approval_state"] != "approved":
                    reasons.append(
                        _reason(
                            "REGISTRY_REFERENCE_UNAPPROVED",
                            "UNKNOWN",
                            "Safety evidence references an unapproved registry object.",
                            kind=kind,
                            object_id=reference.object_id,
                        )
                    )

    signals_by_type: dict[str, list[SafetySignalV2]] = {}
    for signal in snapshot.signals:
        signals_by_type.setdefault(signal.signal_type, []).append(signal)
    for signal_type in policy.required_signal_types:
        candidates = signals_by_type.get(signal_type, [])
        if not candidates:
            reasons.append(
                _reason(
                    "REQUIRED_SIGNAL_MISSING",
                    "UNKNOWN",
                    "A required safety signal type is absent.",
                    signal_type=signal_type,
                )
            )
            continue
        for signal in candidates:
            signal_age_ms = (
                received - parse_datetime(signal.sampled_at)
            ).total_seconds() * 1000.0
            if not signal.valid:
                reasons.append(
                    _reason(
                        "REQUIRED_SIGNAL_INVALID",
                        "UNKNOWN",
                        "A required safety signal is invalid.",
                        signal_id=signal.signal_id,
                    )
                )
            if signal.confidence is None:
                reasons.append(
                    _reason(
                        "REQUIRED_CONFIDENCE_MISSING",
                        "UNKNOWN",
                        "A required safety signal lacks measured confidence.",
                        signal_id=signal.signal_id,
                    )
                )
            else:
                confidence_values.append(signal.confidence)
                if signal.confidence < policy.minimum_required_confidence:
                    reasons.append(
                        _reason(
                            "REQUIRED_CONFIDENCE_LOW",
                            "UNKNOWN",
                            "A required safety signal confidence is below policy.",
                            signal_id=signal.signal_id,
                            confidence=signal.confidence,
                        )
                    )
            if signal_age_ms < 0 or signal_age_ms > policy.max_signal_age_ms:
                reasons.append(
                    _reason(
                        "REQUIRED_SIGNAL_STALE",
                        "UNKNOWN",
                        "A required safety signal is stale or future-dated.",
                        signal_id=signal.signal_id,
                        age_ms=round(signal_age_ms, 3),
                    )
                )

    separations = {
        (item.human_id, item.asset_id): item for item in snapshot.separations
    }
    for rule in policy.separation_rules:
        observation = separations.get((rule.human_id, rule.asset_id))
        if observation is None:
            if rule.required:
                reasons.append(
                    _reason(
                        "SEPARATION_MISSING",
                        "UNKNOWN",
                        "A required human/asset separation is absent.",
                        human_id=rule.human_id,
                        asset_id=rule.asset_id,
                    )
                )
            continue
        sampled_age_ms = (
            received - parse_datetime(observation.sampled_at)
        ).total_seconds() * 1000.0
        if not observation.valid or observation.distance_m is None:
            reasons.append(
                _reason(
                    "SEPARATION_INVALID",
                    "UNKNOWN",
                    "A required separation observation is invalid.",
                    observation_id=observation.observation_id,
                )
            )
        if observation.confidence is None:
            reasons.append(
                _reason(
                    "SEPARATION_CONFIDENCE_MISSING",
                    "UNKNOWN",
                    "A required separation lacks measured confidence.",
                    observation_id=observation.observation_id,
                )
            )
        else:
            confidence_values.append(observation.confidence)
            if observation.confidence < policy.minimum_required_confidence:
                reasons.append(
                    _reason(
                        "SEPARATION_CONFIDENCE_LOW",
                        "UNKNOWN",
                        "A separation confidence is below policy.",
                        observation_id=observation.observation_id,
                        confidence=observation.confidence,
                    )
                )
        if sampled_age_ms < 0 or sampled_age_ms > policy.max_signal_age_ms:
            reasons.append(
                _reason(
                    "SEPARATION_STALE",
                    "UNKNOWN",
                    "A required separation is stale or future-dated.",
                    observation_id=observation.observation_id,
                    age_ms=round(sampled_age_ms, 3),
                )
            )
        if observation.distance_m is not None:
            if observation.distance_m < rule.unsafe_distance_m:
                reasons.append(
                    _reason(
                        "SEPARATION_UNSAFE",
                        "UNSAFE",
                        "Human/asset separation is below the unsafe threshold.",
                        human_id=rule.human_id,
                        asset_id=rule.asset_id,
                        distance_m=observation.distance_m,
                    )
                )
            elif observation.distance_m < rule.warning_distance_m:
                reasons.append(
                    _reason(
                        "SEPARATION_WARNING",
                        "WARNING",
                        "Human/asset separation is below the warning threshold.",
                        human_id=rule.human_id,
                        asset_id=rule.asset_id,
                        distance_m=observation.distance_m,
                    )
                )

    for occupancy in snapshot.zone_occupancies:
        age = (received - parse_datetime(occupancy.sampled_at)).total_seconds() * 1000.0
        if occupancy.required and (
            not occupancy.valid
            or occupancy.confidence is None
            or occupancy.confidence < policy.minimum_required_confidence
            or age < 0
            or age > policy.max_signal_age_ms
        ):
            reasons.append(
                _reason(
                    "ZONE_OBSERVATION_INVALID",
                    "UNKNOWN",
                    "A required zone observation is invalid, stale, or lacks confidence.",
                    observation_id=occupancy.observation_id,
                )
            )
        if occupancy.confidence is not None:
            confidence_values.append(occupancy.confidence)
        if occupancy.valid and occupancy.occupied is True:
            reasons.append(
                _reason(
                    "FORBIDDEN_ZONE_OCCUPIED",
                    "UNSAFE",
                    "An observed entity occupies a restricted safety zone.",
                    entity_id=occupancy.entity_id,
                    zone_id=occupancy.zone_ref.object_id,
                )
            )

    workflow_state = snapshot.workflow_state
    if workflow_state is not None and (
        workflow_state.expected_step != workflow_state.observed_step
    ):
        reasons.append(
            _reason(
                "WORKFLOW_SEQUENCE_MISMATCH",
                "UNSAFE",
                "Independent observation differs from Orchestrator expected state.",
                expected_step=workflow_state.expected_step,
                observed_step=workflow_state.observed_step,
            )
        )

    severities = {item.severity for item in reasons}
    if "UNSAFE" in severities:
        state: SafetyStateV2 = "UNSAFE"
    elif "UNKNOWN" in severities:
        state = "UNKNOWN"
    elif "WARNING" in severities:
        state = "WARNING"
    else:
        state = "SAFE"
    if not confidence_values:
        reasons.append(
            _reason(
                "CONFIDENCE_EVIDENCE_MISSING",
                "UNKNOWN",
                "No measured confidence supports this assessment.",
            )
        )
        if state != "UNSAFE":
            state = "UNKNOWN"
    confidence = 0.0 if state == "UNKNOWN" else min(confidence_values, default=0.0)
    identity = {
        "snapshot": snapshot.model_dump(mode="json"),
        "policy": policy.model_dump(mode="json"),
        "provenance": provenance.model_dump(mode="json"),
        "ordering_issue": ordering_issue,
    }
    assessment_id = f"safety-v2-{hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:24]}"
    return SafetyAssessmentV2(
        assessment_id=assessment_id,
        snapshot_id=snapshot.snapshot_id,
        run_id=snapshot.run_id,
        producer_id=snapshot.producer_id,
        producer_boot_id=snapshot.producer_boot_id,
        sequence=snapshot.sequence,
        source_kind=provenance.source_kind,
        execution_mode=provenance.execution_mode,
        policy_ref=snapshot.policy_ref,
        state=state,
        confidence=confidence,
        captured_at=snapshot.captured_at,
        received_at=snapshot.received_at,
        valid_until=captured + timedelta(milliseconds=policy.max_snapshot_age_ms),
        reasons=reasons,
        recommended_response={
            "SAFE": "none",
            "WARNING": "operator_review",
            "UNSAFE": "task_pause_candidate",
            "UNKNOWN": "task_pause_candidate",
        }[state],
        software_hold_requested=state in {"UNSAFE", "UNKNOWN"},
    )


class SafetyV2Catalog:
    def __init__(self, path: str | Path, orchestration: OrchestrationStore) -> None:
        self.path = Path(path)
        self.orchestration = orchestration
        with self._connect() as connection:
            connection.executescript(SAFETY_V2_MIGRATION)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        return json.loads(row["payload_json"])

    def _ordering_issue(
        self,
        connection: sqlite3.Connection,
        snapshot: SafetyEvidenceIngressV2,
        received_at: str,
    ) -> str | None:
        boot_id = str(snapshot.producer_boot_id)
        state = connection.execute(
            """
            SELECT * FROM safety_v2_producer_state
            WHERE run_id = ? AND producer_id = ?
            """,
            (snapshot.run_id, snapshot.producer_id),
        ).fetchone()
        history = connection.execute(
            """
            SELECT * FROM safety_v2_boot_history
            WHERE run_id = ? AND producer_id = ? AND boot_id = ?
            """,
            (snapshot.run_id, snapshot.producer_id, boot_id),
        ).fetchone()
        issue = None
        if state is not None:
            if boot_id == state["current_boot_id"]:
                if snapshot.sequence <= state["max_sequence"]:
                    issue = "sequence_regression_or_duplicate"
            elif history is not None:
                issue = "retired_boot_id_reused"

        if issue is not None:
            return issue
        if history is None:
            connection.execute(
                """
                INSERT INTO safety_v2_boot_history (
                    run_id, producer_id, boot_id, first_sequence, max_sequence,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.run_id,
                    snapshot.producer_id,
                    boot_id,
                    snapshot.sequence,
                    snapshot.sequence,
                    received_at,
                    received_at,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE safety_v2_boot_history
                SET max_sequence = ?, last_seen_at = ?
                WHERE run_id = ? AND producer_id = ? AND boot_id = ?
                """,
                (
                    snapshot.sequence,
                    received_at,
                    snapshot.run_id,
                    snapshot.producer_id,
                    boot_id,
                ),
            )
        connection.execute(
            """
            INSERT INTO safety_v2_producer_state (
                run_id, producer_id, current_boot_id, max_sequence, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id, producer_id) DO UPDATE SET
                current_boot_id = excluded.current_boot_id,
                max_sequence = excluded.max_sequence,
                updated_at = excluded.updated_at
            """,
            (
                snapshot.run_id,
                snapshot.producer_id,
                boot_id,
                snapshot.sequence,
                received_at,
            ),
        )
        return None

    def accept(
        self,
        ingress: SafetyEvidenceIngressV2,
        *,
        received_at: datetime,
        measured_clock: ClockMeasurementV2,
    ) -> dict:
        run = self.orchestration.get_run(ingress.run_id)
        if run is None:
            raise SafetyV2Invalid(f"unknown orchestration Run: {ingress.run_id}")
        if run["state"] != "ACTIVE":
            raise SafetyV2Invalid("Safety Evidence v2 requires an ACTIVE Run")
        try:
            provenance = self.orchestration.derive_provenance(
                ingress.run_id, ingress.producer_id, ingress.asset_ids
            )
        except OrchestrationInvalid as exc:
            raise SafetyV2Invalid(str(exc)) from exc

        policy_record = self.orchestration.get_registry(
            "safety_policy", ingress.policy_ref.object_id, ingress.policy_ref.version
        )
        if policy_record is None:
            raise SafetyV2Invalid("safety policy is not registered")
        if policy_record["payload_sha256"] != ingress.policy_ref.sha256:
            raise SafetyV2Invalid("safety policy digest does not match")
        if policy_record["approval_state"] != "approved":
            raise SafetyV2Invalid("safety policy is not approved")
        policy = SafetyPolicyV2.model_validate(policy_record["payload"])
        if (
            policy.approval.expires_at is not None
            and parse_datetime(received_at) >= parse_datetime(policy.approval.expires_at)
        ):
            raise SafetyV2Invalid("safety policy approval has expired")

        snapshot = SafetySnapshotV2.model_validate(
            {
                **ingress.model_dump(mode="json"),
                "received_at": isoformat_z(received_at),
                "clock": measured_clock.model_dump(mode="json"),
            }
        )
        snapshot_json = canonical_json(snapshot.model_dump(mode="json"))
        snapshot_sha256 = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        held_run = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_run = connection.execute(
                "SELECT state FROM orchestration_runs WHERE run_id = ?",
                (ingress.run_id,),
            ).fetchone()
            if current_run is None or current_run["state"] != "ACTIVE":
                raise SafetyV2Invalid(
                    "Safety Evidence v2 Run stopped being ACTIVE during evaluation"
                )
            duplicate = connection.execute(
                "SELECT * FROM safety_v2_assessments WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            ).fetchone()
            if duplicate is not None:
                if duplicate["snapshot_sha256"] != snapshot_sha256:
                    raise SafetyV2Conflict(
                        "snapshot_id was already used with different evidence"
                    )
                duplicate_payload = self._decode(duplicate)
                if duplicate_payload["software_hold_requested"]:
                    held_run = self.orchestration.hold_run_in_transaction(
                        connection,
                        ingress.run_id,
                        reason=(
                            f"safety_v2:{duplicate_payload['state']}:"
                            f"{duplicate_payload['assessment_id']}"
                        ),
                        occurred_at=received_at,
                    )
                return {
                    **duplicate_payload,
                    "idempotent_replay": True,
                    "run_state": None if held_run is None else held_run["state"],
                }

            received = isoformat_z(received_at)
            ordering_issue = self._ordering_issue(connection, ingress, received)
            assessment = evaluate_safety_v2(
                snapshot,
                policy,
                provenance,
                ordering_issue=ordering_issue,
                registry=self.orchestration,
            )
            payload = assessment.model_dump(mode="json")
            connection.execute(
                """
                INSERT INTO safety_v2_assessments (
                    assessment_id, snapshot_id, snapshot_sha256, run_id,
                    producer_id, producer_boot_id, sequence, source_kind, state,
                    captured_at, received_at, valid_until, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    snapshot.snapshot_id,
                    snapshot_sha256,
                    snapshot.run_id,
                    snapshot.producer_id,
                    str(snapshot.producer_boot_id),
                    snapshot.sequence,
                    assessment.source_kind,
                    assessment.state,
                    isoformat_z(snapshot.captured_at),
                    received,
                    isoformat_z(assessment.valid_until),
                    canonical_json(payload),
                    received,
                ),
            )
            connection.execute(
                """
                INSERT INTO safety_v2_latest (
                    run_id, producer_id, assessment_id, state, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id, producer_id) DO UPDATE SET
                    assessment_id = excluded.assessment_id,
                    state = excluded.state,
                    updated_at = excluded.updated_at
                """,
                (
                    snapshot.run_id,
                    snapshot.producer_id,
                    assessment.assessment_id,
                    assessment.state,
                    received,
                ),
            )
            if assessment.software_hold_requested:
                held_run = self.orchestration.hold_run_in_transaction(
                    connection,
                    ingress.run_id,
                    reason=f"safety_v2:{assessment.state}:{assessment.assessment_id}",
                    occurred_at=received_at,
                )
        return {
            **payload,
            "idempotent_replay": False,
            "run_state": None if held_run is None else held_run["state"],
        }

    def list_assessments(self, run_id: str, *, limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM safety_v2_assessments
                WHERE run_id = ? ORDER BY id DESC LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [self._decode(row) for row in rows]
