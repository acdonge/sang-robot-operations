"""Draft, validate, and publish immutable robot Action source packages."""

from __future__ import annotations

import ast
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Literal
from uuid import uuid4

from pydantic import ConfigDict, Field, JsonValue, ValidationError, model_validator

from fleet_control_plane.models import FleetModel, SafeId, ShortText
from fleet_control_plane.operations_models import AccessMode, AssetType
from fleet_control_plane.orchestration_models import CapabilityRef
from fleet_control_plane.orchestration_store import payload_digest
from fleet_control_plane.skill_catalog import (
    RobotActionDefinitionV1,
    SkillCatalogV1,
    SkillDefinitionV1,
)
from fleet_control_plane.status import isoformat_z, parse_datetime


MAX_ACTION_SOURCE_BYTES = 256 * 1024


class ActionLibraryInvalid(ValueError):
    pass


class ActionLibraryConflict(ValueError):
    pass


class ActionLibraryNotFound(ValueError):
    pass


class ActionDraftSpecV1(FleetModel):
    # Source bytes are identity-bearing and must not inherit FleetModel's
    # global string trimming. Constrained metadata fields still trim themselves.
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        str_strip_whitespace=False,
    )

    action_id: SafeId
    robot_type: SafeId
    robot_label: ShortText
    title: ShortText
    description: str = Field(min_length=1, max_length=2048)
    capability: CapabilityRef
    asset_type: AssetType
    access_mode: AccessMode = "exclusive"
    fixed_parameters: dict[SafeId, JsonValue] = Field(
        default_factory=dict,
        max_length=128,
    )
    editable_parameters: list[SafeId] = Field(default_factory=list, max_length=128)
    default_timeout_s: int = Field(default=120, ge=1, le=86400)
    runtime: Literal["UNITY_CSHARP", "ROS2_PYTHON"]
    source_file_name: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(?:cs|py)$",
    )
    entrypoint: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$",
    )
    source_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_ACTION_SOURCE_BYTES,
    )

    @model_validator(mode="after")
    def source_matches_runtime(self) -> "ActionDraftSpecV1":
        suffix = Path(self.source_file_name).suffix.lower()
        expected = ".cs" if self.runtime == "UNITY_CSHARP" else ".py"
        if suffix != expected:
            raise ValueError(f"{self.runtime} Action source must use {expected}")
        if self.source_code is not None and "\x00" in self.source_code:
            raise ValueError("Action source cannot contain NUL bytes")
        if (
            self.source_code is not None
            and len(self.source_code.encode("utf-8")) > MAX_ACTION_SOURCE_BYTES
        ):
            raise ValueError("Action source exceeds 256 KiB")
        if len(self.editable_parameters) != len(set(self.editable_parameters)):
            raise ValueError("editable Action input names must be unique")
        overlap = sorted(set(self.fixed_parameters) & set(self.editable_parameters))
        if overlap:
            raise ValueError(
                "Action inputs cannot be both fixed and editable: "
                + ", ".join(overlap)
            )
        return self


class ActionDraftCreateV1(ActionDraftSpecV1):
    request_id: SafeId
    source_code: str = Field(min_length=1, max_length=MAX_ACTION_SOURCE_BYTES)


class ActionDraftReplaceV1(ActionDraftSpecV1):
    request_id: SafeId
    expected_revision: int = Field(ge=1)


class ActionSimulationValidateV1(FleetModel):
    adapter_id: SafeId


class ActionPublishV1(FleetModel):
    request_id: SafeId
    expected_revision: int = Field(ge=1)


