"""Durable Central registry, Run state, and transactional resource reservations."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from pydantic import BaseModel

from fleet_control_plane.orchestration_models import (
    CalibrationV1,
    CapabilityProfileV1,
    CoordinatedGroupV1,
    ControlEnvelopeV1,
    DeviceManifestV1,
    RuntimeBindingV1,
    UnityBuildAttestationV1,
    WorkflowDefinitionV2,
    ZoneV1,
    validate_runtime_binding,
)
from fleet_control_plane.orchestration_runtime_models import (
    DerivedProvenanceV1,
    GenericRegistryObjectV1,
    OrchestrationRunCreateV1,
    OrchestrationRunTransitionV1,
    ProducerBindingCreateV1,
    RegistryKind,
    SafetyPolicyV2,
)
from fleet_control_plane.status import isoformat_z


ORCHESTRATION_MIGRATION = """
CREATE TABLE IF NOT EXISTS orchestration_registry (
    object_kind TEXT NOT NULL,
    object_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    approval_state TEXT NOT NULL CHECK (
        approval_state IN ('draft', 'approved', 'revoked', 'not_applicable')
    ),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (object_kind, object_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS orchestration_registry_digest
    ON orchestration_registry (object_kind, payload_sha256);

CREATE TRIGGER IF NOT EXISTS orchestration_registry_no_update
BEFORE UPDATE ON orchestration_registry
BEGIN
    SELECT RAISE(ABORT, 'orchestration registry objects are immutable');
END;

CREATE TRIGGER IF NOT EXISTS orchestration_registry_no_delete
BEFORE DELETE ON orchestration_registry
BEGIN
    SELECT RAISE(ABORT, 'orchestration registry objects are immutable');
END;

CREATE TABLE IF NOT EXISTS orchestration_runs (
    run_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    workcell_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL CHECK (workflow_version >= 1),
    workflow_sha256 TEXT NOT NULL CHECK (length(workflow_sha256) = 64),
    binding_id TEXT NOT NULL,
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    binding_sha256 TEXT NOT NULL CHECK (length(binding_sha256) = 64),
    execution_mode TEXT NOT NULL CHECK (
        execution_mode IN (
            'OBSERVE_ONLY', 'SIMULATION', 'REPLAY', 'SHADOW', 'HIL',
            'PHYSICAL_AUTO', 'PHYSICAL_TELEOP', 'MAINTENANCE'
        )
    ),
    orchestrator_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'DRAFT', 'READY', 'ACTIVE', 'HELD', 'ABORTING', 'ABORTED',
            'RECONCILING', 'COMPLETE', 'FAILED'
        )
    ),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    state_reason TEXT
);

CREATE TABLE IF NOT EXISTS orchestration_requested_resources (
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id) ON DELETE RESTRICT,
    resource_kind TEXT NOT NULL CHECK (resource_kind IN ('process_resource', 'zone')),
    resource_id TEXT NOT NULL,
    access_mode TEXT NOT NULL CHECK (access_mode IN ('exclusive', 'shared_read')),
    PRIMARY KEY (run_id, resource_kind, resource_id)
);

CREATE TABLE IF NOT EXISTS orchestration_role_bindings (
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id) ON DELETE RESTRICT,
    role TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    asset_id TEXT NOT NULL,
    access_mode TEXT NOT NULL CHECK (access_mode IN ('exclusive', 'shared_read')),
    PRIMARY KEY (run_id, role, ordinal),
    UNIQUE (run_id, role, asset_id)
);

CREATE TABLE IF NOT EXISTS orchestration_reservations (
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id) ON DELETE RESTRICT,
    resource_kind TEXT NOT NULL CHECK (
        resource_kind IN (
            'asset', 'coordinated_group', 'process_resource', 'workcell', 'zone'
        )
    ),
    resource_id TEXT NOT NULL,
    access_mode TEXT NOT NULL CHECK (access_mode IN ('exclusive', 'shared_read')),
    source TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY (run_id, resource_kind, resource_id)
);

