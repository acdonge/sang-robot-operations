"""Generate isolated Phase 4 Agent/Gateway runtime bundles."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tempfile
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator, model_validator
import yaml

from fleet_control_plane.models import FleetModel, SafeId, ShortText
from fleet_control_plane.security_enroll import enroll_phase3_edge
from fleet_control_plane.status import isoformat_z, parse_datetime
from fleet_control_plane.store import FleetStore
from robot_agent.local_health import LocalHealthError, load_mapping_health_digest
from robot_agent.manifest import ManifestValidationError, RobotManifest, load_manifest
from sang_opcua_gateway.contract import ContractError, load_gateway_contract
from sang_security.pki import PkiError, provision_agent_runtime


INVENTORY_SCHEMA_VERSION = "1.0.0"
DEFAULT_COMPOSE_FILE = "docker/compose.phase4-runtime.yaml"
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")


class RuntimeDeploymentError(ValueError):
    """The runtime inventory is invalid or conflicts with existing output."""


def _relative_path(value: str, label: str) -> str:
    if "\\" in value:
        raise ValueError(f"{label} must use POSIX path separators")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a canonical relative path")
    canonical = path.as_posix()
    if canonical != value:
        raise ValueError(f"{label} must be a canonical relative path")
    return canonical


def _canonical_host(value: str) -> str:
    host = value.strip()
    if not host or len(host) > 253 or "/" in host:
        raise ValueError("endpoint_host must be one DNS name or IP address")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        labels = host.rstrip(".").lower().split(".")
        if any(not label or len(label) > 63 or DNS_LABEL.fullmatch(label) is None for label in labels):
            raise ValueError("endpoint_host is not a valid DNS name")
        return ".".join(labels)


def _endpoint(host: str, port: int) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    return f"opc.tcp://{rendered_host}:{port}/sang/"


class CentralRuntimeConfig(FleetModel):
    ingress_url: str

    @field_validator("ingress_url")
    @classmethod
    def secure_ingress_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("central.ingress_url has an invalid port") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or port is None
            or not 1 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("central.ingress_url must be an HTTPS origin with an explicit port")
        return value.rstrip("/")


class AgentRuntimeConfig(FleetModel):
    agent_id: SafeId
    agent_name: ShortText
    robot_id: SafeId
    processing_node_id: SafeId
    endpoint_host: str
    opcua_port: int = Field(ge=1024, le=65535)
    preview_port: int = Field(ge=1024, le=65535)
    ros_domain_id: int = Field(ge=0, le=232)
    manifest: str
    mapping: str
    runtime_directory: str
    heartbeat_interval_s: float = Field(default=1.0, gt=0.0)
    heartbeat_timeout_s: float = Field(default=2.0, gt=0.0)
    startup_grace_s: float = Field(default=3.0, ge=0.0)

    @field_validator("endpoint_host")
    @classmethod
    def endpoint_host_is_canonical(cls, value: str) -> str:
        return _canonical_host(value)

    @field_validator("manifest", "mapping", "runtime_directory")
    @classmethod
    def paths_are_relative(cls, value: str, info) -> str:
        canonical = _relative_path(value, info.field_name)
        if info.field_name == "runtime_directory" and PurePosixPath(canonical).parts[0] != "agents":
            raise ValueError("runtime_directory must be below agents/")
        return canonical

    @model_validator(mode="after")
    def local_ports_are_distinct(self) -> "AgentRuntimeConfig":
        if self.opcua_port == self.preview_port:
            raise ValueError("opcua_port and preview_port must differ")
        return self


class RuntimeInventory(FleetModel):
    schema_version: str
    central: CentralRuntimeConfig
    runtimes: list[AgentRuntimeConfig] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def runtime_allocations_are_unique(self) -> "RuntimeInventory":
        if self.schema_version != INVENTORY_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {INVENTORY_SCHEMA_VERSION}"
            )
        unique_fields = (
            ("agent_id", [item.agent_id for item in self.runtimes]),
            ("robot_id", [item.robot_id for item in self.runtimes]),
            ("runtime_directory", [item.runtime_directory for item in self.runtimes]),
        )
        for label, values in unique_fields:
            duplicates = sorted({value for value in values if values.count(value) > 1})
            if duplicates:
                raise ValueError(f"duplicate {label}: {', '.join(duplicates)}")

        listeners: dict[tuple[str, int], str] = {}
        domains: dict[tuple[str, int], str] = {}
        node_hosts: dict[str, str] = {}
        host_nodes: dict[str, str] = {}
        for runtime in self.runtimes:
            previous_host = node_hosts.get(runtime.processing_node_id)
            if previous_host is not None and previous_host != runtime.endpoint_host:
                raise ValueError(
                    f"processing_node_id {runtime.processing_node_id} uses both "
                    f"{previous_host} and {runtime.endpoint_host}"
                )
            previous_node = host_nodes.get(runtime.endpoint_host)
            if previous_node is not None and previous_node != runtime.processing_node_id:
                raise ValueError(
                    f"endpoint_host {runtime.endpoint_host} is assigned to both "
                    f"{previous_node} and {runtime.processing_node_id}"
                )
            node_hosts[runtime.processing_node_id] = runtime.endpoint_host
            host_nodes[runtime.endpoint_host] = runtime.processing_node_id
            for kind, port in (
                ("OPC UA", runtime.opcua_port),
                ("preview", runtime.preview_port),
            ):
                key = (runtime.processing_node_id, port)
                previous = listeners.get(key)
                if previous is not None:
                    raise ValueError(
                        f"port {port} on {runtime.processing_node_id} is shared by "
                        f"{previous} and {runtime.agent_id} {kind}"
                    )
                listeners[key] = f"{runtime.agent_id} {kind}"
            domain_key = (runtime.processing_node_id, runtime.ros_domain_id)
            previous = domains.get(domain_key)
            if previous is not None:
                raise ValueError(
                    f"ROS_DOMAIN_ID {runtime.ros_domain_id} on "
                    f"{runtime.processing_node_id} is shared by {previous} and "
                    f"{runtime.agent_id}"
                )
            domains[domain_key] = runtime.agent_id
        return self


class RuntimeAllocation(FleetModel):
    agent_id: SafeId
    robot_id: SafeId
    processing_node_id: SafeId
    endpoint_host: str
    opcua_port: int = Field(ge=1024, le=65535)
    preview_port: int = Field(ge=1024, le=65535)
    ros_domain_id: int = Field(ge=0, le=232)
    runtime_directory: str
    generation_directory: str = Field(min_length=1, max_length=4096)

    @field_validator("endpoint_host")
    @classmethod
    def allocation_host_is_canonical(cls, value: str) -> str:
        return _canonical_host(value)

    @field_validator("runtime_directory")
    @classmethod
    def allocation_path_is_relative(cls, value: str) -> str:
        canonical = _relative_path(value, "runtime_directory")
        if PurePosixPath(canonical).parts[0] != "agents":
            raise ValueError("runtime_directory must be below agents/")
        return canonical

    @field_validator("generation_directory")
    @classmethod
    def generation_path_is_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("generation_directory must be absolute")
        canonical = str(Path(value).expanduser().resolve(strict=False))
        if value != canonical:
            raise ValueError("generation_directory must be canonical")
        return canonical

    @model_validator(mode="after")
    def allocation_ports_are_distinct(self) -> "RuntimeAllocation":
        if self.opcua_port == self.preview_port:
            raise ValueError("opcua_port and preview_port must differ")
        return self


@dataclass(frozen=True)
class ResolvedRuntime:
    config: AgentRuntimeConfig
    manifest: RobotManifest
    manifest_path: Path
    mapping_path: Path
    mapping_digest: str
    runtime_path: Path
    data_path: Path
    replay_db_path: Path
    env_path: Path
    deployment_path: Path
    opcua_endpoint: str


def parse_runtime_inventory(value: Any) -> RuntimeInventory:
    try:
        return RuntimeInventory.model_validate(value)
    except ValidationError as exc:
        raise RuntimeDeploymentError(str(exc)) from exc


def load_runtime_inventory(path: Path) -> RuntimeInventory:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeDeploymentError(f"cannot load runtime inventory: {exc}") from exc
    return parse_runtime_inventory(value)


def _contained_file(root: Path, relative: str, label: str) -> Path:
    try:
        path = (root / relative).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RuntimeDeploymentError(f"{label} escapes or is missing from workspace: {relative}") from exc
    if not path.is_file():
        raise RuntimeDeploymentError(f"{label} is not a file: {relative}")
    return path


def resolve_runtime_inventory(
    inventory: RuntimeInventory,
    *,
    workspace_root: Path,
    runtime_root: Path,
) -> tuple[ResolvedRuntime, ...]:
    workspace = workspace_root.expanduser().resolve(strict=True)
    runtime_base = runtime_root.expanduser().resolve(strict=False)
    resolved = []
    for config in inventory.runtimes:
        manifest_path = _contained_file(workspace, config.manifest, "manifest")
        mapping_path = _contained_file(workspace, config.mapping, "mapping")
        try:
            manifest = load_manifest(manifest_path)
            if manifest.robot.robot_id != config.robot_id:
                raise RuntimeDeploymentError(
                    f"{config.agent_id} robot_id differs from its manifest"
                )
            mapping_digest = load_mapping_health_digest(
                mapping_path,
                expected_robot_id=config.robot_id,
            )
            load_gateway_contract(
                manifest_path=manifest_path,
                mapping_path=mapping_path,
                model_contract_path=workspace / "contracts/opcua/model-contract.yaml",
                status_codes_path=workspace / "contracts/opcua/status-code-mapping.yaml",
                nodeset_path=workspace / "contracts/opcua/SANG.NodeSet2.xml",
                endpoint=_endpoint(config.endpoint_host, config.opcua_port),
                agent_id=config.agent_id,
                preview_base_url=f"http://127.0.0.1:{config.preview_port}",
                allow_remote_endpoint=True,
            )
        except (ContractError, LocalHealthError, ManifestValidationError, OSError) as exc:
            raise RuntimeDeploymentError(
                f"{config.agent_id} manifest/mapping contract is invalid: {exc}"
            ) from exc
        runtime_path = (runtime_base / config.runtime_directory).resolve(strict=False)
        try:
            runtime_path.relative_to(runtime_base)
        except ValueError as exc:
            raise RuntimeDeploymentError(
                f"{config.agent_id} runtime_directory escapes runtime root"
            ) from exc
        resolved.append(
            ResolvedRuntime(
                config=config,
                manifest=manifest,
                manifest_path=manifest_path,
                mapping_path=mapping_path,
                mapping_digest=mapping_digest,
                runtime_path=runtime_path,
                data_path=runtime_path / "data",
                replay_db_path=runtime_path / "data/local-health-replay.db",
                env_path=runtime_base / "config" / f"{config.agent_id}.env",
                deployment_path=runtime_path / "deployment.json",
                opcua_endpoint=_endpoint(config.endpoint_host, config.opcua_port),
            )
        )
    return tuple(resolved)


def _host_path(root: Path, relative: str) -> Path:
    return root.expanduser().resolve(strict=False) / relative


def _env_value(value: object) -> str:
    rendered = str(value)
    if not rendered or any(character in rendered for character in "\r\n\0"):
        raise RuntimeDeploymentError("environment values must be non-empty single-line strings")
    if re.fullmatch(r"[A-Za-z0-9_./:@+\-]+", rendered):
        return rendered
    if "'" in rendered:
        raise RuntimeDeploymentError("environment values may not contain single quotes")
    return f"'{rendered}'"


def _environment_text(
    runtime: ResolvedRuntime,
    *,
    inventory: RuntimeInventory,
    host_workspace_root: Path,
    host_runtime_root: Path,
    host_pki_root: Path,
    image: str,
    uid: int,
    gid: int,
) -> str:
    config = runtime.config
    values = {
        "SANG_IMAGE": image,
        "SANG_UID": uid,
        "SANG_GID": gid,
        "SANG_WORKSPACE": host_workspace_root,
        "SANG_AGENT_DATA": _host_path(host_runtime_root, f"{config.runtime_directory}/data"),
        "SANG_EDGE_SECRETS": _host_path(
            host_pki_root,
            f"roles/agents/{config.agent_id}/edge",
        ),
        "SANG_AGENT_ID": config.agent_id,
        "SANG_AGENT_NAME": config.agent_name,
        "SANG_ROBOT_ID": config.robot_id,
        "SANG_PROCESSING_NODE_ID": config.processing_node_id,
        "SANG_MANIFEST": f"/ws/{config.manifest}",
        "SANG_MAPPING": f"/ws/{config.mapping}",
        "CENTRAL_INGRESS_URL": inventory.central.ingress_url,
        "EDGE_OPCUA_ENDPOINT": runtime.opcua_endpoint,
        "EDGE_PREVIEW_PORT": config.preview_port,
        "LOCAL_HEALTH_KEY_ID": f"{config.agent_id}-health-01",
        "LOCAL_HEALTH_REPLAY_DB": "/var/lib/sang/local-health-replay.db",
        "ROS_DOMAIN_ID": config.ros_domain_id,
        "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST",
        "HEARTBEAT_INTERVAL_S": config.heartbeat_interval_s,
        "HEARTBEAT_TIMEOUT_S": config.heartbeat_timeout_s,
        "STARTUP_GRACE_S": config.startup_grace_s,
    }
    return "".join(f"{key}={_env_value(value)}\n" for key, value in values.items())


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def _write_identical_or_new(path: Path, data: bytes, mode: int) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise RuntimeDeploymentError(f"refusing to overwrite different runtime output: {path}")
        if mode == 0o600 and path.stat().st_mode & 0o077:
            raise RuntimeDeploymentError(f"existing private runtime output is too permissive: {path}")
        return
    _atomic_write(path, data, mode)


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _deployment_contract(item: ResolvedRuntime) -> dict[str, Any]:
    config = item.config
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "agent_id": config.agent_id,
        "agent_name": config.agent_name,
        "robot_id": config.robot_id,
        "runtime_kind": item.manifest.robot.runtime_kind,
        "processing_node_id": config.processing_node_id,
        "application_uri": f"urn:sang:agent:{config.agent_id}",
        "opcua_endpoint": item.opcua_endpoint,
        "opcua_server_certificate_ref": f"secret://opcua/pins/{config.agent_id}.pem",
        "preview_port": config.preview_port,
        "ros_domain_id": config.ros_domain_id,
        "manifest": config.manifest,
        "mapping": config.mapping,
        "mapping_digest": item.mapping_digest,
        "runtime_directory": config.runtime_directory,
        "data_directory": f"{config.runtime_directory}/data",
        "local_health_replay_db": f"{config.runtime_directory}/data/local-health-replay.db",
        "environment_file": f"config/{config.agent_id}.env",
        "compose_file": DEFAULT_COMPOSE_FILE,
        "connection_profile_ids": [
            f"{config.agent_id}-https",
            f"{config.agent_id}-opcua",
        ],
    }


def _allocation_contract(
    item: ResolvedRuntime,
    *,
    host_runtime_root: Path,
) -> RuntimeAllocation:
    config = item.config
    return RuntimeAllocation(
        agent_id=config.agent_id,
        robot_id=config.robot_id,
        processing_node_id=config.processing_node_id,
        endpoint_host=config.endpoint_host,
        opcua_port=config.opcua_port,
        preview_port=config.preview_port,
        ros_domain_id=config.ros_domain_id,
        runtime_directory=config.runtime_directory,
        generation_directory=str(
            _host_path(host_runtime_root, config.runtime_directory)
        ),
    )


def _validate_global_allocations(allocations: list[RuntimeAllocation]) -> None:
    unique_fields = (
        ("agent_id", [item.agent_id for item in allocations]),
        ("robot_id", [item.robot_id for item in allocations]),
        (
            "generation_directory",
            [item.generation_directory for item in allocations],
        ),
    )
    for label, values in unique_fields:
        duplicates = sorted({value for value in values if values.count(value) > 1})
        if duplicates:
            raise RuntimeDeploymentError(
                f"global runtime allocation has duplicate {label}: "
                + ", ".join(duplicates)
            )

    node_hosts: dict[str, str] = {}
    host_nodes: dict[str, str] = {}
    listeners: dict[tuple[str, int], str] = {}
    domains: dict[tuple[str, int], str] = {}
    for item in allocations:
        previous_host = node_hosts.get(item.processing_node_id)
        if previous_host is not None and previous_host != item.endpoint_host:
            raise RuntimeDeploymentError(
                f"global runtime allocation maps {item.processing_node_id} to both "
                f"{previous_host} and {item.endpoint_host}"
            )
        previous_node = host_nodes.get(item.endpoint_host)
        if previous_node is not None and previous_node != item.processing_node_id:
            raise RuntimeDeploymentError(
                f"global runtime allocation maps {item.endpoint_host} to both "
                f"{previous_node} and {item.processing_node_id}"
            )
        node_hosts[item.processing_node_id] = item.endpoint_host
        host_nodes[item.endpoint_host] = item.processing_node_id

        for port in (item.opcua_port, item.preview_port):
            key = (item.processing_node_id, port)
            previous = listeners.get(key)
            if previous is not None:
                raise RuntimeDeploymentError(
                    f"global runtime allocation reuses port {port} on "
                    f"{item.processing_node_id} for {previous} and {item.agent_id}"
                )
            listeners[key] = item.agent_id
        domain_key = (item.processing_node_id, item.ros_domain_id)
        previous = domains.get(domain_key)
        if previous is not None:
            raise RuntimeDeploymentError(
                f"global runtime allocation reuses ROS_DOMAIN_ID {item.ros_domain_id} "
                f"on {item.processing_node_id} for {previous} and {item.agent_id}"
            )
        domains[domain_key] = item.agent_id


def _merge_allocation_registry(
    path: Path,
    *,
    central_ingress_url: str,
    requested: list[RuntimeAllocation],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    existing_document = None
    existing_allocations: list[RuntimeAllocation] = []
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise TypeError("top level must be an object")
            raw_allocations = value["runtimes"]
            if (
                value.get("schema_version") != INVENTORY_SCHEMA_VERSION
                or value.get("central_ingress_url") != central_ingress_url
                or not isinstance(raw_allocations, list)
            ):
                raise ValueError("registry contract differs")
            existing_allocations = [
                RuntimeAllocation.model_validate(item) for item in raw_allocations
            ]
            existing_document = value
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeDeploymentError(
                f"existing global runtime allocation registry is invalid: {path}"
            ) from exc

    merged = {item.agent_id: item for item in existing_allocations}
    for item in requested:
        existing = merged.get(item.agent_id)
        if existing is not None and existing != item:
            mismatched = _contract_mismatches(
                existing.model_dump(mode="json"),
                item.model_dump(mode="json"),
            )
            raise RuntimeDeploymentError(
                f"global runtime allocation differs for {item.agent_id} in: "
                + ", ".join(mismatched)
            )
        merged[item.agent_id] = item
    allocations = sorted(merged.values(), key=lambda item: item.agent_id)
    _validate_global_allocations(allocations)
    return existing_document, {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "central_ingress_url": central_ingress_url,
        "runtimes": [item.model_dump(mode="json") for item in allocations],
    }


def _write_allocation_registry(
    path: Path,
    value: dict[str, Any],
    existing: dict[str, Any] | None,
) -> None:
    data = _json_bytes(value)
    if existing is not None and _json_bytes(existing) == data:
        return
    _atomic_write(path, data, 0o644)


def _contract_mismatches(existing: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    return sorted(
        key for key, value in expected.items() if existing.get(key) != value
    )


def _load_existing_index(
    path: Path,
    *,
    central_ingress_url: str,
    contracts: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError("top level must be an object")
        runtimes = value["runtimes"]
    except (KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeDeploymentError(f"existing deployment index is invalid: {path}") from exc
    if (
        value.get("schema_version") != INVENTORY_SCHEMA_VERSION
        or value.get("central_ingress_url") != central_ingress_url
        or not isinstance(runtimes, list)
    ):
        raise RuntimeDeploymentError("existing deployment index contract differs")
    seen = set()
    for existing in runtimes:
        if not isinstance(existing, dict) or not isinstance(existing.get("agent_id"), str):
            raise RuntimeDeploymentError("existing deployment index runtime is invalid")
        agent_id = existing["agent_id"]
        if agent_id in seen:
            raise RuntimeDeploymentError("existing deployment index has duplicate Agent IDs")
        seen.add(agent_id)
        expected = contracts.get(agent_id)
        if expected is None:
            raise RuntimeDeploymentError(
                f"inventory would remove existing runtime {agent_id}"
            )
        mismatched = _contract_mismatches(existing, expected)
        if mismatched:
            raise RuntimeDeploymentError(
                f"existing deployment index differs for {agent_id} in: "
                + ", ".join(mismatched)
            )
    return value


def _write_deployment_index(
    path: Path,
    value: dict[str, Any],
    existing: dict[str, Any] | None,
) -> None:
    data = _json_bytes(value)
    if existing is None:
        _write_identical_or_new(path, data, 0o644)
        return
    if _json_bytes(existing) == data:
        return
    existing_ids = {item["agent_id"] for item in existing["runtimes"]}
    updated = {item["agent_id"]: item for item in value["runtimes"]}
    if any(updated.get(item["agent_id"]) != item for item in existing["runtimes"]):
        raise RuntimeDeploymentError("refusing to alter an existing deployment index runtime")
    if not existing_ids.issubset(updated):
        raise RuntimeDeploymentError("refusing to remove a runtime from deployment index")
    _atomic_write(path, data, 0o644)


def _preflight_store(
    store: FleetStore,
    inventory: RuntimeInventory,
    resolved: tuple[ResolvedRuntime, ...],
    *,
    pki_root: Path,
) -> None:
    registrations = {
        item["agent_id"]: item for item in store.list_agent_registrations()
    }
    credentials = {
        item["credential_id"]: item for item in store.list_agent_credentials()
    }
    profiles = {
        item["profile_id"]: item for item in store.list_connection_profiles()
    }
    for item in resolved:
        config = item.config
        registration = registrations.get(config.agent_id)
        registration_expected = {
            "agent_id": config.agent_id,
            "display_name": config.agent_name,
            "identity_uri": f"urn:sang:agent:{config.agent_id}",
            "enabled": True,
        }
        if registration is not None:
            mismatched = _contract_mismatches(registration, registration_expected)
            if mismatched:
                raise RuntimeDeploymentError(
                    f"existing Agent registration differs for {config.agent_id} in: "
                    + ", ".join(mismatched)
                )

        credential_id = f"{config.agent_id}-https-01"
        credential = credentials.get(credential_id)
        if credential is not None:
            metadata_path = (
                pki_root
                / "roles/agents"
                / config.agent_id
                / "central/role-metadata.json"
            )
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                edge = metadata["edge"]
                credential_expected = {
                    "credential_id": credential_id,
                    "agent_id": config.agent_id,
                    "certificate_sha256": edge[
                        "https_client_certificate_sha256"
                    ],
                    "certificate_ref": "secret://https/client/chain.pem",
                    "not_before": isoformat_z(
                        parse_datetime(edge["https_client_not_before"])
                    ),
                    "not_after": isoformat_z(
                        parse_datetime(edge["https_client_not_after"])
                    ),
                    "status": "active",
                }
            except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeDeploymentError(
                    f"existing Agent credential has no matching PKI role for "
                    f"{config.agent_id}"
                ) from exc
            mismatched = _contract_mismatches(credential, credential_expected)
            if mismatched:
                raise RuntimeDeploymentError(
                    f"existing Agent credential differs for {config.agent_id} in: "
                    + ", ".join(mismatched)
                )

        expected_profiles = {
            f"{config.agent_id}-https": {
                "profile_id": f"{config.agent_id}-https",
                "owner_kind": "agent",
                "agent_id": config.agent_id,
                "asset_id": None,
                "protocol": "https_agent",
                "desired_endpoint": inventory.central.ingress_url,
                "expected_application_uri": None,
                "security_mode": "MutualTLS",
                "security_policy": "TLS1.2+",
                "client_certificate_ref": "secret://https/client/chain.pem",
                "client_private_key_ref": "secret://https/client/key.pem",
                "trust_list_ref": "secret://https/trust/ca-crl.pem",
                "crl_ref": "secret://https/trust/ca-crl.pem",
                "server_certificate_ref": None,
                "user_certificate_ref": None,
                "user_private_key_ref": None,
                "enabled": True,
            },
            f"{config.agent_id}-opcua": {
                "profile_id": f"{config.agent_id}-opcua",
                "owner_kind": "agent",
                "agent_id": config.agent_id,
                "asset_id": None,
                "protocol": "opcua",
                "desired_endpoint": item.opcua_endpoint,
                "expected_application_uri": f"urn:sang:agent:{config.agent_id}",
                "security_mode": "SignAndEncrypt",
                "security_policy": "Basic256Sha256",
                "client_certificate_ref": "secret://opcua/application/cert.pem",
                "client_private_key_ref": "secret://opcua/application/key.pem",
                "trust_list_ref": "secret://opcua/trust/servers",
                "crl_ref": "secret://opcua/crl",
                "server_certificate_ref": f"secret://opcua/pins/{config.agent_id}.pem",
                "user_certificate_ref": "secret://opcua/user/cert.pem",
                "user_private_key_ref": "secret://opcua/user/key.pem",
                "enabled": True,
            },
        }
        for profile_id, expected in expected_profiles.items():
            conflicting = [
                profile
                for profile in profiles.values()
                if profile["owner_kind"] == "agent"
                and profile["agent_id"] == config.agent_id
                and profile["protocol"] == expected["protocol"]
                and profile["profile_id"] != profile_id
            ]
            if conflicting:
                raise RuntimeDeploymentError(
                    f"existing {expected['protocol']} profile for {config.agent_id} "
                    f"uses unexpected ID {conflicting[0]['profile_id']}"
                )
            existing = profiles.get(profile_id)
            if existing is None:
                continue
            mismatched = _contract_mismatches(existing, expected)
            if mismatched:
                raise RuntimeDeploymentError(
                    f"existing profile {profile_id} differs in: "
                    + ", ".join(mismatched)
                )


def _generate_runtime_deployments_locked(
    inventory: RuntimeInventory,
    *,
    workspace_root: Path,
    runtime_root: Path,
    pki_root: Path,
    db_path: Path,
    host_workspace_root: Path | None = None,
    host_runtime_root: Path | None = None,
    host_pki_root: Path | None = None,
    image: str = "sang-fleet:jazzy",
    uid: int | None = None,
    gid: int | None = None,
) -> dict[str, Any]:
    workspace = workspace_root.expanduser().resolve(strict=True)
    runtime_base = runtime_root.expanduser().resolve(strict=False)
    pki = pki_root.expanduser().resolve(strict=True)
    host_workspace = (host_workspace_root or workspace).expanduser().resolve(strict=False)
    host_runtime = (host_runtime_root or runtime_base).expanduser().resolve(strict=False)
    host_pki = (host_pki_root or pki).expanduser().resolve(strict=False)
    resolved = resolve_runtime_inventory(
        inventory,
        workspace_root=workspace,
        runtime_root=runtime_base,
    )
    uid_value = os.getuid() if uid is None else uid
    gid_value = os.getgid() if gid is None else gid
    if uid_value < 0 or gid_value < 0:
        raise RuntimeDeploymentError("uid and gid must be non-negative")

    env_documents = {
        item.config.agent_id: _environment_text(
            item,
            inventory=inventory,
            host_workspace_root=host_workspace,
            host_runtime_root=host_runtime,
            host_pki_root=host_pki,
            image=image,
            uid=uid_value,
            gid=gid_value,
        ).encode("utf-8")
        for item in resolved
    }
    contracts = {
        item.config.agent_id: _deployment_contract(item) for item in resolved
    }
    allocation_path = pki / "roles/central/runtime-allocations.json"
    existing_allocations, allocation_registry = _merge_allocation_registry(
        allocation_path,
        central_ingress_url=inventory.central.ingress_url,
        requested=[
            _allocation_contract(item, host_runtime_root=host_runtime)
            for item in resolved
        ],
    )
    index_path = runtime_base / "deployment-index.json"
    existing_index = _load_existing_index(
        index_path,
        central_ingress_url=inventory.central.ingress_url,
        contracts=contracts,
    )
    for item in resolved:
        if item.env_path.exists() and item.env_path.read_bytes() != env_documents[item.config.agent_id]:
            raise RuntimeDeploymentError(
                f"refusing to overwrite different runtime output: {item.env_path}"
            )
        if item.deployment_path.exists():
            try:
                existing = json.loads(item.deployment_path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    raise TypeError("top level must be an object")
            except (OSError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeDeploymentError(
                    f"existing deployment record is invalid: {item.deployment_path}"
                ) from exc
            mismatched = _contract_mismatches(
                existing,
                contracts[item.config.agent_id],
            )
            if mismatched:
                raise RuntimeDeploymentError(
                    "existing deployment ownership differs in: "
                    + ", ".join(sorted(mismatched))
                )
        if item.runtime_path.exists() and not item.deployment_path.exists():
            entries = list(item.runtime_path.iterdir())
            if entries:
                raise RuntimeDeploymentError(
                    f"runtime directory is non-empty without deployment ownership: {item.runtime_path}"
                )

    try:
        store = FleetStore(db_path)
    except (RuntimeError, sqlite3.Error) as exc:
        raise RuntimeDeploymentError(
            f"cannot open or migrate Central Fleet DB: {exc}"
        ) from exc
    _preflight_store(store, inventory, resolved, pki_root=pki)
    _write_allocation_registry(
        allocation_path,
        allocation_registry,
        existing_allocations,
    )
    reports = []
    for item in resolved:
        config = item.config
        try:
            roles = provision_agent_runtime(
                pki,
                agent_id=config.agent_id,
                edge_host=config.endpoint_host,
            )
            https_metadata_path = (
                pki
                / "roles/agents"
                / config.agent_id
                / "central/role-metadata.json"
            )
            https_metadata = json.loads(https_metadata_path.read_text(encoding="utf-8"))
            enrollment = enroll_phase3_edge(
                store,
                metadata=https_metadata,
                display_name=config.agent_name,
                central_ingress_url=inventory.central.ingress_url,
                edge_opcua_endpoint=item.opcua_endpoint,
                opcua_server_certificate_ref=roles["server_certificate_ref"],
            )
        except (json.JSONDecodeError, OSError, PkiError, ValueError) as exc:
            raise RuntimeDeploymentError(
                f"cannot provision {config.agent_id}: {exc}"
            ) from exc

        item.data_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(item.data_path, 0o700)
        _write_identical_or_new(
            item.env_path,
            env_documents[config.agent_id],
            0o600,
        )
        profile_ids = sorted(profile["profile_id"] for profile in enrollment["connection_profiles"])
        report = {
            **contracts[config.agent_id],
            "opcua_server_certificate_sha256": roles["certificate_sha256"],
        }
        if report["application_uri"] != roles["application_uri"]:
            raise RuntimeDeploymentError("provisioned OPC UA ApplicationUri drifted")
        if report["opcua_server_certificate_ref"] != roles["server_certificate_ref"]:
            raise RuntimeDeploymentError("provisioned OPC UA server pin reference drifted")
        if sorted(report["connection_profile_ids"]) != profile_ids:
            raise RuntimeDeploymentError("enrolled connection profile IDs drifted")
        _write_identical_or_new(item.deployment_path, _json_bytes(report), 0o644)
        reports.append(report)

    index = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "central_ingress_url": inventory.central.ingress_url,
        "runtimes": reports,
    }
    _write_deployment_index(index_path, index, existing_index)
    return index


def generate_runtime_deployments(
    inventory: RuntimeInventory,
    *,
    workspace_root: Path,
    runtime_root: Path,
    pki_root: Path,
    db_path: Path,
    host_workspace_root: Path | None = None,
    host_runtime_root: Path | None = None,
    host_pki_root: Path | None = None,
    image: str = "sang-fleet:jazzy",
    uid: int | None = None,
    gid: int | None = None,
) -> dict[str, Any]:
    try:
        pki = pki_root.expanduser().resolve(strict=True)
        lock_path = pki / ".runtime-deployment.lock"
        with lock_path.open("a+b") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            return _generate_runtime_deployments_locked(
                inventory,
                workspace_root=workspace_root,
                runtime_root=runtime_root,
                pki_root=pki,
                db_path=db_path,
                host_workspace_root=host_workspace_root,
                host_runtime_root=host_runtime_root,
                host_pki_root=host_pki_root,
                image=image,
                uid=uid,
                gid=gid,
            )
    except RuntimeDeploymentError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeDeploymentError(f"runtime generation I/O failed: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or generate isolated SANG Phase 4 runtimes"
    )
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--pki-root", type=Path)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--host-workspace-root", type=Path)
    parser.add_argument("--host-runtime-root", type=Path)
    parser.add_argument("--host-pki-root", type=Path)
    parser.add_argument("--image", default="sang-fleet:jazzy")
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(args: list[str] | None = None) -> None:
    options = _parser().parse_args(args)
    try:
        inventory = load_runtime_inventory(options.inventory)
        resolved = resolve_runtime_inventory(
            inventory,
            workspace_root=options.workspace_root,
            runtime_root=options.runtime_root,
        )
        if options.validate_only:
            result = {
                "schema_version": INVENTORY_SCHEMA_VERSION,
                "valid": True,
                "runtimes": [
                    {
                        "agent_id": item.config.agent_id,
                        "robot_id": item.config.robot_id,
                        "runtime_kind": item.manifest.robot.runtime_kind,
                        "opcua_endpoint": item.opcua_endpoint,
                        "runtime_directory": item.config.runtime_directory,
                    }
                    for item in resolved
                ],
            }
        else:
            if options.pki_root is None or options.db_path is None:
                raise RuntimeDeploymentError(
                    "generation requires --pki-root and --db-path"
                )
            result = generate_runtime_deployments(
                inventory,
                workspace_root=options.workspace_root,
                runtime_root=options.runtime_root,
                pki_root=options.pki_root,
                db_path=options.db_path,
                host_workspace_root=options.host_workspace_root,
                host_runtime_root=options.host_runtime_root,
                host_pki_root=options.host_pki_root,
                image=options.image,
                uid=options.uid,
                gid=options.gid,
            )
    except RuntimeDeploymentError as exc:
        raise SystemExit(f"Phase 4 runtime deployment failed: {exc}") from exc
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