ACTION_LIBRARY_MIGRATION = """
CREATE TABLE IF NOT EXISTS action_library_drafts (
    draft_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    target_version INTEGER NOT NULL CHECK (target_version >= 1),
    robot_type TEXT NOT NULL,
    robot_label TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    capability TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    access_mode TEXT NOT NULL CHECK (access_mode IN ('exclusive', 'shared_read')),
    fixed_parameters_json TEXT NOT NULL,
    editable_parameters_json TEXT NOT NULL,
    default_timeout_s INTEGER NOT NULL CHECK (default_timeout_s BETWEEN 1 AND 86400),
    runtime TEXT NOT NULL CHECK (runtime IN ('UNITY_CSHARP', 'ROS2_PYTHON')),
    source_file_name TEXT NOT NULL,
    entrypoint TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
    source_artifact TEXT NOT NULL,
    source_size_bytes INTEGER NOT NULL CHECK (source_size_bytes BETWEEN 1 AND 262144),
    source_validation_state TEXT NOT NULL CHECK (
        source_validation_state IN ('PENDING', 'PASS', 'FAIL')
    ),
    source_validation_json TEXT,
    simulation_validation_state TEXT NOT NULL CHECK (
        simulation_validation_state IN ('PENDING', 'PASS', 'FAIL')
    ),
    simulation_validation_json TEXT,
    state TEXT NOT NULL CHECK (state IN ('DRAFT', 'PUBLISHED')),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    validated_at TEXT,
    published_at TEXT,
    UNIQUE (action_id, target_version)
);

CREATE TABLE IF NOT EXISTS action_library_publications (
    action_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    action_ref TEXT NOT NULL UNIQUE,
    definition_json TEXT NOT NULL,
    definition_sha256 TEXT NOT NULL CHECK (length(definition_sha256) = 64),
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
    source_artifact TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    source_draft_id TEXT NOT NULL REFERENCES action_library_drafts(draft_id) ON DELETE RESTRICT,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (action_id, version)
);

CREATE TRIGGER IF NOT EXISTS action_library_publications_no_update
BEFORE UPDATE ON action_library_publications
BEGIN
    SELECT RAISE(ABORT, 'Action publications are immutable');
END;

CREATE TRIGGER IF NOT EXISTS action_library_publications_no_delete
BEFORE DELETE ON action_library_publications
BEGIN
    SELECT RAISE(ABORT, 'Action publications are immutable');
END;

CREATE TABLE IF NOT EXISTS action_library_requests (
    principal_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, request_id)
);
"""


_CSHARP_FORBIDDEN = (
    "System.Diagnostics",
    "System.IO",
    "System.Net",
    "System.Reflection",
    "System.Runtime.InteropServices",
    "Application.OpenURL",
    "AssetDatabase",
    "Directory.",
    "DllImport",
    "Environment.",
    "File.",
    "HttpClient",
    "Process.",
    "Resources.Load",
    "Socket",
    "UnityWebRequest",
    "WebClient",
    "unsafe",
)

_PYTHON_FORBIDDEN_MODULES = frozenset(
    {
        "asyncio",
        "builtins",
        "ctypes",
        "http",
        "importlib",
        "multiprocessing",
        "os",
        "pathlib",
        "requests",
        "shutil",
        "socket",
        "subprocess",
        "sys",
        "urllib",
    }
)
_PYTHON_FORBIDDEN_CALLS = frozenset(
    {"__import__", "compile", "eval", "exec", "input", "open"}
)


def _role_template(robot_type: str) -> str:
    suffix = "-action"
    return f"{robot_type[:64 - len(suffix)]}{suffix}"


def _action_base_and_version(action_ref: str) -> tuple[str, int]:
    base, version = action_ref.rsplit("@", 1)
    return base, int(version)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


