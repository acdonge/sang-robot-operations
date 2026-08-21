"""Validated configuration contracts for task-scoped fleet views."""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from fleet_control_plane.models import FleetModel, RuntimeKind, SafeId, ShortText


AssetType = Literal[
    "robot",
    "camera",
    "eef",
    "welder",
    "feeder",
    "mobile_robot",
    "sensor",
    "other",
]
AccessMode = Literal["exclusive", "shared_read"]
RunState = Literal["DRAFT", "READY"]
CertificateFingerprint = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$", strip_whitespace=True),
]
SecretReference = Annotated[
    str,
    StringConstraints(
        min_length=10,
        max_length=512,
        pattern=r"^secret://[A-Za-z0-9][A-Za-z0-9._/-]*$",
        strip_whitespace=True,
    ),
]
Endpoint = Annotated[str, StringConstraints(min_length=1, max_length=2048, strip_whitespace=True)]


def _validate_secret_reference(value: str) -> str:
    path = value.removeprefix("secret://")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("secret references must use canonical path components")
    return value


class AgentRegistrationCreate(FleetModel):
    agent_id: SafeId
    display_name: ShortText
    identity_uri: Annotated[
        str,
        StringConstraints(min_length=1, max_length=512, strip_whitespace=True),
    ] | None = None
    enabled: bool = True

    @field_validator("identity_uri")
    @classmethod
    def identity_uri_matches_agent(cls, value: str | None, info) -> str | None:
        if value is None:
            return value
        agent_id = info.data.get("agent_id")
        if agent_id is not None and value != f"urn:sang:agent:{agent_id}":
            raise ValueError("identity_uri must be urn:sang:agent:<agent_id>")
        return value


class AgentRegistrationUpdate(FleetModel):
    expected_revision: int = Field(ge=1)
    display_name: ShortText
    enabled: bool


class AgentCredentialCreate(FleetModel):
    credential_id: SafeId
    agent_id: SafeId
    certificate_sha256: CertificateFingerprint
    certificate_ref: SecretReference
    not_before: AwareDatetime
    not_after: AwareDatetime

    @field_validator("certificate_ref")
    @classmethod
    def certificate_ref_is_canonical(cls, value: str) -> str:
        return _validate_secret_reference(value)

    @model_validator(mode="after")
    def validity_window_is_ordered(self) -> "AgentCredentialCreate":
        if self.not_after <= self.not_before:
            raise ValueError("not_after must be later than not_before")
        return self


class CredentialRevoke(FleetModel):
    reason: Annotated[
        str,
        StringConstraints(min_length=1, max_length=512, strip_whitespace=True),
    ]


class ConnectionProfileSpec(FleetModel):
    protocol: Literal["opcua", "https_agent"]
    desired_endpoint: Endpoint
    expected_application_uri: Annotated[
        str,
        StringConstraints(min_length=1, max_length=512, strip_whitespace=True),
    ] | None = None
    security_mode: Literal["SignAndEncrypt", "MutualTLS"]
    security_policy: Literal["Basic256Sha256", "TLS1.2+"]
    client_certificate_ref: SecretReference
    client_private_key_ref: SecretReference
    trust_list_ref: SecretReference
    crl_ref: SecretReference
    server_certificate_ref: SecretReference | None = None
    user_certificate_ref: SecretReference | None = None
    user_private_key_ref: SecretReference | None = None
    enabled: bool = True

    @field_validator(
        "client_certificate_ref",
        "client_private_key_ref",
        "trust_list_ref",
        "crl_ref",
        "server_certificate_ref",
        "user_certificate_ref",
        "user_private_key_ref",
    )
    @classmethod
    def secret_refs_are_canonical(cls, value: str | None) -> str | None:
        return None if value is None else _validate_secret_reference(value)

    @model_validator(mode="after")
    def protocol_security_is_consistent(self) -> "ConnectionProfileSpec":
        parsed = urlsplit(self.desired_endpoint)
        if parsed.hostname is None or parsed.port is None or not 1 <= parsed.port <= 65535:
            raise ValueError("desired_endpoint must include a host and valid port")
        if self.protocol == "opcua":
            if parsed.scheme != "opc.tcp":
                raise ValueError("OPC UA profiles require an opc.tcp endpoint")
            if self.security_mode != "SignAndEncrypt" or self.security_policy != "Basic256Sha256":
                raise ValueError("OPC UA v1 requires Basic256Sha256 SignAndEncrypt")
            if self.expected_application_uri is None:
                raise ValueError("OPC UA profiles require expected_application_uri")
            if self.user_certificate_ref is None or self.user_private_key_ref is None:
                raise ValueError("OPC UA profiles require X.509 user credential references")
            if self.server_certificate_ref is None:
                self.server_certificate_ref = "secret://opcua/pins/edge-cert.pem"
        else:
            if parsed.scheme != "https":
                raise ValueError("Agent ingress profiles require an https endpoint")
            if self.security_mode != "MutualTLS" or self.security_policy != "TLS1.2+":
                raise ValueError("Agent ingress profiles require MutualTLS with TLS1.2+")
            if self.user_certificate_ref is not None or self.user_private_key_ref is not None:
                raise ValueError("Agent ingress profiles do not use OPC UA user credentials")
            if self.server_certificate_ref is not None:
                raise ValueError("Agent ingress profiles do not use an OPC UA server pin")
            if self.trust_list_ref != self.crl_ref:
                raise ValueError(
                    "Agent ingress uses one combined CA and CRL bundle reference"
                )
        return self