CREATE INDEX IF NOT EXISTS orchestration_active_reservations
    ON orchestration_reservations (resource_kind, resource_id, access_mode)
    WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS orchestration_producer_bindings (
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id) ON DELETE RESTRICT,
    producer_id TEXT NOT NULL,
    asset_ids_json TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('live', 'replay', 'synthetic')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, producer_id)
);

CREATE TABLE IF NOT EXISTS orchestration_run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id) ON DELETE RESTRICT,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    previous_state TEXT,
    state TEXT NOT NULL,
    reason TEXT,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS orchestration_run_events_run
    ON orchestration_run_events (run_id, id);
"""


class OrchestrationConflict(ValueError):
    """An immutable identity or reservation conflicts with persisted state."""


class OrchestrationNotFound(ValueError):
    """A referenced orchestration object does not exist."""


class OrchestrationInvalid(ValueError):
    """A contract or state transition violates orchestration rules."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def payload_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


_MODEL_IDENTITIES: dict[type[BaseModel], tuple[RegistryKind, str, str]] = {
    CalibrationV1: ("calibration", "calibration_id", "version"),
    CapabilityProfileV1: ("capability_profile", "profile_id", "version"),
    CoordinatedGroupV1: ("coordinated_group", "group_asset_id", "version"),
    ControlEnvelopeV1: ("control_envelope", "envelope_id", "version"),
    DeviceManifestV1: ("device_manifest", "manifest_id", "version"),
    RuntimeBindingV1: ("runtime_binding", "binding_id", "schema_version"),
    SafetyPolicyV2: ("safety_policy", "policy_id", "version"),
    UnityBuildAttestationV1: (
        "unity_build_attestation",
        "build_id",
        "schema_version",
    ),
    WorkflowDefinitionV2: ("workflow", "workflow_id", "version"),
    ZoneV1: ("zone", "zone_id", "version"),
}


_TERMINAL_STATES = frozenset({"ABORTED", "COMPLETE", "FAILED"})
_TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"READY", "ABORTING", "FAILED"}),
    "READY": frozenset({"ACTIVE", "ABORTING", "FAILED"}),
    "ACTIVE": frozenset(
        {"HELD", "ABORTING", "RECONCILING", "COMPLETE", "FAILED"}
    ),
    "HELD": frozenset({"ACTIVE", "ABORTING", "RECONCILING", "FAILED"}),
    "RECONCILING": frozenset({"HELD", "ABORTING", "FAILED"}),
    "ABORTING": frozenset({"ABORTED", "FAILED"}),
    "ABORTED": frozenset(),
    "COMPLETE": frozenset(),
    "FAILED": frozenset(),
}


class OrchestrationStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(ORCHESTRATION_MIGRATION)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _registry_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "object_kind": row["object_kind"],
            "object_id": row["object_id"],
            "version": row["version"],
            "schema_version": row["schema_version"],
            "approval_state": row["approval_state"],
            "payload_sha256": row["payload_sha256"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }

    def register_model(
        self,
        model: BaseModel,
        *,
        occurred_at: datetime,
        approval_state: str = "not_applicable",
    ) -> dict[str, Any]:
        identity = _MODEL_IDENTITIES.get(type(model))
        if identity is None:
            raise OrchestrationInvalid(
                f"unsupported typed registry model: {type(model).__name__}"
            )
        kind, id_field, version_field = identity
        embedded_approval = getattr(model, "approval", None)
        if approval_state == "not_applicable" and embedded_approval is not None:
            approval_state = embedded_approval.state
        return self._register(
            kind,
            str(getattr(model, id_field)),
            int(getattr(model, version_field)),
            model.model_dump(mode="json"),
            schema_version=int(getattr(model, "schema_version", 1)),
            approval_state=approval_state,
            occurred_at=occurred_at,
        )

    def register_generic(
        self,
        kind: RegistryKind,
        model: GenericRegistryObjectV1,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        if kind in {
            "calibration",
            "capability_profile",
            "control_envelope",
            "coordinated_group",
            "device_manifest",
            "runtime_binding",
            "safety_policy",
            "unity_build_attestation",
            "workflow",
            "zone",
        }:
            raise OrchestrationInvalid(f"{kind} requires its typed contract")
        return self._register(
            kind,
            model.object_id,
            model.version,
            model.model_dump(mode="json"),
            schema_version=model.schema_version,
            approval_state=model.approval_state,
            occurred_at=occurred_at,
        )

    def _register(
        self,
        kind: RegistryKind,
        object_id: str,
        version: int,
        payload: dict[str, Any],
        *,
        schema_version: int,
        approval_state: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        encoded = canonical_json(payload)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        timestamp = isoformat_z(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM orchestration_registry
                WHERE object_kind = ? AND object_id = ? AND version = ?
                """,
                (kind, object_id, version),
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != digest:
                    raise OrchestrationConflict(
                        f"{kind} {object_id}@{version} is immutable and has different content"
                    )
                return {**self._registry_row(existing), "idempotent_replay": True}
            try:
                connection.execute(
                    """
                    INSERT INTO orchestration_registry (
                        object_kind, object_id, version, schema_version,
                        approval_state, payload_sha256, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        kind,
                        object_id,
                        version,
                        schema_version,
                        approval_state,
                        digest,
                        encoded,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise OrchestrationConflict(str(exc)) from exc
            row = connection.execute(
                """
                SELECT * FROM orchestration_registry
                WHERE object_kind = ? AND object_id = ? AND version = ?
                """,
                (kind, object_id, version),
            ).fetchone()
            return {**self._registry_row(row), "idempotent_replay": False}

    def get_registry(
        self,
        kind: RegistryKind,
        object_id: str,
        version: int,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM orchestration_registry
                WHERE object_kind = ? AND object_id = ? AND version = ?
                """,
                (kind, object_id, version),
            ).fetchone()
        return None if row is None else self._registry_row(row)

    def list_registry(self, kind: RegistryKind | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if kind is None:
                rows = connection.execute(
                    """
                    SELECT * FROM orchestration_registry
                    ORDER BY object_kind, object_id, version DESC
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM orchestration_registry WHERE object_kind = ?
                    ORDER BY object_id, version DESC
                    """,
                    (kind,),
                ).fetchall()
        return [self._registry_row(row) for row in rows]

    def _required_registry(
        self,
        connection: sqlite3.Connection,
        kind: RegistryKind,
        object_id: str,
        version: int,
        expected_sha256: str | None = None,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM orchestration_registry
            WHERE object_kind = ? AND object_id = ? AND version = ?
            """,
            (kind, object_id, version),
        ).fetchone()
        if row is None:
            raise OrchestrationNotFound(f"missing {kind}: {object_id}@{version}")
        if expected_sha256 is not None and row["payload_sha256"] != expected_sha256:
            raise OrchestrationInvalid(
                f"{kind} {object_id}@{version} digest does not match its pinned reference"
            )
        return row

    def _validate_run_contract(
        self,
        connection: sqlite3.Connection,
        body: OrchestrationRunCreateV1,
    ) -> tuple[WorkflowDefinitionV2, RuntimeBindingV1, sqlite3.Row, sqlite3.Row]:
        workflow_row = self._required_registry(
            connection, "workflow", body.workflow_id, body.workflow_version
        )
        binding_row = self._required_registry(
            connection, "runtime_binding", body.binding_id, 1
        )
        workflow = WorkflowDefinitionV2.model_validate_json(workflow_row["payload_json"])
        binding = RuntimeBindingV1.model_validate_json(binding_row["payload_json"])
        if binding.run_id != body.run_id:
            raise OrchestrationInvalid("runtime binding pins a different run_id")
        if binding.execution_mode != body.execution_mode:
            raise OrchestrationInvalid("Run execution_mode differs from its immutable binding")

        definitions = []
        profile_rows = connection.execute(
            """
            SELECT payload_json FROM orchestration_registry
            WHERE object_kind = 'capability_profile'
            """
        ).fetchall()
        for row in profile_rows:
            definitions.extend(
                CapabilityProfileV1.model_validate_json(row["payload_json"]).capabilities
            )
        if not definitions:
            raise OrchestrationInvalid("no capability profiles are registered")

        group_rows = connection.execute(
            """
            SELECT payload_json FROM orchestration_registry
            WHERE object_kind = 'coordinated_group'
            """
        ).fetchall()
        groups = [
            CoordinatedGroupV1.model_validate_json(row["payload_json"])
            for row in group_rows
        ]
        try:
            validate_runtime_binding(workflow, binding, definitions, groups)
        except ValueError as exc:
            raise OrchestrationInvalid(str(exc)) from exc

        for asset in binding.assets:
            self._required_registry(
                connection,
                "device_manifest",
                asset.manifest_ref.manifest_id,
                asset.manifest_ref.version,
                asset.manifest_ref.sha256,
            )
            self._required_registry(
                connection,
                "connection_snapshot",
                asset.connection_ref.object_id,
                asset.connection_ref.version,
                asset.connection_ref.sha256,
            )
            for reference in asset.calibration_refs:
                self._required_registry(
                    connection,
                    "calibration",
                    reference.object_id,
                    reference.version,
                    reference.sha256,
                )
            if asset.output_isolation_evidence_ref is not None:
                reference = asset.output_isolation_evidence_ref
                self._required_registry(
                    connection,
                    "output_isolation_evidence",
                    reference.object_id,
                    reference.version,
                    reference.sha256,
                )
        return workflow, binding, workflow_row, binding_row

    def create_run(
        self,
        body: OrchestrationRunCreateV1,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        timestamp = isoformat_z(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            workflow, binding, workflow_row, binding_row = self._validate_run_contract(
                connection, body
            )
            existing = connection.execute(
                "SELECT * FROM orchestration_runs WHERE run_id = ?", (body.run_id,)
            ).fetchone()
            identity = {
                "display_name": body.display_name,
                "workcell_id": body.workcell_id,
                "workflow_id": body.workflow_id,
                "workflow_version": body.workflow_version,
                "workflow_sha256": workflow_row["payload_sha256"],
                "binding_id": body.binding_id,
                "binding_version": 1,
                "binding_sha256": binding_row["payload_sha256"],
                "execution_mode": body.execution_mode,
                "orchestrator_id": body.orchestrator_id,
            }
            if existing is not None:
                if any(existing[key] != value for key, value in identity.items()):
                    raise OrchestrationConflict(
                        f"Run {body.run_id} already exists with a different immutable contract"
                    )
                return {**self._decode_run(connection, existing), "idempotent_replay": True}

            connection.execute(
                """
                INSERT INTO orchestration_runs (
                    run_id, display_name, workcell_id, workflow_id, workflow_version,
                    workflow_sha256, binding_id, binding_version, binding_sha256,
                    execution_mode, orchestrator_id, state, revision, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DRAFT', 1, ?, ?)
                """,
                (
                    body.run_id,
                    body.display_name,
                    body.workcell_id,
                    body.workflow_id,
                    body.workflow_version,
                    workflow_row["payload_sha256"],
                    body.binding_id,
                    1,
                    binding_row["payload_sha256"],
                    body.execution_mode,
                    body.orchestrator_id,
                    timestamp,
                    timestamp,
                ),
            )
            role_index = {role.role: role for role in workflow.roles}
            for role_binding in binding.role_bindings:
                access = role_index[role_binding.role].access
                for ordinal, asset_id in enumerate(role_binding.asset_ids):
                    connection.execute(
                        """
                        INSERT INTO orchestration_role_bindings (
                            run_id, role, ordinal, asset_id, access_mode
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (body.run_id, role_binding.role, ordinal, asset_id, access),
                    )
            for resource in body.resources:
                connection.execute(
                    """
                    INSERT INTO orchestration_requested_resources (
                        run_id, resource_kind, resource_id, access_mode
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        body.run_id,
                        resource.resource_kind,
                        resource.resource_id,
                        resource.access_mode,
                    ),
                )
            self._insert_event(
                connection,
                body.run_id,
                timestamp,
                "run_created",
                None,
                "DRAFT",
                None,
                {"execution_mode": body.execution_mode},
            )
            row = connection.execute(
                "SELECT * FROM orchestration_runs WHERE run_id = ?", (body.run_id,)
            ).fetchone()
            return {**self._decode_run(connection, row), "idempotent_replay": False}

    @staticmethod
    def _merge_reservation(
        reservations: dict[tuple[str, str], tuple[str, str]],
        kind: str,
        resource_id: str,
        access_mode: str,
        source: str,
    ) -> None:
        key = (kind, resource_id)
        previous = reservations.get(key)
        if previous is None:
            reservations[key] = (access_mode, source)
            return
        previous_access, previous_source = previous
        merged_access = (
            "exclusive"
            if "exclusive" in {previous_access, access_mode}
            else "shared_read"
        )
        reservations[key] = (merged_access, f"{previous_source};{source}")

    def _reservation_set(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
    ) -> dict[tuple[str, str], tuple[str, str]]:
        workflow_row = self._required_registry(
            connection, "workflow", run["workflow_id"], run["workflow_version"]
        )
        binding_row = self._required_registry(
            connection, "runtime_binding", run["binding_id"], run["binding_version"]
        )
        workflow = WorkflowDefinitionV2.model_validate_json(workflow_row["payload_json"])
        binding = RuntimeBindingV1.model_validate_json(binding_row["payload_json"])
        role_index = {role.role: role for role in workflow.roles}
        asset_index = {asset.asset_id: asset for asset in binding.assets}
        reservations: dict[tuple[str, str], tuple[str, str]] = {}
        self._merge_reservation(
            reservations, "workcell", run["workcell_id"], "exclusive", "run.workcell"
        )
        for role_binding in binding.role_bindings:
            role = role_index[role_binding.role]
            for asset_id in role_binding.asset_ids:
                self._merge_reservation(
                    reservations,
                    "asset",
                    asset_id,
                    role.access,
                    f"role:{role.role}",
                )
                group_ref = asset_index[asset_id].coordinated_group_ref
                if group_ref is None:
                    continue
                group_row = self._required_registry(
                    connection,
                    "coordinated_group",
                    group_ref.object_id,
                    group_ref.version,
                    group_ref.sha256,
                )
                group = CoordinatedGroupV1.model_validate_json(group_row["payload_json"])
                self._merge_reservation(
                    reservations,
                    "coordinated_group",
                    group.group_asset_id,
                    "exclusive",
                    f"group:{group.group_asset_id}",
                )
                for member_id in group.member_asset_ids:
                    self._merge_reservation(
                        reservations,
                        "asset",
                        member_id,
                        "exclusive",
                        f"group-member:{group.group_asset_id}",
                    )
            if role.selector.zone_id is not None:
                self._merge_reservation(
                    reservations,
                    "zone",
                    role.selector.zone_id,
                    "exclusive",
                    f"role-zone:{role.role}",
                )
        requested = connection.execute(
            "SELECT * FROM orchestration_requested_resources WHERE run_id = ?",
            (run["run_id"],),
        ).fetchall()
        for resource in requested:
            self._merge_reservation(
                reservations,
                resource["resource_kind"],
                resource["resource_id"],
                resource["access_mode"],
                "run.request",
            )
        return reservations

    def _acquire_reservations(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        timestamp: str,
    ) -> None:
        for (kind, resource_id), (access, source) in self._reservation_set(
            connection, run
        ).items():
            conflicts = connection.execute(
                """
                SELECT reservations.run_id, reservations.access_mode
                FROM orchestration_reservations AS reservations
                JOIN orchestration_runs AS runs ON runs.run_id = reservations.run_id
                WHERE reservations.resource_kind = ?
                  AND reservations.resource_id = ?
                  AND reservations.released_at IS NULL
                  AND reservations.run_id != ?
                  AND runs.state NOT IN ('ABORTED', 'COMPLETE', 'FAILED')
                """,
                (kind, resource_id, run["run_id"]),
            ).fetchall()
            incompatible = [
                conflict
                for conflict in conflicts
                if access == "exclusive" or conflict["access_mode"] == "exclusive"
            ]
            if incompatible:
                holders = sorted({conflict["run_id"] for conflict in incompatible})
                raise OrchestrationConflict(
                    f"{kind} {resource_id} conflicts with active Run(s): {holders}"
                )
            connection.execute(
                """
                INSERT INTO orchestration_reservations (
                    run_id, resource_kind, resource_id, access_mode, source,
                    acquired_at, released_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(run_id, resource_kind, resource_id) DO NOTHING
                """,
                (run["run_id"], kind, resource_id, access, source, timestamp),
            )

    def transition_run_in_transaction(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        body: OrchestrationRunTransitionV1,
        *,
        occurred_at: datetime,
        resume_authorized: bool = False,
    ) -> dict[str, Any]:
        timestamp = isoformat_z(occurred_at)
        run = connection.execute(
            "SELECT * FROM orchestration_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise OrchestrationNotFound(f"unknown orchestration Run: {run_id}")
        if run["revision"] != body.expected_revision:
            raise OrchestrationConflict(
                f"Run revision is {run['revision']}, not {body.expected_revision}"
            )
        current = run["state"]
        if body.state == current:
            return self._decode_run(connection, run)
        if body.state not in _TRANSITIONS[current]:
            raise OrchestrationInvalid(f"invalid Run transition {current} -> {body.state}")
        if current == "HELD" and body.state == "ACTIVE" and not resume_authorized:
            raise OrchestrationInvalid(
                "HELD Runs require completed dual approval before resuming"
            )
        if body.state == "READY":
            self._acquire_reservations(connection, run, timestamp)

        started_at = run["started_at"]
        finished_at = run["finished_at"]
        if body.state == "ACTIVE" and started_at is None:
            started_at = timestamp
        if body.state in _TERMINAL_STATES:
            finished_at = timestamp
            connection.execute(
                """
                UPDATE orchestration_reservations
                SET released_at = ? WHERE run_id = ? AND released_at IS NULL
                """,
                (timestamp, run_id),
            )
        connection.execute(
            """
            UPDATE orchestration_runs
            SET state = ?, revision = revision + 1, updated_at = ?,
                started_at = ?, finished_at = ?, state_reason = ?
            WHERE run_id = ?
            """,
            (
                body.state,
                timestamp,
                started_at,
                finished_at,
                body.reason,
                run_id,
            ),
        )
        self._insert_event(
            connection,
            run_id,
            timestamp,
            "state_transition",
            current,
            body.state,
            body.reason,
            {},
        )
        updated = connection.execute(
            "SELECT * FROM orchestration_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._decode_run(connection, updated)

    def transition_run(
        self,
        run_id: str,
        body: OrchestrationRunTransitionV1,
        *,
        occurred_at: datetime,
        resume_authorized: bool = False,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self.transition_run_in_transaction(
                connection,
                run_id,
                body,
                occurred_at=occurred_at,
                resume_authorized=resume_authorized,
            )

    def hold_run_in_transaction(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        reason: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM orchestration_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise OrchestrationNotFound(f"unknown orchestration Run: {run_id}")
        if row["state"] == "HELD":
            return self._decode_run(connection, row)
        if row["state"] != "ACTIVE":
            raise OrchestrationInvalid(
                f"safety hold requires ACTIVE Run, not {row['state']}"
            )
        return self.transition_run_in_transaction(
            connection,
            run_id,
            OrchestrationRunTransitionV1(
                expected_revision=row["revision"], state="HELD", reason=reason
            ),
            occurred_at=occurred_at,
        )

    def hold_run(self, run_id: str, *, reason: str, occurred_at: datetime) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self.hold_run_in_transaction(
                connection,
                run_id,
                reason=reason,
                occurred_at=occurred_at,
            )

    def bind_producer(
        self,
        run_id: str,
        body: ProducerBindingCreateV1,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        timestamp = isoformat_z(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM orchestration_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise OrchestrationNotFound(f"unknown orchestration Run: {run_id}")
            if run["state"] not in {"DRAFT", "READY"}:
                raise OrchestrationInvalid("producer bindings are immutable after activation")
            assets = {
                row["asset_id"]
                for row in connection.execute(
                    "SELECT asset_id FROM orchestration_role_bindings WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
            }
            missing = sorted(set(body.asset_ids) - assets)
            if missing:
                raise OrchestrationInvalid(
                    f"producer binding references assets outside the Run: {missing}"
                )
            source_kind = self._source_kind(run["execution_mode"])
            encoded_assets = canonical_json(sorted(body.asset_ids))
            existing = connection.execute(
                """
                SELECT * FROM orchestration_producer_bindings
                WHERE run_id = ? AND producer_id = ?
                """,
                (run_id, body.producer_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["asset_ids_json"] != encoded_assets
                    or existing["source_kind"] != source_kind
                ):
                    raise OrchestrationConflict("producer binding is immutable")
            else:
                connection.execute(
                    """
                    INSERT INTO orchestration_producer_bindings (
                        run_id, producer_id, asset_ids_json, source_kind, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (run_id, body.producer_id, encoded_assets, source_kind, timestamp),
                )
            return {
                "run_id": run_id,
                "producer_id": body.producer_id,
                "asset_ids": sorted(body.asset_ids),
                "source_kind": source_kind,
                "created_at": timestamp if existing is None else existing["created_at"],
            }

    @staticmethod
    def _source_kind(execution_mode: str) -> str:
        if execution_mode == "REPLAY":
            return "replay"
        if execution_mode == "SIMULATION":
            return "synthetic"
        return "live"

    def derive_provenance(
        self,
        run_id: str,
        producer_id: str,
        asset_ids: list[str],
    ) -> DerivedProvenanceV1:
        with self._connect() as connection:
            run = connection.execute(
                "SELECT * FROM orchestration_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            producer = connection.execute(
                """
                SELECT * FROM orchestration_producer_bindings
                WHERE run_id = ? AND producer_id = ?
                """,
                (run_id, producer_id),
            ).fetchone()
        if run is None:
            raise OrchestrationNotFound(f"unknown orchestration Run: {run_id}")
        if producer is None:
            raise OrchestrationInvalid("producer is not bound to the Run")
        allowed_assets = set(json.loads(producer["asset_ids_json"]))
        missing = sorted(set(asset_ids) - allowed_assets)
        if missing:
            raise OrchestrationInvalid(
                f"producer is not bound to safety assets: {missing}"
            )
        return DerivedProvenanceV1(
            source_kind=producer["source_kind"],
            execution_mode=run["execution_mode"],
            producer_id=producer_id,
            run_id=run_id,
            binding_ref={
                "object_id": run["binding_id"],
                "version": run["binding_version"],
                "sha256": run["binding_sha256"],
            },
        )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        run_id: str,
        occurred_at: str,
        event_type: str,
        previous_state: str | None,
        state: str,
        reason: str | None,
        details: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO orchestration_run_events (
                run_id, occurred_at, event_type, previous_state, state,
                reason, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                occurred_at,
                event_type,
                previous_state,
                state,
                reason,
                canonical_json(details),
            ),
        )

    @staticmethod
    def _decode_run(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        role_rows = connection.execute(
            """
            SELECT role, ordinal, asset_id, access_mode
            FROM orchestration_role_bindings WHERE run_id = ?
            ORDER BY role, ordinal
            """,
            (row["run_id"],),
        ).fetchall()
        reservation_rows = connection.execute(
            """
            SELECT resource_kind, resource_id, access_mode, source,
                   acquired_at, released_at
            FROM orchestration_reservations WHERE run_id = ?
            ORDER BY resource_kind, resource_id
            """,
            (row["run_id"],),
        ).fetchall()
        return {
            "run_id": row["run_id"],
            "display_name": row["display_name"],
            "workcell_id": row["workcell_id"],
            "workflow_ref": {
                "object_id": row["workflow_id"],
                "version": row["workflow_version"],
                "sha256": row["workflow_sha256"],
            },
            "binding_ref": {
                "object_id": row["binding_id"],
                "version": row["binding_version"],
                "sha256": row["binding_sha256"],
            },
            "execution_mode": row["execution_mode"],
            "orchestrator_id": row["orchestrator_id"],
            "state": row["state"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "state_reason": row["state_reason"],
            "role_bindings": [dict(item) for item in role_rows],
            "reservations": [dict(item) for item in reservation_rows],
        }

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM orchestration_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return None if row is None else self._decode_run(connection, row)

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM orchestration_runs ORDER BY created_at DESC, run_id"
            ).fetchall()
            return [self._decode_run(connection, row) for row in rows]

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM orchestration_run_events
                WHERE run_id = ? ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]