class ActionLibraryStore:
    def __init__(
        self,
        path: str | Path,
        *,
        skill_definitions: dict[str, SkillDefinitionV1],
        builtin_actions: dict[str, RobotActionDefinitionV1],
        adapter_profiles: dict[str, tuple[Any, str]],
        artifact_root: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.skill_definitions = skill_definitions
        self.builtin_actions = builtin_actions
        self.adapter_profiles = adapter_profiles
        self.artifact_root = Path(
            artifact_root or self.path.parent / "action-library"
        )
        self.source_root = self.artifact_root / "sources"
        self.source_root.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(ACTION_LIBRARY_MIGRATION)

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
            SELECT * FROM action_library_requests
            WHERE principal_id = ? AND request_id = ?
            """,
            (principal_id, request_id),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_sha256"] != digest:
            raise ActionLibraryConflict(
                "request_id was already used with different Action Library input"
            )
        response = json.loads(row["response_json"])
        response["idempotent_replay"] = True
        return response

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
            INSERT INTO action_library_requests (
                principal_id, request_id, operation, request_sha256,
                response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                principal_id,
                request_id,
                operation,
                self._request_digest(operation, payload),
                _json(response),
                isoformat_z(parse_datetime(occurred_at)),
            ),
        )

    def _write_source(self, source: str, runtime: str) -> tuple[str, str, int]:
        content = source.encode("utf-8")
        if not content or len(content) > MAX_ACTION_SOURCE_BYTES:
            raise ActionLibraryInvalid("Action source must be between 1 byte and 256 KiB")
        digest = hashlib.sha256(content).hexdigest()
        suffix = ".cs" if runtime == "UNITY_CSHARP" else ".py"
        relative = Path("sources") / f"{digest}{suffix}"
        destination = self.artifact_root / relative
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise ActionLibraryConflict("stored Action source digest mismatch")
            return digest, relative.as_posix(), len(content)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return digest, relative.as_posix(), len(content)

    def _source_path(self, relative: str) -> Path:
        candidate = (self.artifact_root / relative).resolve()
        root = self.artifact_root.resolve()
        if candidate != root and root not in candidate.parents:
            raise ActionLibraryInvalid("Action source artifact escaped its storage root")
        return candidate

    def _next_version(self, connection: sqlite3.Connection, action_id: str) -> int:
        versions = [
            version
            for action_ref in self.builtin_actions
            for base, version in [_action_base_and_version(action_ref)]
            if base == action_id
        ]
        row = connection.execute(
            """
            SELECT MAX(version) AS version FROM action_library_publications
            WHERE action_id = ?
            """,
            (action_id,),
        ).fetchone()
        if row is not None and row["version"] is not None:
            versions.append(int(row["version"]))
        return max(versions, default=0) + 1

    def _step_id_for_action(self, action_id: str) -> str:
        candidates = {
            **self.builtin_actions,
            **self.published_definitions(),
        }
        matching = [
            (version, definition.step_id_template)
            for action_ref, definition in candidates.items()
            for base, version in [_action_base_and_version(action_ref)]
            if base == action_id
        ]
        return max(matching)[1] if matching else action_id

    def _definition_from_values(
        self,
        *,
        action_id: str,
        version: int,
        robot_type: str,
        robot_label: str,
        title: str,
        description: str,
        capability: str,
        asset_type: str,
        access_mode: str,
        fixed_parameters: dict[str, JsonValue],
        editable_parameters: list[str],
        default_timeout_s: int,
        runtime: str,
        source_file_name: str,
        entrypoint: str,
        source_sha256: str,
    ) -> RobotActionDefinitionV1:
        scheme = "unity" if runtime == "UNITY_CSHARP" else "ros2-python"
        definition = RobotActionDefinitionV1.model_validate(
            {
                "action_ref": f"{action_id}@{version}",
                "robot_type": robot_type,
                "robot_label": robot_label,
                "title": title,
                "description": description,
                "execution_mode": "SIMULATION",
                "capability": capability,
                "role_template": _role_template(robot_type),
                "asset_type": asset_type,
                "access_mode": access_mode,
                "step_id_template": self._step_id_for_action(action_id),
                "fixed_parameters": fixed_parameters,
                "editable_parameters": editable_parameters,
                "default_timeout_s": default_timeout_s,
                "script": {
                    "runtime": runtime,
                    "script_ref": (
                        f"{scheme}://action-library/{action_id}/{version}/"
                        f"{source_file_name}"
                    ),
                    "entrypoint": entrypoint,
                    "sha256": source_sha256,
                },
            }
        )
        SkillCatalogV1(
            catalog_id="action-library-validation",
            version=1,
            skills=list(self.skill_definitions.values()),
            actions=[definition],
        )
        return definition

    def _definition_from_row(self, row: sqlite3.Row) -> RobotActionDefinitionV1:
        return self._definition_from_values(
            action_id=row["action_id"],
            version=int(row["target_version"]),
            robot_type=row["robot_type"],
            robot_label=row["robot_label"],
            title=row["title"],
            description=row["description"],
            capability=row["capability"],
            asset_type=row["asset_type"],
            access_mode=row["access_mode"],
            fixed_parameters=json.loads(row["fixed_parameters_json"]),
            editable_parameters=json.loads(row["editable_parameters_json"]),
            default_timeout_s=int(row["default_timeout_s"]),
            runtime=row["runtime"],
            source_file_name=row["source_file_name"],
            entrypoint=row["entrypoint"],
            source_sha256=row["source_sha256"],
        )

    @staticmethod
    def _validation(value: str | None) -> dict[str, Any] | None:
        return None if value is None else json.loads(value)

    def _draft(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "origin": "LIBRARY",
            "draft_id": row["draft_id"],
            "action_id": row["action_id"],
            "target_version": int(row["target_version"]),
            "action_ref": f'{row["action_id"]}@{row["target_version"]}',
            "robot_type": row["robot_type"],
            "robot_label": row["robot_label"],
            "title": row["title"],
            "description": row["description"],
            "execution_mode": "SIMULATION",
            "capability": row["capability"],
            "asset_type": row["asset_type"],
            "access_mode": row["access_mode"],
            "role_template": _role_template(row["robot_type"]),
            "step_id_template": self._step_id_for_action(row["action_id"]),
            "fixed_parameters": json.loads(row["fixed_parameters_json"]),
            "editable_parameters": json.loads(row["editable_parameters_json"]),
            "default_timeout_s": int(row["default_timeout_s"]),
            "script": {
                "runtime": row["runtime"],
                "file_name": row["source_file_name"],
                "entrypoint": row["entrypoint"],
                "sha256": row["source_sha256"],
                "size_bytes": int(row["source_size_bytes"]),
            },
            "source_validation_state": row["source_validation_state"],
            "source_validation": self._validation(row["source_validation_json"]),
            "simulation_validation_state": row["simulation_validation_state"],
            "simulation_validation": self._validation(
                row["simulation_validation_json"]
            ),
            "state": row["state"],
            "revision": int(row["revision"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "validated_at": row["validated_at"],
            "published_at": row["published_at"],
        }

    def _draft_row(self, connection: sqlite3.Connection, draft_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM action_library_drafts WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
        if row is None:
            raise ActionLibraryNotFound(f"Action Draft not found: {draft_id}")
        return row

    def list_entries(self) -> list[dict[str, Any]]:
        builtins = []
        for action_ref, definition in sorted(self.builtin_actions.items()):
            action_id, version = _action_base_and_version(action_ref)
            builtins.append(
                {
                    "origin": "BUILTIN",
                    "draft_id": None,
                    "action_id": action_id,
                    "target_version": version,
                    **definition.model_dump(mode="json", by_alias=True),
                    "source_validation_state": "PASS",
                    "source_validation": {
                        "checks": ["versioned catalog contract"],
                        "detail": "Source identity is pinned by the installed catalog.",
                    },
                    "simulation_validation_state": "PASS",
                    "simulation_validation": {
                        "checks": ["installed simulation Action"],
                        "detail": "Built-in Action was qualified with its simulation build.",
                    },
                    "state": "PUBLISHED",
                    "revision": 1,
                    "created_by": "catalog",
                    "created_at": None,
                    "updated_at": None,
                    "validated_at": None,
                    "published_at": None,
                }
            )
        with self._connect() as connection:
            drafts = connection.execute(
                """
                SELECT * FROM action_library_drafts
                ORDER BY updated_at DESC, action_id, target_version
                """
            ).fetchall()
        return builtins + [self._draft(row) for row in drafts]

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._draft(self._draft_row(connection, draft_id))

    def create_draft(
        self,
        body: ActionDraftCreateV1,
        *,
        principal_id: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json", exclude={"request_id"})
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = self._replay(
                    connection,
                    principal_id=principal_id,
                    request_id=body.request_id,
                    operation="create_action_draft",
                    payload=payload,
                )
                if replay is not None:
                    return replay
                version = self._next_version(connection, body.action_id)
                digest, artifact, size = self._write_source(
                    body.source_code,
                    body.runtime,
                )
                timestamp = isoformat_z(parse_datetime(occurred_at))
                draft_id = f"action-draft-{uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO action_library_drafts (
                        draft_id, action_id, target_version, robot_type,
                        robot_label, title, description, capability, asset_type,
                        access_mode, fixed_parameters_json,
                        editable_parameters_json, default_timeout_s, runtime,
                        source_file_name, entrypoint, source_sha256,
                        source_artifact, source_size_bytes,
                        source_validation_state, source_validation_json,
                        simulation_validation_state, simulation_validation_json,
                        state, revision, created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'PENDING', NULL, 'PENDING', NULL, 'DRAFT', 1, ?, ?, ?)
                    """,
                    (
                        draft_id,
                        body.action_id,
                        version,
                        body.robot_type,
                        body.robot_label,
                        body.title,
                        body.description,
                        body.capability,
                        body.asset_type,
                        body.access_mode,
                        _json(body.fixed_parameters),
                        _json(body.editable_parameters),
                        body.default_timeout_s,
                        body.runtime,
                        body.source_file_name,
                        body.entrypoint,
                        digest,
                        artifact,
                        size,
                        principal_id,
                        timestamp,
                        timestamp,
                    ),
                )
                response = self._draft(self._draft_row(connection, draft_id))
                response["idempotent_replay"] = False
                self._record_request(
                    connection,
                    principal_id=principal_id,
                    request_id=body.request_id,
                    operation="create_action_draft",
                    payload=payload,
                    response=response,
                    occurred_at=occurred_at,
                )
                connection.commit()
                return response
        except sqlite3.IntegrityError as exc:
            raise ActionLibraryConflict(str(exc)) from exc

    def replace_draft(
        self,
        draft_id: str,
        body: ActionDraftReplaceV1,
        *,
        principal_id: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
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
                operation="replace_action_draft",
                payload=payload,
            )
            if replay is not None:
                return replay
            row = self._draft_row(connection, draft_id)
            if row["state"] != "DRAFT":
                raise ActionLibraryConflict("published Action Drafts cannot be changed")
            if int(row["revision"]) != body.expected_revision:
                raise ActionLibraryConflict("Action Draft revision mismatch")
            if body.action_id != row["action_id"]:
                raise ActionLibraryInvalid("Action ID cannot change after Draft creation")
            if body.source_code is None:
                digest = row["source_sha256"]
                artifact = row["source_artifact"]
                size = int(row["source_size_bytes"])
            else:
                digest, artifact, size = self._write_source(
                    body.source_code,
                    body.runtime,
                )
            timestamp = isoformat_z(parse_datetime(occurred_at))
            connection.execute(
                """
                UPDATE action_library_drafts SET
                    robot_type = ?, robot_label = ?, title = ?, description = ?,
                    capability = ?, asset_type = ?, access_mode = ?,
                    fixed_parameters_json = ?, editable_parameters_json = ?,
                    default_timeout_s = ?, runtime = ?, source_file_name = ?,
                    entrypoint = ?, source_sha256 = ?, source_artifact = ?,
                    source_size_bytes = ?, source_validation_state = 'PENDING',
                    source_validation_json = NULL,
                    simulation_validation_state = 'PENDING',
                    simulation_validation_json = NULL, validated_at = NULL,
                    revision = revision + 1, updated_at = ?
                WHERE draft_id = ?
                """,
                (
                    body.robot_type,
                    body.robot_label,
                    body.title,
                    body.description,
                    body.capability,
                    body.asset_type,
                    body.access_mode,
                    _json(body.fixed_parameters),
                    _json(body.editable_parameters),
                    body.default_timeout_s,
                    body.runtime,
                    body.source_file_name,
                    body.entrypoint,
                    digest,
                    artifact,
                    size,
                    timestamp,
                    draft_id,
                ),
            )
            response = self._draft(self._draft_row(connection, draft_id))
            response["idempotent_replay"] = False
            self._record_request(
                connection,
                principal_id=principal_id,
                request_id=body.request_id,
                operation="replace_action_draft",
                payload=payload,
                response=response,
                occurred_at=occurred_at,
            )
            connection.commit()
            return response

    def _source_checks(self, row: sqlite3.Row) -> list[dict[str, Any]]:
        source_path = self._source_path(row["source_artifact"])
        content = source_path.read_bytes()
        checks: list[dict[str, Any]] = []

        digest = hashlib.sha256(content).hexdigest()
        if digest != row["source_sha256"]:
            raise ValueError("stored source digest does not match its Draft")
        checks.append({"check": "source_digest", "state": "PASS"})

        source = content.decode("utf-8")
        definition = self._definition_from_row(row)
        checks.append({"check": "skill_parameter_contract", "state": "PASS"})

        if row["runtime"] == "UNITY_CSHARP":
            class_name = row["entrypoint"].rsplit(".", 1)[-1]
            declaration = re.compile(
                rf"\bclass\s+{re.escape(class_name)}\s*:\s*[^{{\n]*"
                r"\bISangUnityRobotAction\b"
            )
            if declaration.search(source) is None:
                raise ValueError(
                    "C# entrypoint must implement ISangUnityRobotAction"
                )
            if not re.search(
                rf'\bStepId\s*=>\s*"{re.escape(definition.step_id_template)}"',
                source,
            ):
                raise ValueError(
                    "C# Action StepId must equal the server-generated Action ID"
                )
            for required in ("TryBegin", "GetState"):
                if not re.search(rf"\b{required}\s*\(", source):
                    raise ValueError(f"C# Action must implement {required}()")
            forbidden = [token for token in _CSHARP_FORBIDDEN if token in source]
            if forbidden:
                raise ValueError(
                    "C# source uses blocked API tokens: " + ", ".join(forbidden)
                )
            if re.search(r"#\s*(?:load|r)\b", source):
                raise ValueError("C# source cannot load external code or resources")
            checks.extend(
                [
                    {"check": "unity_action_interface", "state": "PASS"},
                    {"check": "blocked_csharp_api_scan", "state": "PASS"},
                ]
            )
        else:
            tree = ast.parse(source, filename=row["source_file_name"])
            entrypoint = row["entrypoint"].rsplit(".", 1)[-1]
            found = False
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = (
                        [item.name for item in node.names]
                        if isinstance(node, ast.Import)
                        else [node.module or ""]
                    )
                    for name in names:
                        root = name.split(".", 1)[0]
                        if root in _PYTHON_FORBIDDEN_MODULES:
                            raise ValueError(f"Python import is blocked: {root}")
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name == entrypoint:
                        found = True
                        if len(node.args.args) < 2:
                            raise ValueError(
                                "Python entrypoint requires context and parameters"
                            )
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in _PYTHON_FORBIDDEN_CALLS:
                        raise ValueError(f"Python call is blocked: {node.func.id}")
                if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                    raise ValueError("Python dunder attribute access is blocked")
            if not found:
                raise ValueError("Python entrypoint function was not found")
            checks.extend(
                [
                    {"check": "python_ast_parse", "state": "PASS"},
                    {"check": "blocked_python_api_scan", "state": "PASS"},
                ]
            )
        return checks

    def validate_source(
        self,
        draft_id: str,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        timestamp = isoformat_z(parse_datetime(occurred_at))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._draft_row(connection, draft_id)
            if row["state"] != "DRAFT":
                raise ActionLibraryConflict("published Action Drafts cannot be revalidated")
            try:
                checks = self._source_checks(row)
                state = "PASS"
                detail = "Source and Skill input contracts passed without code execution."
            except (OSError, UnicodeDecodeError, SyntaxError, ValidationError, ValueError) as exc:
                checks = []
                state = "FAIL"
                detail = str(exc)
            report = {
                "state": state,
                "detail": detail,
                "checks": checks,
                "source_sha256": row["source_sha256"],
                "validated_at": timestamp,
                "central_executed_source": False,
            }
            connection.execute(
                """
                UPDATE action_library_drafts SET
                    source_validation_state = ?, source_validation_json = ?,
                    simulation_validation_state = 'PENDING',
                    simulation_validation_json = NULL,
                    validated_at = ?, revision = revision + 1, updated_at = ?
                WHERE draft_id = ?
                """,
                (state, _json(report), timestamp, timestamp, draft_id),
            )
            connection.commit()
            return self._draft(self._draft_row(connection, draft_id))

    def _installed_action(self, definition: RobotActionDefinitionV1) -> bool:
        candidates = list(self.builtin_actions.values())
        candidates.extend(self.published_definitions().values())
        return any(
            item.script.runtime == definition.script.runtime
            and item.script.sha256 == definition.script.sha256
            and item.script.entrypoint == definition.script.entrypoint
            and item.step_id_template == definition.step_id_template
            and item.capability == definition.capability
            and item.fixed_parameters == definition.fixed_parameters
            for item in candidates
        )

    @staticmethod
    def _step_matches(
        step: Any,
        definition: RobotActionDefinitionV1,
    ) -> bool:
        parameters = step.parameters if hasattr(step, "parameters") else step.get("parameters", {})
        return (
            (step.step_id if hasattr(step, "step_id") else step.get("step_id"))
            == definition.step_id_template
            and (step.capability if hasattr(step, "capability") else step.get("capability"))
            == definition.capability
            and all(parameters.get(key) == value for key, value in definition.fixed_parameters.items())
        )

    def validate_simulation(
        self,
        draft_id: str,
        body: ActionSimulationValidateV1,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        timestamp = isoformat_z(parse_datetime(occurred_at))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._draft_row(connection, draft_id)
            if row["state"] != "DRAFT":
                raise ActionLibraryConflict("published Action Drafts cannot be revalidated")
            if row["source_validation_state"] != "PASS":
                raise ActionLibraryInvalid("Source Gate must pass before Simulation Gate")
            definition = self._definition_from_row(row)
            adapter_entry = self.adapter_profiles.get(body.adapter_id)
            detail: str
            evidence_run_id: str | None = None
            checks: list[dict[str, Any]] = []
            state = "FAIL"
            if adapter_entry is None:
                detail = f"Simulation adapter is not configured: {body.adapter_id}"
            elif not self._installed_action(definition):
                detail = (
                    "This exact script digest is not installed in a validated simulation "
                    "build. The Build Owner must install and register it before Publish."
                )
            else:
                adapter, _ = adapter_entry
                adapter_step = next(
                    (
                        step
                        for step in adapter.steps
                        if self._step_matches(step, definition)
                    ),
                    None,
                )
                if adapter_step is None:
                    detail = "The selected simulation adapter does not expose this Action contract."
                else:
                    checks.extend(
                        [
                            {"check": "exact_script_installed", "state": "PASS"},
                            {"check": "adapter_contract_match", "state": "PASS"},
                            {"check": "physical_output_connected", "state": "PASS", "value": False},
                        ]
                    )
                    run_rows = connection.execute(
                        """
                        SELECT runs.*, registry.payload_json
                        FROM workflow_simulation_runs AS runs
                        JOIN orchestration_registry AS registry
                          ON registry.object_kind = 'workflow'
                         AND registry.object_id = runs.workflow_id
                         AND registry.version = runs.workflow_version
                        WHERE runs.adapter_id = ? AND runs.state = 'COMPLETE'
                        ORDER BY runs.finished_at DESC, runs.created_at DESC
                        """,
                        (body.adapter_id,),
                    ).fetchall()
                    for run in run_rows:
                        result = json.loads(run["result_json"] or "{}")
                        workflow = json.loads(run["payload_json"])
                        if (
                            result.get("physical_output_connected") is False
                            and result.get("orchestrator_state") == "COMPLETE"
                            and any(
                                self._step_matches(step, definition)
                                for step in workflow.get("steps", [])
                            )
                        ):
                            evidence_run_id = run["run_id"]
                            state = "PASS"
                            detail = (
                                "Exact installed script and completed output-disconnected "
                                "Simulation Run evidence matched."
                            )
                            checks.append(
                                {"check": "completed_simulation_run", "state": "PASS"}
                            )
                            break
                    else:
                        detail = (
                            "No completed output-disconnected Simulation Run contains this "
                            "exact Action contract."
                        )
            report = {
                "state": state,
                "detail": detail,
                "checks": checks,
                "adapter_id": body.adapter_id,
                "adapter_profile_sha256": (
                    None if adapter_entry is None else adapter_entry[1]
                ),
                "evidence_run_id": evidence_run_id,
                "source_sha256": row["source_sha256"],
                "validated_at": timestamp,
                "physical_output_connected": False,
            }
            connection.execute(
                """
                UPDATE action_library_drafts SET
                    simulation_validation_state = ?, simulation_validation_json = ?,
                    validated_at = ?, revision = revision + 1, updated_at = ?
                WHERE draft_id = ?
                """,
                (state, _json(report), timestamp, timestamp, draft_id),
            )
            connection.commit()
            return self._draft(self._draft_row(connection, draft_id))

    def publish(
        self,
        draft_id: str,
        body: ActionPublishV1,
        *,
        principal_id: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        payload = {
            "draft_id": draft_id,
            "expected_revision": body.expected_revision,
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(
                connection,
                principal_id=principal_id,
                request_id=body.request_id,
                operation="publish_action",
                payload=payload,
            )
            if replay is not None:
                return replay
            row = self._draft_row(connection, draft_id)
            if row["state"] != "DRAFT":
                raise ActionLibraryConflict("Action Draft is already published")
            if int(row["revision"]) != body.expected_revision:
                raise ActionLibraryConflict("Action Draft revision mismatch")
            if row["source_validation_state"] != "PASS":
                raise ActionLibraryInvalid("Source Gate has not passed")
            if row["simulation_validation_state"] != "PASS":
                raise ActionLibraryInvalid("Simulation Gate has not passed")
            definition = self._definition_from_row(row)
            definition_payload = definition.model_dump(mode="json", by_alias=True)
            definition_digest = payload_digest(definition_payload)
            validation = {
                "source": json.loads(row["source_validation_json"]),
                "simulation": json.loads(row["simulation_validation_json"]),
            }
            timestamp = isoformat_z(parse_datetime(occurred_at))
            try:
                connection.execute(
                    """
                    INSERT INTO action_library_publications (
                        action_id, version, action_ref, definition_json,
                        definition_sha256, source_sha256, source_artifact,
                        validation_json, source_draft_id, published_by, published_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["action_id"],
                        row["target_version"],
                        definition.action_ref,
                        _json(definition_payload),
                        definition_digest,
                        row["source_sha256"],
                        row["source_artifact"],
                        _json(validation),
                        draft_id,
                        principal_id,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE action_library_drafts SET
                        state = 'PUBLISHED', revision = revision + 1,
                        updated_at = ?, published_at = ?
                    WHERE draft_id = ?
                    """,
                    (timestamp, timestamp, draft_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ActionLibraryConflict(str(exc)) from exc
            response = self._draft(self._draft_row(connection, draft_id))
            response["definition_sha256"] = definition_digest
            response["idempotent_replay"] = False
            self._record_request(
                connection,
                principal_id=principal_id,
                request_id=body.request_id,
                operation="publish_action",
                payload=payload,
                response=response,
                occurred_at=occurred_at,
            )
            connection.commit()
            return response

    def published_definitions(self) -> dict[str, RobotActionDefinitionV1]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT action_ref, definition_json
                FROM action_library_publications
                ORDER BY action_id, version
                """
            ).fetchall()
        return {
            row["action_ref"]: RobotActionDefinitionV1.model_validate_json(
                row["definition_json"]
            )
            for row in rows
        }