class ConnectionProfileCreate(ConnectionProfileSpec):
    profile_id: SafeId
    owner_kind: Literal["agent", "asset"]
    agent_id: SafeId | None = None
    asset_id: SafeId | None = None

    @model_validator(mode="after")
    def owner_is_unambiguous(self) -> "ConnectionProfileCreate":
        if self.owner_kind == "agent" and (self.agent_id is None or self.asset_id is not None):
            raise ValueError("agent profiles require only agent_id")
        if self.owner_kind == "asset" and (self.asset_id is None or self.agent_id is not None):
            raise ValueError("asset profiles require only asset_id")
        return self


class ConnectionProfileReplace(ConnectionProfileSpec):
    expected_revision: int = Field(ge=1)


class ProcessingNodeCreate(FleetModel):
    node_id: SafeId
    display_name: ShortText
    agent_ids: list[SafeId] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def agent_ids_are_unique(self) -> "ProcessingNodeCreate":
        if len(self.agent_ids) != len(set(self.agent_ids)):
            raise ValueError("agent_ids must be unique")
        return self


class ObservedBinding(FleetModel):
    agent_id: SafeId
    robot_id: SafeId
    source_key: ShortText | None = None


class AssetCreate(FleetModel):
    asset_id: SafeId
    asset_type: AssetType
    display_name: ShortText
    runtime_kind: RuntimeKind | None = None
    node_id: SafeId | None = None
    binding: ObservedBinding | None = None
    capabilities: list[SafeId] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def binding_matches_asset_type(self) -> "AssetCreate":
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("capabilities must be unique")
        if self.binding is None:
            return self
        if self.node_id is None:
            raise ValueError("observed bindings require node_id")
        if self.asset_type not in {"robot", "camera"}:
            raise ValueError("only robot and camera assets may use observed bindings")
        if self.asset_type == "camera" and self.binding.source_key is None:
            raise ValueError("camera bindings require source_key")
        if self.asset_type == "robot" and self.binding.source_key is not None:
            raise ValueError("robot bindings must not include source_key")
        return self


class AssetRuntimeKindUpdate(FleetModel):
    runtime_kind: RuntimeKind | None


class WorkcellCreate(FleetModel):
    workcell_id: SafeId
    display_name: ShortText
    description: str | None = Field(default=None, max_length=2048)
    asset_ids: list[SafeId] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def asset_ids_are_unique(self) -> "WorkcellCreate":
        if len(self.asset_ids) != len(set(self.asset_ids)):
            raise ValueError("asset_ids must be unique")
        return self


class TaskRoleCreate(FleetModel):
    role: SafeId
    asset_type: AssetType
    access_mode: AccessMode = "exclusive"


class TaskDefinitionCreate(FleetModel):
    task_definition_id: SafeId
    version: int = Field(ge=1)
    display_name: ShortText
    description: str | None = Field(default=None, max_length=4096)
    roles: list[TaskRoleCreate] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def roles_are_unique(self) -> "TaskDefinitionCreate":
        role_names = [role.role for role in self.roles]
        if len(role_names) != len(set(role_names)):
            raise ValueError("task role names must be unique")
        return self


class AssignmentCreate(FleetModel):
    role: SafeId
    asset_id: SafeId
    access_mode: AccessMode


class TaskRunCreate(FleetModel):
    run_id: SafeId
    task_definition_id: SafeId
    task_version: int = Field(ge=1)
    workcell_id: SafeId | None = None
    display_name: ShortText
    state: RunState = "DRAFT"
    assignments: list[AssignmentCreate] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def assignments_are_unique(self) -> "TaskRunCreate":
        roles = [assignment.role for assignment in self.assignments]
        assets = [assignment.asset_id for assignment in self.assignments]
        if len(roles) != len(set(roles)):
            raise ValueError("assignment roles must be unique")
        if len(assets) != len(set(assets)):
            raise ValueError("an asset may only fill one role in a task run")
        return self
