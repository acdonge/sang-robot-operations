"""Append-only, non-authorizing G6b laboratory evidence workflow."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from fleet_control_plane.commissioning_readiness import ReadinessCheckV1
from fleet_control_plane.models import FleetModel, SafeId, ShortText
from fleet_control_plane.orchestration_models import (
    CapabilityRef,
    RegistryRefV1,
    SchemaReference,
    Sha256Digest,
)
from fleet_control_plane.recording_storage import FilesystemObjectStore, ObjectStoreError
from fleet_control_plane.status import isoformat_z, parse_datetime


MAX_G6B_EVIDENCE_BYTES = 2 * 1024 * 1024
MAX_G6B_ARTIFACT_BYTES = 64 * 1024 * 1024
G6B_ARTIFACT_URI_PREFIX = "g6b-artifact://"
SAFE_EVIDENCE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+ -]{0,255}$")
G6B_REVIEW_ROLES = frozenset({"manager", "safety_officer"})
G6B_REQUIRED_ROLES = frozenset({"edge_operator", "manager", "safety_officer"})
G6B_REQUIRED_SAFETY_FUNCTIONS = frozenset(
    {
        "emergency_stop",
        "protective_stop",
        "manual_reset",
        "power_restore_no_auto_resume",
    }
)
G6B_REQUIRED_FAULT_SCENARIOS = frozenset(
    {"cancellation", "lease_expiry", "network_loss", "controller_restart"}
)
G6B_REQUIRED_XR_LOSS_MODES = frozenset(
    {"tracking_loss", "focus_loss", "network_loss"}
)


G6B_EVIDENCE_MIGRATION = """
CREATE TABLE IF NOT EXISTS g6b_supporting_artifacts (
    artifact_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE CHECK (length(sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
    object_key TEXT NOT NULL UNIQUE,
    source_file_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    uploaded_by TEXT,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS g6b_commissioning_cases (
    case_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE RESTRICT,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE RESTRICT,
    case_sha256 TEXT NOT NULL UNIQUE CHECK (length(case_sha256) = 64),
    case_json TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS g6b_evidence_submissions (
    submission_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES g6b_commissioning_cases(case_id)
        ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    payload_size_bytes INTEGER NOT NULL CHECK (payload_size_bytes > 0),
    object_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT,
    submitted_at TEXT NOT NULL,
    UNIQUE (case_id, revision),
    UNIQUE (case_id, payload_sha256)
);

CREATE TABLE IF NOT EXISTS g6b_evidence_reviews (
    review_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL REFERENCES g6b_evidence_submissions(submission_id)
        ON DELETE RESTRICT,
    submission_sha256 TEXT NOT NULL CHECK (length(submission_sha256) = 64),
    review_role TEXT NOT NULL CHECK (review_role IN ('manager', 'safety_officer')),
    reviewer_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('accept', 'reject')),
    rationale TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    UNIQUE (submission_id, review_role)
);

CREATE INDEX IF NOT EXISTS g6b_submissions_case_revision
    ON g6b_evidence_submissions (case_id, revision DESC);
CREATE INDEX IF NOT EXISTS g6b_reviews_submission
    ON g6b_evidence_reviews (submission_id, review_role);

CREATE TRIGGER IF NOT EXISTS g6b_artifacts_no_update
BEFORE UPDATE ON g6b_supporting_artifacts
BEGIN SELECT RAISE(ABORT, 'G6b supporting artifacts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS g6b_artifacts_no_delete
BEFORE DELETE ON g6b_supporting_artifacts
BEGIN SELECT RAISE(ABORT, 'G6b supporting artifacts are append-only'); END;

CREATE TRIGGER IF NOT EXISTS g6b_cases_no_update
BEFORE UPDATE ON g6b_commissioning_cases
BEGIN SELECT RAISE(ABORT, 'G6b cases are immutable'); END;
CREATE TRIGGER IF NOT EXISTS g6b_cases_no_delete
BEFORE DELETE ON g6b_commissioning_cases
BEGIN SELECT RAISE(ABORT, 'G6b cases are append-only'); END;
CREATE TRIGGER IF NOT EXISTS g6b_submissions_no_update
BEFORE UPDATE ON g6b_evidence_submissions
BEGIN SELECT RAISE(ABORT, 'G6b submissions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS g6b_submissions_no_delete
BEFORE DELETE ON g6b_evidence_submissions
BEGIN SELECT RAISE(ABORT, 'G6b submissions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS g6b_reviews_no_update
BEFORE UPDATE ON g6b_evidence_reviews
BEGIN SELECT RAISE(ABORT, 'G6b reviews are immutable'); END;
CREATE TRIGGER IF NOT EXISTS g6b_reviews_no_delete
BEFORE DELETE ON g6b_evidence_reviews
BEGIN SELECT RAISE(ABORT, 'G6b reviews are append-only'); END;
"""


class G6bEvidenceInvalid(ValueError):
    """G6b evidence is incomplete, inconsistent, or unsafe to accept."""


class G6bEvidenceConflict(ValueError):
    """An immutable G6b identity already contains different content."""


class G6bEvidenceNotFound(ValueError):
    """A G6b case, submission, or review does not exist."""


class G6bArtifactRefV1(FleetModel):
    uri: SchemaReference
    sha256: Sha256Digest


class G6bEvidenceRecordV1(FleetModel):
    evidence_id: SafeId
    observed_at: AwareDatetime
    valid_until: AwareDatetime
    observer_ids: list[SafeId] = Field(min_length=1, max_length=32)
    artifact_refs: list[G6bArtifactRefV1] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def evidence_window_and_observers_are_valid(self) -> Self:
        if self.valid_until <= self.observed_at:
            raise ValueError("valid_until must be later than observed_at")
        if len(self.observer_ids) != len(set(self.observer_ids)):
            raise ValueError("observer_ids must be unique")
        refs = [(item.uri, item.sha256) for item in self.artifact_refs]
        if len(refs) != len(set(refs)):
            raise ValueError("artifact_refs must be unique")
        return self


StopType = Literal["controlled_stop", "protective_stop", "emergency_stop"]


class G6bStopScopeV1(FleetModel):
    scope_id: SafeId
    tool_configuration_id: SafeId
    payload_configuration_id: SafeId
    speed_envelope_ref: SchemaReference
    pose_envelope_ref: SchemaReference
    required_stop_types: list[StopType] = Field(min_length=1, max_length=3)

    @field_validator("required_stop_types")
    @classmethod
    def stop_types_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("required_stop_types must be unique")
        return value


class G6bCaseCreateV1(FleetModel):
    schema_version: Literal[1] = 1
    case_id: SafeId
    asset_id: SafeId
    manifest_ref: RegistryRefV1
    capability_refs: list[CapabilityRef] = Field(min_length=1, max_length=64)
    stop_scopes: list[G6bStopScopeV1] = Field(min_length=1, max_length=128)
    cell_frame_ref: SchemaReference
    xr_control_in_scope: bool
    shared_resources_in_scope: bool
    output_disconnected: Literal[True] = True
    command_sink_present: Literal[False] = False
    process_output_connected: Literal[False] = False

    @model_validator(mode="after")
    def scope_is_unique(self) -> Self:
        if len(self.capability_refs) != len(set(self.capability_refs)):
            raise ValueError("capability_refs must be unique")
        scope_ids = [item.scope_id for item in self.stop_scopes]
        if len(scope_ids) != len(set(scope_ids)):
            raise ValueError("stop scope ids must be unique")
        return self


class G6bRiskAssessmentEvidenceV1(G6bEvidenceRecordV1):
    protection_strategy: Literal["guarded_zone", "speed_separation_monitoring"]
    risk_assessment_id: SafeId
    accepted_by_lab: bool
    person_excluded_during_motion: bool


class G6bStopSampleV1(FleetModel):
    stop_time_ms: float = Field(gt=0.0)
    stop_distance_mm: float = Field(ge=0.0)


class G6bStopMeasurementEvidenceV1(G6bEvidenceRecordV1):
    stop_scope_id: SafeId
    stop_type: StopType
    maximum_permitted_stop_time_ms: float = Field(gt=0.0)
    maximum_permitted_stop_distance_mm: float = Field(gt=0.0)
    samples: list[G6bStopSampleV1] = Field(min_length=1, max_length=10_000)
    test_passed: bool


SafetyFunction = Literal[
    "emergency_stop",
    "protective_stop",
    "manual_reset",
    "power_restore_no_auto_resume",
]


class G6bSafetyChainEvidenceV1(G6bEvidenceRecordV1):
    tested_functions: list[SafetyFunction] = Field(min_length=1, max_length=4)
    independent_of_sang: bool
    manual_local_reset: bool
    no_automatic_restart: bool
    test_passed: bool

    @field_validator("tested_functions")
    @classmethod
    def safety_functions_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("tested_functions must be unique")
        return value


class G6bCalibrationEvidenceV1(G6bEvidenceRecordV1):
    cell_frame_ref: SchemaReference
    workspace_ref: SchemaReference
    calibration_method: ShortText
    error_samples_mm: list[float] = Field(min_length=1, max_length=10_000)
    maximum_permitted_error_mm: float = Field(gt=0.0)
    test_passed: bool

    @field_validator("error_samples_mm")
    @classmethod
    def errors_are_nonnegative(cls, value: list[float]) -> list[float]:
        if any(item < 0.0 for item in value):
            raise ValueError("calibration error samples cannot be negative")
        return value


FaultScenario = Literal[
    "cancellation",
    "lease_expiry",
    "network_loss",
    "controller_restart",
]
SafeBehavior = Literal["controlled_hold", "controlled_cancel", "safe_state", "inhibit"]


class G6bFaultResponseEvidenceV1(G6bEvidenceRecordV1):
    scenario: FaultScenario
    expected_behavior: SafeBehavior
    observed_behavior: SafeBehavior
    measured_time_to_safe_state_ms: float = Field(ge=0.0)
    maximum_permitted_time_to_safe_state_ms: float = Field(gt=0.0)
    test_passed: bool


class G6bLeaseTimingEvidenceV1(G6bEvidenceRecordV1):
    lease_expiry_ms: float = Field(gt=0.0)
    renewal_margin_ms: float = Field(gt=0.0)
    measured_time_to_safe_state_ms: float = Field(ge=0.0)
    maximum_permitted_time_to_safe_state_ms: float = Field(gt=0.0)
    test_passed: bool

    @model_validator(mode="after")
    def renewal_precedes_expiry(self) -> Self:
        if self.renewal_margin_ms >= self.lease_expiry_ms:
            raise ValueError("renewal_margin_ms must be shorter than lease_expiry_ms")
        return self


LabRole = Literal["edge_operator", "manager", "safety_officer"]


class G6bRoleAssignmentV1(FleetModel):
    role: LabRole
    principal_id: SafeId


class G6bRoleAssignmentsEvidenceV1(G6bEvidenceRecordV1):
    assignments: list[G6bRoleAssignmentV1] = Field(min_length=1, max_length=3)
    release_key_custody_documented: bool

    @model_validator(mode="after")
    def assignments_are_unique(self) -> Self:
        roles = [item.role for item in self.assignments]
        principals = [item.principal_id for item in self.assignments]
        if len(roles) != len(set(roles)):
            raise ValueError("lab role assignments must be unique")
        if len(principals) != len(set(principals)):
            raise ValueError("independent lab roles require distinct principal identities")
        return self


class G6bOperatingEnvelopeEvidenceV1(G6bEvidenceRecordV1):
    capability_refs: list[CapabilityRef] = Field(min_length=1, max_length=64)
    workspace_ref: SchemaReference
    speed_envelope_refs: list[SchemaReference] = Field(min_length=1, max_length=128)
    pose_envelope_refs: list[SchemaReference] = Field(min_length=1, max_length=128)
    tool_configuration_ids: list[SafeId] = Field(min_length=1, max_length=128)
    payload_configuration_ids: list[SafeId] = Field(min_length=1, max_length=128)
    command_sink_present: Literal[False] = False
    process_output_connected: Literal[False] = False
    test_passed: bool

    @model_validator(mode="after")
    def envelope_sets_are_unique(self) -> Self:
        for name, values in (
            ("capability_refs", self.capability_refs),
            ("speed_envelope_refs", self.speed_envelope_refs),
            ("pose_envelope_refs", self.pose_envelope_refs),
            ("tool_configuration_ids", self.tool_configuration_ids),
            ("payload_configuration_ids", self.payload_configuration_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must be unique")
        return self


XrLossMode = Literal["tracking_loss", "focus_loss", "network_loss"]


class G6bXrLossEvidenceV1(G6bEvidenceRecordV1):
    xr_control_in_scope: bool
    tested_loss_modes: list[XrLossMode] = Field(default_factory=list, max_length=3)
    intervention_inhibited_on_loss: bool
    manual_reauthorization_required: bool
    physical_estop_independent: bool
    xr_control_prohibited_when_unqualified: bool
    test_passed: bool

    @field_validator("tested_loss_modes")
    @classmethod
    def xr_loss_modes_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("tested_loss_modes must be unique")
        return value


class G6bResourceSharingEvidenceV1(G6bEvidenceRecordV1):
    sharing_permitted: bool
    shared_resource_refs: list[SchemaReference] = Field(default_factory=list, max_length=128)
    coordination_policy_ref: SchemaReference | None = None
    exclusive_reservations_required: bool
    test_passed: bool

    @model_validator(mode="after")
    def sharing_policy_is_coherent(self) -> Self:
        if self.sharing_permitted:
            if not self.shared_resource_refs or self.coordination_policy_ref is None:
                raise ValueError("shared resources require a coordination policy")
        elif self.shared_resource_refs or self.coordination_policy_ref is not None:
            raise ValueError("non-sharing evidence cannot declare shared resources")
        return self


class G6bEvidencePackageV1(FleetModel):
    schema_version: Literal[1] = 1
    case_id: SafeId
    revision: int = Field(ge=1)
    risk_assessment: G6bRiskAssessmentEvidenceV1 | None = None
    stop_measurements: list[G6bStopMeasurementEvidenceV1] = Field(
        default_factory=list,
        max_length=512,
    )
    safety_chain: G6bSafetyChainEvidenceV1 | None = None
    calibration: G6bCalibrationEvidenceV1 | None = None
    fault_response_tests: list[G6bFaultResponseEvidenceV1] = Field(
        default_factory=list,
        max_length=16,
    )
    lease_timing: G6bLeaseTimingEvidenceV1 | None = None
    role_assignments: G6bRoleAssignmentsEvidenceV1 | None = None
    operating_envelope: G6bOperatingEnvelopeEvidenceV1 | None = None
    xr_loss_response: G6bXrLossEvidenceV1 | None = None
    resource_sharing: G6bResourceSharingEvidenceV1 | None = None
    output_disconnected: Literal[True] = True
    command_sink_present: Literal[False] = False
    physical_motion_enabled: Literal[False] = False
    process_output_enabled: Literal[False] = False

    @model_validator(mode="after")
    def evidence_identities_are_unique(self) -> Self:
        records: list[G6bEvidenceRecordV1] = [
            *self.stop_measurements,
            *self.fault_response_tests,
        ]
        records.extend(
            item
            for item in (
                self.risk_assessment,
                self.safety_chain,
                self.calibration,
                self.lease_timing,
                self.role_assignments,
                self.operating_envelope,
                self.xr_loss_response,
                self.resource_sharing,
            )
            if item is not None
        )
        evidence_ids = [item.evidence_id for item in records]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence_id values must be unique within a submission")
        stop_keys = [(item.stop_scope_id, item.stop_type) for item in self.stop_measurements]
        if len(stop_keys) != len(set(stop_keys)):
            raise ValueError("stop measurement scope/type pairs must be unique")
        fault_scenarios = [item.scenario for item in self.fault_response_tests]
        if len(fault_scenarios) != len(set(fault_scenarios)):
            raise ValueError("fault response scenarios must be unique")
        return self


class G6bReviewRequestV1(FleetModel):
    schema_version: Literal[1] = 1
    reviewer_id: SafeId
    review_role: Literal["manager", "safety_officer"]
    decision: Literal["accept", "reject"]
    expected_submission_sha256: Sha256Digest
    rationale: str = Field(min_length=1, max_length=2048)


class G6bReadinessReportV1(FleetModel):
    schema_version: Literal[1] = 1
    case_id: SafeId
    asset_id: SafeId
    agent_id: SafeId
    submission_id: SafeId | None = None
    submission_revision: int | None = Field(default=None, ge=1)
    submission_sha256: Sha256Digest | None = None
    generated_at: AwareDatetime
    evidence_complete: bool
    lab_evidence_status: Literal["UNVERIFIED", "READY_FOR_REVIEW", "REVIEWED"]
    checks: list[ReadinessCheckV1]
    reviews: list[dict[str, Any]]
    blockers: list[str]
    command_sink_present: Literal[False] = False
    physical_motion_enabled: Literal[False] = False
    process_output_enabled: Literal[False] = False
    can_create_physical_manifest: Literal[False] = False
    g7_authorized: Literal[False] = False


def _canonical_bytes(model: FleetModel) -> bytes:
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _fresh(record: G6bEvidenceRecordV1 | None, now: datetime) -> bool:
    return bool(
        record is not None
        and parse_datetime(record.observed_at) <= parse_datetime(now)
        and parse_datetime(record.valid_until) > parse_datetime(now)
    )


class G6bEvidenceCatalog:
    """Durable G6b evidence and review ledger that cannot authorize G7."""

    def __init__(
        self,
        db_path: str | Path,
        object_store: FilesystemObjectStore,
    ) -> None:
        self.path = Path(db_path)
        self.object_store = object_store
        with self._connect() as connection:
            connection.executescript(G6B_EVIDENCE_MIGRATION)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _case_row(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["case_json"])
        return {
            **payload,
            "agent_id": row["agent_id"],
            "case_sha256": row["case_sha256"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _submission_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "submission_id": row["submission_id"],
            "case_id": row["case_id"],
            "revision": row["revision"],
            "payload_sha256": row["payload_sha256"],
            "payload_size_bytes": row["payload_size_bytes"],
            "submitted_by": row["submitted_by"],
            "submitted_at": row["submitted_at"],
        }

    @staticmethod
    def _review_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "review_id": row["review_id"],
            "submission_id": row["submission_id"],
            "submission_sha256": row["submission_sha256"],
            "review_role": row["review_role"],
            "reviewer_id": row["reviewer_id"],
            "decision": row["decision"],
            "rationale": row["rationale"],
            "reviewed_at": row["reviewed_at"],
        }

    @staticmethod
    def _artifact_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "artifact_id": row["artifact_id"],
            "uri": f"{G6B_ARTIFACT_URI_PREFIX}{row['artifact_id']}",
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "source_file_name": row["source_file_name"],
            "media_type": row["media_type"],
            "uploaded_by": row["uploaded_by"],
            "uploaded_at": row["uploaded_at"],
        }

    async def import_supporting_artifact(
        self,
        payload: bytes,
        *,
        source_file_name: str,
        media_type: str,
        expected_sha256: str,
        uploaded_by: str | None,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        if not 0 < len(payload) <= MAX_G6B_ARTIFACT_BYTES:
            raise G6bEvidenceInvalid("G6b supporting artifact size is outside the limit")
        if SAFE_EVIDENCE_FILENAME.fullmatch(source_file_name) is None:
            raise G6bEvidenceInvalid("G6b supporting artifact filename is not canonical")
        if not 1 <= len(media_type) <= 128 or any(
            character in media_type for character in "\r\n\0"
        ):
            raise G6bEvidenceInvalid("G6b supporting artifact media type is invalid")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != expected_sha256:
            raise G6bEvidenceInvalid("G6b supporting artifact SHA-256 differs")
        artifact_id = f"g6bart-{digest[:24]}"
        suffix = Path(source_file_name).suffix.casefold()
        object_key = f"commissioning/g6b/artifacts/{digest[:2]}/{digest}/source{suffix}"
        await self.object_store.put_bytes(object_key, payload, sha256=digest)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM g6b_supporting_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            if existing is not None:
                expected = (digest, len(payload), source_file_name, media_type)
                actual = (
                    existing["sha256"],
                    existing["size_bytes"],
                    existing["source_file_name"],
                    existing["media_type"],
                )
                if actual != expected:
                    raise G6bEvidenceConflict(
                        "G6b supporting artifact identity already differs"
                    )
                return self._artifact_row(existing)
            connection.execute(
                """
                INSERT INTO g6b_supporting_artifacts (
                    artifact_id, sha256, size_bytes, object_key, source_file_name,
                    media_type, uploaded_by, uploaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    digest,
                    len(payload),
                    object_key,
                    source_file_name,
                    media_type,
                    uploaded_by,
                    isoformat_z(occurred_at),
                ),
            )
            row = connection.execute(
                "SELECT * FROM g6b_supporting_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        return self._artifact_row(row)

    def get_supporting_artifact(self, artifact_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM g6b_supporting_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise G6bEvidenceNotFound(f"unknown G6b supporting artifact: {artifact_id}")
        return self._artifact_row(row)

    def open_supporting_artifact(self, artifact_id: str) -> Path:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM g6b_supporting_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise G6bEvidenceNotFound(f"unknown G6b supporting artifact: {artifact_id}")
        return self.object_store.open_path(
            row["object_key"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
        )

    def create_case(
        self,
        body: G6bCaseCreateV1,
        *,
        created_by: str | None,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        payload = _canonical_bytes(body)
        digest = hashlib.sha256(payload).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            asset = connection.execute(
                """
                SELECT asset_id, asset_type, lifecycle_state, runtime_kind, agent_id
                FROM assets WHERE asset_id = ?
                """,
                (body.asset_id,),
            ).fetchone()
            if asset is None:
                raise G6bEvidenceInvalid(f"unknown commissioning asset: {body.asset_id}")
            if asset["asset_type"] not in {"robot", "mobile_robot"}:
                raise G6bEvidenceInvalid("G6b commissioning requires a robot asset")
            if asset["lifecycle_state"] != "active" or asset["runtime_kind"] != "physical":
                raise G6bEvidenceInvalid(
                    "G6b commissioning requires an active physical robot asset"
                )
            if asset["agent_id"] is None:
                raise G6bEvidenceInvalid(
                    "G6b commissioning requires a robot bound to a registered Agent"
                )
            existing = connection.execute(
                "SELECT * FROM g6b_commissioning_cases WHERE case_id = ?",
                (body.case_id,),
            ).fetchone()
            if existing is not None:
                if existing["case_sha256"] != digest:
                    raise G6bEvidenceConflict(
                        "G6b case identity already contains different scope"
                    )
                return self._case_row(existing)
            connection.execute(
                """
                INSERT INTO g6b_commissioning_cases (
                    case_id, asset_id, agent_id, case_sha256, case_json,
                    created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    body.case_id,
                    body.asset_id,
                    asset["agent_id"],
                    digest,
                    payload.decode("utf-8"),
                    created_by,
                    isoformat_z(occurred_at),
                ),
            )
            row = connection.execute(
                "SELECT * FROM g6b_commissioning_cases WHERE case_id = ?",
                (body.case_id,),
            ).fetchone()
            return self._case_row(row)

    def list_cases(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM g6b_commissioning_cases
                ORDER BY created_at DESC, case_id
                """
            ).fetchall()
        return [self._case_row(row) for row in rows]

    def get_case(self, case_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM g6b_commissioning_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        if row is None:
            raise G6bEvidenceNotFound(f"unknown G6b case: {case_id}")
        return self._case_row(row)

    async def submit(
        self,
        body: G6bEvidencePackageV1,
        *,
        submitted_by: str | None,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        self.get_case(body.case_id)
        payload = _canonical_bytes(body)
        if len(payload) > MAX_G6B_EVIDENCE_BYTES:
            raise G6bEvidenceInvalid("G6b evidence package exceeds 2 MiB")
        digest = hashlib.sha256(payload).hexdigest()
        submission_identity = hashlib.sha256(
            f"{body.case_id}\0{body.revision}\0{digest}".encode()
        ).hexdigest()
        submission_id = f"g6bsub-{submission_identity[:24]}"
        object_key = (
            f"commissioning/g6b/{body.case_id}/{body.revision:08d}/"
            f"{digest}.json"
        )

        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM g6b_evidence_submissions
                WHERE case_id = ? AND revision = ?
                """,
                (body.case_id, body.revision),
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != digest:
                    raise G6bEvidenceConflict(
                        "G6b submission revision already contains different evidence"
                    )
                return self._submission_row(existing)
            latest = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS revision
                FROM g6b_evidence_submissions WHERE case_id = ?
                """,
                (body.case_id,),
            ).fetchone()["revision"]
            if body.revision != latest + 1:
                raise G6bEvidenceInvalid(
                    f"G6b evidence revision must be {latest + 1}"
                )

        await self.object_store.put_bytes(object_key, payload, sha256=digest)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM g6b_evidence_submissions
                WHERE case_id = ? AND revision = ?
                """,
                (body.case_id, body.revision),
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != digest:
                    raise G6bEvidenceConflict(
                        "G6b submission revision already contains different evidence"
                    )
                return self._submission_row(existing)
            latest = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS revision
                FROM g6b_evidence_submissions WHERE case_id = ?
                """,
                (body.case_id,),
            ).fetchone()["revision"]
            if body.revision != latest + 1:
                raise G6bEvidenceInvalid(
                    f"G6b evidence revision must be {latest + 1}"
                )
            connection.execute(
                """
                INSERT INTO g6b_evidence_submissions (
                    submission_id, case_id, revision, payload_sha256,
                    payload_size_bytes, object_key, submitted_by, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    body.case_id,
                    body.revision,
                    digest,
                    len(payload),
                    object_key,
                    submitted_by,
                    isoformat_z(occurred_at),
                ),
            )
            row = connection.execute(
                "SELECT * FROM g6b_evidence_submissions WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        return self._submission_row(row)

    def list_submissions(self, case_id: str) -> list[dict[str, Any]]:
        self.get_case(case_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM g6b_evidence_submissions
                WHERE case_id = ? ORDER BY revision DESC
                """,
                (case_id,),
            ).fetchall()
        return [self._submission_row(row) for row in rows]

    def get_submission(self, submission_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM g6b_evidence_submissions WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        if row is None:
            raise G6bEvidenceNotFound(f"unknown G6b submission: {submission_id}")
        return self._submission_row(row)

    def _submission_package(self, submission_id: str) -> G6bEvidencePackageV1:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM g6b_evidence_submissions WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        if row is None:
            raise G6bEvidenceNotFound(f"unknown G6b submission: {submission_id}")
        path = self.object_store.open_path(
            row["object_key"],
            size_bytes=row["payload_size_bytes"],
            sha256=row["payload_sha256"],
        )
        return G6bEvidencePackageV1.model_validate_json(path.read_bytes())

    def open_submission(self, submission_id: str) -> Path:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM g6b_evidence_submissions WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        if row is None:
            raise G6bEvidenceNotFound(f"unknown G6b submission: {submission_id}")
        return self.object_store.open_path(
            row["object_key"],
            size_bytes=row["payload_size_bytes"],
            sha256=row["payload_sha256"],
        )

    def _reviews(self, submission_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM g6b_evidence_reviews
                WHERE submission_id = ? ORDER BY reviewed_at, review_role
                """,
                (submission_id,),
            ).fetchall()
        return [self._review_row(row) for row in rows]

    def _principal_assignments_valid(
        self,
        evidence: G6bRoleAssignmentsEvidenceV1,
    ) -> bool:
        assignments = {item.role: item.principal_id for item in evidence.assignments}
        if set(assignments) != G6B_REQUIRED_ROLES:
            return False
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT principal_id, principal_kind, role, enabled
                FROM principals
                WHERE principal_id IN (?, ?, ?)
                """,
                (
                    assignments["edge_operator"],
                    assignments["manager"],
                    assignments["safety_officer"],
                ),
            ).fetchall()
        principals = {row["principal_id"]: row for row in rows}
        if set(principals) != set(assignments.values()):
            return False
        if any(
            row["principal_kind"] != "user" or not bool(row["enabled"])
            for row in principals.values()
        ):
            return False
        return (
            principals[assignments["edge_operator"]]["role"] in {"operator", "admin"}
            and principals[assignments["manager"]]["role"] == "admin"
            and principals[assignments["safety_officer"]]["role"] == "admin"
        )

    def _supporting_artifacts_valid(self, package: G6bEvidencePackageV1) -> bool:
        records: list[G6bEvidenceRecordV1] = [
            *package.stop_measurements,
            *package.fault_response_tests,
        ]
        records.extend(
            item
            for item in (
                package.risk_assessment,
                package.safety_chain,
                package.calibration,
                package.lease_timing,
                package.role_assignments,
                package.operating_envelope,
                package.xr_loss_response,
                package.resource_sharing,
            )
            if item is not None
        )
        refs: dict[str, str] = {}
        for record in records:
            for ref in record.artifact_refs:
                if not ref.uri.startswith(G6B_ARTIFACT_URI_PREFIX):
                    return False
                artifact_id = ref.uri.removeprefix(G6B_ARTIFACT_URI_PREFIX)
                if not artifact_id or (artifact_id in refs and refs[artifact_id] != ref.sha256):
                    return False
                refs[artifact_id] = ref.sha256
        if not refs:
            return False
        placeholders = ",".join("?" for _ in refs)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM g6b_supporting_artifacts WHERE artifact_id IN ({placeholders})",
                tuple(refs),
            ).fetchall()
        if {row["artifact_id"] for row in rows} != set(refs):
            return False
        try:
            for row in rows:
                if row["sha256"] != refs[row["artifact_id"]]:
                    return False
                self.object_store.open_path(
                    row["object_key"],
                    size_bytes=row["size_bytes"],
                    sha256=row["sha256"],
                )
        except ObjectStoreError:
            return False
        return True

    def report(
        self,
        case_id: str,
        *,
        generated_at: datetime,
        submission_id: str | None = None,
    ) -> G6bReadinessReportV1:
        case_view = self.get_case(case_id)
        case = G6bCaseCreateV1.model_validate(
            {key: value for key, value in case_view.items() if key in G6bCaseCreateV1.model_fields}
        )
        if submission_id is None:
            submissions = self.list_submissions(case_id)
            submission = submissions[0] if submissions else None
        else:
            submission = self.get_submission(submission_id)
            if submission["case_id"] != case_id:
                raise G6bEvidenceInvalid("G6b submission belongs to a different case")

        checks: list[ReadinessCheckV1] = []

        def add(check_id: str, passed: bool, detail: str) -> None:
            checks.append(ReadinessCheckV1(check_id=check_id, passed=passed, detail=detail))

        if submission is None:
            for check_id, detail in (
                ("risk-assessment", "Dated guarded-cell or separation-monitoring risk evidence is required."),
                ("stop-measurements", "Every declared tool, payload, speed, pose, and stop type requires measured evidence."),
                ("safety-chain", "Independent E-stop, protective-stop, reset, and restart behavior must be witnessed."),
                ("cell-calibration", "The cell frame, workspace, tolerance, and revalidation evidence are required."),
                ("fault-responses", "Cancellation, lease, network, and controller-restart behavior must be witnessed."),
                ("lease-timing", "Measured lease and safe-state timing evidence is required."),
                ("named-roles", "Enabled Edge Operator, Manager, and Safety Officer accounts are required."),
                ("operating-envelope", "The bounded capability and workspace envelope must be evidenced."),
                ("xr-loss", "XR scope and loss behavior must be explicitly evidenced."),
                ("resource-sharing", "Exclusive or coordinated resource-use policy evidence is required."),
                ("supporting-artifacts", "Every evidence attachment must exist in the immutable Object Store with its declared hash."),
            ):
                add(check_id, False, detail)
            return G6bReadinessReportV1(
                case_id=case_id,
                asset_id=case.asset_id,
                agent_id=case_view["agent_id"],
                generated_at=generated_at,
                evidence_complete=False,
                lab_evidence_status="UNVERIFIED",
                checks=checks,
                reviews=[],
                blockers=[check.detail for check in checks],
            )

        package = self._submission_package(submission["submission_id"])
        if package.case_id != case_id:
            raise G6bEvidenceInvalid("stored G6b package case identity differs")
        if package.revision != submission["revision"]:
            raise G6bEvidenceInvalid("stored G6b package revision differs")

        risk = package.risk_assessment
        risk_ok = bool(
            _fresh(risk, generated_at)
            and risk.accepted_by_lab
            and (
                risk.protection_strategy != "guarded_zone"
                or risk.person_excluded_during_motion
            )
        )
        add(
            "risk-assessment",
            risk_ok,
            "Risk evidence must be current, lab-accepted, and exclude people during guarded-zone motion.",
        )

        required_stops = {
            (scope.scope_id, stop_type)
            for scope in case.stop_scopes
            for stop_type in scope.required_stop_types
        }
        observed_stops = {
            (item.stop_scope_id, item.stop_type): item
            for item in package.stop_measurements
        }
        stop_ok = required_stops == set(observed_stops)
        if stop_ok:
            stop_ok = all(
                _fresh(item, generated_at)
                and item.test_passed
                and max(sample.stop_time_ms for sample in item.samples)
                <= item.maximum_permitted_stop_time_ms
                and max(sample.stop_distance_mm for sample in item.samples)
                <= item.maximum_permitted_stop_distance_mm
                for item in observed_stops.values()
            )
        add(
            "stop-measurements",
            stop_ok,
            "Each declared stop scope/type must have current samples within its reviewed limits.",
        )

        safety = package.safety_chain
        safety_ok = bool(
            _fresh(safety, generated_at)
            and set(safety.tested_functions) == G6B_REQUIRED_SAFETY_FUNCTIONS
            and safety.independent_of_sang
            and safety.manual_local_reset
            and safety.no_automatic_restart
            and safety.test_passed
        )
        add(
            "safety-chain",
            safety_ok,
            "Independent E-stop, protective stop, local reset, and no-auto-restart evidence must pass.",
        )

        calibration = package.calibration
        calibration_ok = bool(
            _fresh(calibration, generated_at)
            and calibration.cell_frame_ref == case.cell_frame_ref
            and calibration.test_passed
            and max(calibration.error_samples_mm)
            <= calibration.maximum_permitted_error_mm
        )
        add(
            "cell-calibration",
            calibration_ok,
            "Current cell-frame and workspace calibration samples must remain within the declared tolerance.",
        )

        faults = {item.scenario: item for item in package.fault_response_tests}
        fault_ok = set(faults) == G6B_REQUIRED_FAULT_SCENARIOS
        if fault_ok:
            fault_ok = all(
                _fresh(item, generated_at)
                and item.test_passed
                and item.observed_behavior == item.expected_behavior
                and item.measured_time_to_safe_state_ms
                <= item.maximum_permitted_time_to_safe_state_ms
                for item in faults.values()
            )
        add(
            "fault-responses",
            fault_ok,
            "Cancellation, lease expiry, network loss, and controller restart must reach their declared behavior in time.",
        )

        lease = package.lease_timing
        lease_ok = bool(
            _fresh(lease, generated_at)
            and lease.test_passed
            and lease.measured_time_to_safe_state_ms
            <= lease.maximum_permitted_time_to_safe_state_ms
        )
        add(
            "lease-timing",
            lease_ok,
            "Current measured lease expiry, renewal margin, and safe-state timing evidence must pass.",
        )

        roles = package.role_assignments
        roles_ok = bool(
            _fresh(roles, generated_at)
            and roles.release_key_custody_documented
            and self._principal_assignments_valid(roles)
        )
        add(
            "named-roles",
            roles_ok,
            "Distinct enabled Edge Operator, Manager, and Safety Officer users plus key custody are required.",
        )

        operating = package.operating_envelope
        expected_tools = {scope.tool_configuration_id for scope in case.stop_scopes}
        expected_payloads = {scope.payload_configuration_id for scope in case.stop_scopes}
        expected_speeds = {scope.speed_envelope_ref for scope in case.stop_scopes}
        expected_poses = {scope.pose_envelope_ref for scope in case.stop_scopes}
        operating_ok = bool(
            _fresh(operating, generated_at)
            and operating.test_passed
            and set(operating.capability_refs) == set(case.capability_refs)
            and set(operating.tool_configuration_ids) == expected_tools
            and set(operating.payload_configuration_ids) == expected_payloads
            and set(operating.speed_envelope_refs) == expected_speeds
            and set(operating.pose_envelope_refs) == expected_poses
        )
        add(
            "operating-envelope",
            operating_ok,
            "The output-disconnected envelope must exactly cover the case capabilities, tools, payloads, and speed scope.",
        )

        xr = package.xr_loss_response
        if case.xr_control_in_scope:
            xr_ok = bool(
                _fresh(xr, generated_at)
                and xr.xr_control_in_scope
                and set(xr.tested_loss_modes) == G6B_REQUIRED_XR_LOSS_MODES
                and xr.intervention_inhibited_on_loss
                and xr.manual_reauthorization_required
                and xr.physical_estop_independent
                and xr.xr_control_prohibited_when_unqualified
                and xr.test_passed
            )
        else:
            xr_ok = bool(
                _fresh(xr, generated_at)
                and not xr.xr_control_in_scope
                and not xr.tested_loss_modes
                and xr.xr_control_prohibited_when_unqualified
                and xr.physical_estop_independent
                and xr.test_passed
            )
        add(
            "xr-loss",
            xr_ok,
            "XR must either pass all declared loss modes or be explicitly prohibited from control.",
        )

        sharing = package.resource_sharing
        if case.shared_resources_in_scope:
            sharing_ok = bool(
                _fresh(sharing, generated_at)
                and sharing.sharing_permitted
                and sharing.shared_resource_refs
                and sharing.coordination_policy_ref is not None
                and sharing.exclusive_reservations_required
                and sharing.test_passed
            )
        else:
            sharing_ok = bool(
                _fresh(sharing, generated_at)
                and not sharing.sharing_permitted
                and not sharing.shared_resource_refs
                and sharing.coordination_policy_ref is None
                and sharing.exclusive_reservations_required
                and sharing.test_passed
            )
        add(
            "resource-sharing",
            sharing_ok,
            "Resource evidence must match the case sharing scope and retain exclusive reservations.",
        )

        add(
            "supporting-artifacts",
            self._supporting_artifacts_valid(package),
            "Every evidence attachment must exist in the immutable Object Store with its declared hash.",
        )

        evidence_complete = all(check.passed for check in checks)
        reviews = self._reviews(submission["submission_id"])
        assignments = (
            {}
            if roles is None
            else {item.role: item.principal_id for item in roles.assignments}
        )
        accepted_roles = {
            review["review_role"]
            for review in reviews
            if review["decision"] == "accept"
            and review["submission_sha256"] == submission["payload_sha256"]
            and assignments.get(review["review_role"]) == review["reviewer_id"]
        }
        rejected = [review for review in reviews if review["decision"] == "reject"]
        if not evidence_complete or rejected:
            status = "UNVERIFIED"
        elif accepted_roles == G6B_REVIEW_ROLES:
            status = "REVIEWED"
        else:
            status = "READY_FOR_REVIEW"

        blockers = [check.detail for check in checks if not check.passed]
        if rejected:
            blockers.append("The current immutable evidence revision has a rejected review.")
        elif evidence_complete:
            for role in sorted(G6B_REVIEW_ROLES - accepted_roles):
                blockers.append(f"A matching {role} review is still required.")

        return G6bReadinessReportV1(
            case_id=case_id,
            asset_id=case.asset_id,
            agent_id=case_view["agent_id"],
            submission_id=submission["submission_id"],
            submission_revision=submission["revision"],
            submission_sha256=submission["payload_sha256"],
            generated_at=generated_at,
            evidence_complete=evidence_complete,
            lab_evidence_status=status,
            checks=checks,
            reviews=reviews,
            blockers=blockers,
        )

    def review(
        self,
        submission_id: str,
        body: G6bReviewRequestV1,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        submission = self.get_submission(submission_id)
        if body.expected_submission_sha256 != submission["payload_sha256"]:
            raise G6bEvidenceConflict("review targets a stale G6b evidence digest")
        package = self._submission_package(submission_id)
        report = self.report(
            package.case_id,
            generated_at=occurred_at,
            submission_id=submission_id,
        )
        roles = package.role_assignments
        assignments = (
            {}
            if roles is None
            else {item.role: item.principal_id for item in roles.assignments}
        )
        if assignments.get(body.review_role) != body.reviewer_id:
            raise G6bEvidenceInvalid(
                "reviewer does not match the named role in this evidence revision"
            )
        if body.decision == "accept" and not report.evidence_complete:
            raise G6bEvidenceInvalid("incomplete or expired G6b evidence cannot be accepted")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            principal = connection.execute(
                """
                SELECT principal_kind, role, enabled FROM principals
                WHERE principal_id = ?
                """,
                (body.reviewer_id,),
            ).fetchone()
            if (
                principal is None
                or principal["principal_kind"] != "user"
                or principal["role"] != "admin"
                or not bool(principal["enabled"])
            ):
                raise G6bEvidenceInvalid(
                    "G6b reviewers must be enabled human admin principals"
                )
            existing = connection.execute(
                """
                SELECT * FROM g6b_evidence_reviews
                WHERE submission_id = ? AND review_role = ?
                """,
                (submission_id, body.review_role),
            ).fetchone()
            if existing is not None:
                expected = (
                    body.reviewer_id,
                    body.decision,
                    body.rationale,
                    body.expected_submission_sha256,
                )
                actual = (
                    existing["reviewer_id"],
                    existing["decision"],
                    existing["rationale"],
                    existing["submission_sha256"],
                )
                if actual != expected:
                    raise G6bEvidenceConflict(
                        "G6b review role already contains a different decision"
                    )
                return self._review_row(existing)
            review_identity = hashlib.sha256(
                f"{submission_id}\0{body.review_role}\0{body.reviewer_id}".encode()
            ).hexdigest()
            review_id = f"g6brev-{review_identity[:24]}"
            connection.execute(
                """
                INSERT INTO g6b_evidence_reviews (
                    review_id, submission_id, submission_sha256, review_role,
                    reviewer_id, decision, rationale, reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    submission_id,
                    submission["payload_sha256"],
                    body.review_role,
                    body.reviewer_id,
                    body.decision,
                    body.rationale,
                    isoformat_z(occurred_at),
                ),
            )
            row = connection.execute(
                "SELECT * FROM g6b_evidence_reviews WHERE review_id = ?",
                (review_id,),
            ).fetchone()
        return self._review_row(row)
