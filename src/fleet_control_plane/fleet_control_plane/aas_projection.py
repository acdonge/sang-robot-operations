"""IDTA AAS 3.1.2 validation and command-free asset projection storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
from typing import Any, Mapping
from urllib.parse import urlsplit
import zipfile

from jsonschema import Draft201909Validator
from jsonschema.exceptions import SchemaError
import yaml

from fleet_control_plane.recording_storage import FilesystemObjectStore
from fleet_control_plane.status import isoformat_z


MAX_AAS_IMPORT_BYTES = 64 * 1024 * 1024
MAX_AASX_ENTRIES = 2048
MAX_AASX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_AASX_COMPRESSION_RATIO = 200
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+ -]{0,255}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,191}$")


AAS_PROJECTION_MIGRATION = """
CREATE TABLE IF NOT EXISTS aas_artifacts (
    sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
    format TEXT NOT NULL CHECK (format IN ('json', 'aasx-json')),
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
    object_key TEXT NOT NULL UNIQUE,
    source_file_name TEXT NOT NULL,
    metamodel_version TEXT NOT NULL,
    environment_sha256 TEXT NOT NULL CHECK (length(environment_sha256) = 64),
    environment_size_bytes INTEGER NOT NULL CHECK (environment_size_bytes > 0),
    environment_object_key TEXT NOT NULL,
    imported_by TEXT REFERENCES principals(principal_id) ON DELETE RESTRICT,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS aas_asset_projections (
    projection_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE RESTRICT,
    artifact_sha256 TEXT NOT NULL REFERENCES aas_artifacts(sha256) ON DELETE RESTRICT,
    shell_id TEXT NOT NULL,
    shell_id_short TEXT,
    global_asset_id TEXT NOT NULL,
    submodel_ids_json TEXT NOT NULL,
    operation_element_count INTEGER NOT NULL CHECK (operation_element_count >= 0),
    robotics_profile_id TEXT,
    opcua_endpoint TEXT,
    projection_mode TEXT NOT NULL CHECK (projection_mode = 'read_only'),
    created_at TEXT NOT NULL,
    UNIQUE (asset_id, artifact_sha256, shell_id),
    CHECK (
        (robotics_profile_id IS NULL AND opcua_endpoint IS NULL)
        OR (robotics_profile_id IS NOT NULL AND opcua_endpoint IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS aas_asset_projections_asset_created
    ON aas_asset_projections (asset_id, created_at DESC);
"""


class AasProjectionInvalid(ValueError):
    """An AAS artifact cannot be safely validated or linked."""


class AasProjectionConflict(ValueError):
    """An immutable AAS identity conflicts with an existing projection."""


class AasProjectionNotFound(ValueError):
    """An AAS projection does not exist."""


@dataclass(frozen=True)
class AasModelLock:
    specification: str
    release_tag: str
    commit: str
    schema_id: str
    schema_path: Path
    schema_sha256: str


@dataclass(frozen=True)
class ParsedAasEnvironment:
    environment: Mapping[str, Any]
    canonical_json: bytes
    format: str


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AasProjectionInvalid(f"{label} must be a mapping")
    return value


def _resolve_schema_path(lock_path: Path, configured: str) -> Path:
    path = Path(configured)
    candidates = (path,) if path.is_absolute() else (
        Path.cwd() / path,
        lock_path.parents[2] / path,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise AasProjectionInvalid(f"pinned AAS JSON Schema is missing: {configured}")


def load_aas_model_lock(path: str | Path) -> tuple[AasModelLock, Mapping[str, Any]]:
    lock_path = Path(path).resolve()
    try:
        raw = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AasProjectionInvalid(f"failed to load AAS model lock: {exc}") from exc
    lock = _mapping(raw, "AAS model lock")
    if lock.get("schema_version") != "1.0.0":
        raise AasProjectionInvalid("unsupported AAS model lock schema_version")
    definition = _mapping(lock.get("json_schema"), "AAS JSON Schema lock")
    configured_path = definition.get("path")
    if not isinstance(configured_path, str):
        raise AasProjectionInvalid("AAS JSON Schema path is invalid")
    schema_path = _resolve_schema_path(lock_path, configured_path)
    expected_digest = definition.get("sha256")
    digest = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    if digest != expected_digest:
        raise AasProjectionInvalid(f"pinned AAS JSON Schema digest changed: {digest}")
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft201909Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, SchemaError) as exc:
        raise AasProjectionInvalid(f"pinned AAS JSON Schema is invalid: {exc}") from exc
    if schema.get("$id") != definition.get("id"):
        raise AasProjectionInvalid("AAS JSON Schema ID differs from its lock")
    commit = lock.get("commit")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise AasProjectionInvalid("AAS model lock commit is invalid")
    return (
        AasModelLock(
            specification=str(lock.get("specification")),
            release_tag=str(lock.get("release_tag")),
            commit=commit,
            schema_id=str(definition.get("id")),
            schema_path=schema_path,
            schema_sha256=digest,
        ),
        schema,
    )


def _canonical_environment(environment: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            environment,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _validate_environment(
    value: Any, validator: Draft201909Validator
) -> Mapping[str, Any]:
    environment = _mapping(value, "AAS Environment")
    errors = sorted(validator.iter_errors(environment), key=lambda item: list(item.path))
    if errors:
        error = errors[0]
        location = "/".join(str(part) for part in error.absolute_path) or "$"
        raise AasProjectionInvalid(
            f"AAS 3.1.2 schema validation failed at {location}: {error.message}"
        )
    return environment


def _read_json_environment(
    payload: bytes, validator: Draft201909Validator
) -> Mapping[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AasProjectionInvalid(f"AAS JSON is invalid: {exc}") from exc
    return _validate_environment(value, validator)


def _safe_zip_member(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & 0x1:
        raise AasProjectionInvalid("encrypted AASX entries are not accepted")
    path = PurePosixPath(info.filename)
    if (
        "\\" in info.filename
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise AasProjectionInvalid("AASX contains a non-canonical member path")
    unix_mode = (info.external_attr >> 16) & 0o170000
    if unix_mode == 0o120000:
        raise AasProjectionInvalid("AASX symbolic links are not accepted")
    if info.file_size and info.compress_size == 0:
        raise AasProjectionInvalid("AASX entry has an invalid compression size")
    if info.compress_size and info.file_size / info.compress_size > MAX_AASX_COMPRESSION_RATIO:
        raise AasProjectionInvalid("AASX entry exceeds the compression-ratio limit")


def _read_aasx_environment(
    payload: bytes, validator: Draft201909Validator
) -> Mapping[str, Any]:
    try:
        archive = zipfile.ZipFile(BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise AasProjectionInvalid("AASX is not a valid ZIP package") from exc
    with archive:
        entries = archive.infolist()
        if not entries or len(entries) > MAX_AASX_ENTRIES:
            raise AasProjectionInvalid("AASX entry count is invalid")
        names: set[str] = set()
        total_size = 0
        json_candidates: list[Mapping[str, Any]] = []
        has_xml_payload = False
        for info in entries:
            _safe_zip_member(info)
            normalized = info.filename.casefold()
            if normalized in names:
                raise AasProjectionInvalid("AASX contains duplicate member names")
            names.add(normalized)
            total_size += info.file_size
            if total_size > MAX_AASX_UNCOMPRESSED_BYTES:
                raise AasProjectionInvalid("AASX uncompressed size exceeds the limit")
            if info.is_dir():
                continue
            suffix = PurePosixPath(info.filename).suffix.casefold()
            if suffix == ".xml" and "aas" in normalized:
                has_xml_payload = True
            if suffix != ".json" or info.file_size > MAX_AAS_IMPORT_BYTES:
                continue
            candidate = archive.read(info)
            try:
                json_candidates.append(_read_json_environment(candidate, validator))
            except AasProjectionInvalid:
                continue
        if len(json_candidates) == 1:
            return json_candidates[0]
        if len(json_candidates) > 1:
            raise AasProjectionInvalid("AASX contains multiple valid AAS Environments")
        if has_xml_payload:
            raise AasProjectionInvalid(
                "this AASX contains only XML; v1 requires an AAS 3.1 JSON Environment"
            )
        raise AasProjectionInvalid("AASX contains no valid AAS 3.1 JSON Environment")


def parse_aas_environment(
    payload: bytes,
    *,
    source_file_name: str,
    validator: Draft201909Validator,
) -> ParsedAasEnvironment:
    if not payload or len(payload) > MAX_AAS_IMPORT_BYTES:
        raise AasProjectionInvalid("AAS artifact size is outside the accepted range")
    if SAFE_FILENAME.fullmatch(source_file_name) is None:
        raise AasProjectionInvalid("AAS source filename is not canonical")
    suffix = Path(source_file_name).suffix.casefold()
    if suffix == ".json":
        environment = _read_json_environment(payload, validator)
        format_name = "json"
    elif suffix == ".aasx":
        environment = _read_aasx_environment(payload, validator)
        format_name = "aasx-json"
    else:
        raise AasProjectionInvalid("AAS artifact must end in .json or .aasx")
    return ParsedAasEnvironment(
        environment=environment,
        canonical_json=_canonical_environment(environment),
        format=format_name,
    )


def _reference_target(reference: Any) -> str | None:
    if not isinstance(reference, Mapping):
        return None
    keys = reference.get("keys")
    if not isinstance(keys, list):
        return None
    for key in reversed(keys):
        if isinstance(key, Mapping) and key.get("type") == "Submodel":
            value = key.get("value")
            return value if isinstance(value, str) and value else None
    return None


def _operation_count(value: Any) -> int:
    if isinstance(value, Mapping):
        return int(value.get("modelType") == "Operation") + sum(
            _operation_count(item) for item in value.values()
        )
    if isinstance(value, list):
        return sum(_operation_count(item) for item in value)
    return 0


def _select_shell(
    environment: Mapping[str, Any], shell_id: str | None
) -> tuple[Mapping[str, Any], list[str], int]:
    shells = environment.get("assetAdministrationShells") or []
    if not isinstance(shells, list) or not shells:
        raise AasProjectionInvalid("AAS Environment contains no shells")
    if shell_id is None:
        if len(shells) != 1:
            raise AasProjectionInvalid("shell_id is required for a multi-shell AAS artifact")
        shell = _mapping(shells[0], "AssetAdministrationShell")
    else:
        matches = [item for item in shells if isinstance(item, Mapping) and item.get("id") == shell_id]
        if len(matches) != 1:
            raise AasProjectionInvalid(f"AAS shell_id is absent or ambiguous: {shell_id}")
        shell = matches[0]
    embedded_submodels = {
        item.get("id"): item
        for item in environment.get("submodels") or []
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    submodel_ids = []
    for reference in shell.get("submodels") or []:
        target = _reference_target(reference)
        if target is None:
            raise AasProjectionInvalid("AAS shell has an invalid Submodel reference")
        submodel_ids.append(target)
    if len(submodel_ids) != len(set(submodel_ids)):
        raise AasProjectionInvalid("AAS shell has duplicate Submodel references")
    missing = set(submodel_ids) - set(embedded_submodels)
    if missing:
        raise AasProjectionInvalid(
            "AAS import closure is missing Submodels: " + ", ".join(sorted(missing))
        )
    operations = sum(_operation_count(embedded_submodels[item]) for item in submodel_ids)
    return shell, submodel_ids, operations


def _validate_opcua_link(profile_id: str | None, endpoint: str | None) -> None:
    if (profile_id is None) != (endpoint is None):
        raise AasProjectionInvalid(
            "robotics_profile_id and opcua_endpoint must be supplied together"
        )
    if profile_id is None:
        return
    if SAFE_ID.fullmatch(profile_id) is None:
        raise AasProjectionInvalid("robotics_profile_id is invalid")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "opc.tcp" or parsed.hostname is None or parsed.port is None:
        raise AasProjectionInvalid("opcua_endpoint must be an opc.tcp URL with a port")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AasProjectionInvalid("opcua_endpoint may not contain credentials, query, or fragment")


class AasProjectionCatalog:
    """Append-only AAS artifact catalog linked to the operational Asset Registry."""

    def __init__(
        self,
        db_path: str | Path,
        object_store: FilesystemObjectStore,
        model_lock_path: str | Path,
    ) -> None:
        self.path = Path(db_path)
        self.object_store = object_store
        self.model_lock, schema = load_aas_model_lock(model_lock_path)
        self.validator = Draft201909Validator(schema)
        with self._connect() as connection:
            connection.executescript(AAS_PROJECTION_MIGRATION)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _projection_id(asset_id: str, shell_id: str, digest: str) -> str:
        identity = hashlib.sha256(f"{asset_id}\0{shell_id}\0{digest}".encode()).hexdigest()
        return f"aasproj-{identity[:24]}"

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "projection_id": row["projection_id"],
            "asset_id": row["asset_id"],
            "artifact_sha256": row["artifact_sha256"],
            "artifact_format": row["format"],
            "artifact_media_type": row["media_type"],
            "artifact_size_bytes": row["size_bytes"],
            "source_file_name": row["source_file_name"],
            "metamodel_version": row["metamodel_version"],
            "environment_sha256": row["environment_sha256"],
            "shell_id": row["shell_id"],
            "shell_id_short": row["shell_id_short"],
            "global_asset_id": row["global_asset_id"],
            "submodel_ids": json.loads(row["submodel_ids_json"]),
            "operation_element_count": row["operation_element_count"],
            "robotics_profile_id": row["robotics_profile_id"],
            "opcua_endpoint": row["opcua_endpoint"],
            "projection_mode": row["projection_mode"],
            "imported_by": row["imported_by"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _select_sql() -> str:
        return """
            SELECT projection.*, artifact.format, artifact.media_type,
                   artifact.size_bytes, artifact.source_file_name,
                   artifact.metamodel_version, artifact.environment_sha256,
                   artifact.imported_by
            FROM aas_asset_projections AS projection
            JOIN aas_artifacts AS artifact
              ON artifact.sha256 = projection.artifact_sha256
        """

    async def import_artifact(
        self,
        payload: bytes,
        *,
        asset_id: str,
        source_file_name: str,
        shell_id: str | None,
        robotics_profile_id: str | None,
        opcua_endpoint: str | None,
        imported_by: str | None,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        _validate_opcua_link(robotics_profile_id, opcua_endpoint)
        parsed = parse_aas_environment(
            payload,
            source_file_name=source_file_name,
            validator=self.validator,
        )
        shell, submodel_ids, operation_count = _select_shell(
            parsed.environment, shell_id
        )
        selected_shell_id = shell.get("id")
        if not isinstance(selected_shell_id, str) or not selected_shell_id:
            raise AasProjectionInvalid("AAS shell has no stable id")
        asset_information = _mapping(shell.get("assetInformation"), "assetInformation")
        global_asset_id = asset_information.get("globalAssetId")
        if not isinstance(global_asset_id, str) or not global_asset_id:
            raise AasProjectionInvalid("AAS shell requires assetInformation.globalAssetId")

        digest = hashlib.sha256(payload).hexdigest()
        environment_digest = hashlib.sha256(parsed.canonical_json).hexdigest()
        projection_id = self._projection_id(asset_id, selected_shell_id, digest)
        extension = ".aasx" if parsed.format == "aasx-json" else ".json"
        raw_key = f"aas/artifacts/{digest[:2]}/{digest}/source{extension}"
        environment_key = f"aas/environments/{environment_digest[:2]}/{environment_digest}.json"
        media_type = (
            "application/asset-administration-shell-package"
            if parsed.format == "aasx-json"
            else "application/json"
        )
        occurred = isoformat_z(occurred_at)

        with self._connect() as connection:
            asset = connection.execute(
                "SELECT lifecycle_state FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            if asset is None:
                raise AasProjectionInvalid(f"AAS projection references unknown asset: {asset_id}")
            if asset["lifecycle_state"] != "active":
                raise AasProjectionInvalid("AAS projection cannot target a decommissioned asset")

        await self.object_store.put_bytes(raw_key, payload, sha256=digest)
        await self.object_store.put_bytes(
            environment_key,
            parsed.canonical_json,
            sha256=environment_digest,
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO aas_artifacts (
                    sha256, format, media_type, size_bytes, object_key,
                    source_file_name, metamodel_version, environment_sha256,
                    environment_size_bytes, environment_object_key,
                    imported_by, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    digest,
                    parsed.format,
                    media_type,
                    len(payload),
                    raw_key,
                    source_file_name,
                    self.model_lock.specification,
                    environment_digest,
                    len(parsed.canonical_json),
                    environment_key,
                    imported_by,
                    occurred,
                ),
            )
            existing = connection.execute(
                self._select_sql() + " WHERE projection.projection_id = ?",
                (projection_id,),
            ).fetchone()
            if existing is not None:
                expected = (
                    asset_id,
                    digest,
                    selected_shell_id,
                    robotics_profile_id,
                    opcua_endpoint,
                )
                actual = (
                    existing["asset_id"],
                    existing["artifact_sha256"],
                    existing["shell_id"],
                    existing["robotics_profile_id"],
                    existing["opcua_endpoint"],
                )
                if actual != expected:
                    raise AasProjectionConflict("AAS projection identity already differs")
                return self._row(existing)
            connection.execute(
                """
                INSERT INTO aas_asset_projections (
                    projection_id, asset_id, artifact_sha256, shell_id,
                    shell_id_short, global_asset_id, submodel_ids_json,
                    operation_element_count, robotics_profile_id, opcua_endpoint,
                    projection_mode, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'read_only', ?)
                """,
                (
                    projection_id,
                    asset_id,
                    digest,
                    selected_shell_id,
                    shell.get("idShort"),
                    global_asset_id,
                    json.dumps(submodel_ids, separators=(",", ":")),
                    operation_count,
                    robotics_profile_id,
                    opcua_endpoint,
                    occurred,
                ),
            )
            row = connection.execute(
                self._select_sql() + " WHERE projection.projection_id = ?",
                (projection_id,),
            ).fetchone()
            return self._row(row)

    def list_projections(self, *, asset_id: str | None = None) -> list[dict[str, Any]]:
        sql = self._select_sql()
        parameters: tuple[Any, ...] = ()
        if asset_id is not None:
            sql += " WHERE projection.asset_id = ?"
            parameters = (asset_id,)
        sql += " ORDER BY projection.created_at DESC, projection.projection_id"
        with self._connect() as connection:
            return [self._row(row) for row in connection.execute(sql, parameters)]

    def get_projection(self, projection_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                self._select_sql() + " WHERE projection.projection_id = ?",
                (projection_id,),
            ).fetchone()
        if row is None:
            raise AasProjectionNotFound(f"unknown AAS projection: {projection_id}")
        return self._row(row)

    def open_artifact(self, projection_id: str, *, environment: bool) -> Path:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT artifact.*
                FROM aas_asset_projections AS projection
                JOIN aas_artifacts AS artifact
                  ON artifact.sha256 = projection.artifact_sha256
                WHERE projection.projection_id = ?
                """,
                (projection_id,),
            ).fetchone()
        if row is None:
            raise AasProjectionNotFound(f"unknown AAS projection: {projection_id}")
        if environment:
            return self.object_store.open_path(
                row["environment_object_key"],
                size_bytes=row["environment_size_bytes"],
                sha256=row["environment_sha256"],
            )
        return self.object_store.open_path(
            row["object_key"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
        )
