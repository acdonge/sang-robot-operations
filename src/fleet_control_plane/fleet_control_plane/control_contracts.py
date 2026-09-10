"""Transport-neutral execution and lease contracts shared by ROS and simulators."""

from __future__ import annotations

import hashlib
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from fleet_control_plane.models import FleetModel, SafeId
from fleet_control_plane.orchestration_models import (
    CapabilityRef,
    ControlEnvelopeV1,
    ControlSubleaseV1,
    DelegationGrantV1,
    ExecuteSkillGoalV1,
    ExecutionMode,
    validate_control_sublease,
)
from fleet_control_plane.orchestration_store import canonical_json


ExecutionState = Literal[
    "ACCEPTED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "EXPIRED",
]


class ExecuteSkillRequestV1(FleetModel):
    """Authenticated runtime envelope around the frozen G0 execution contracts."""

    schema_version: Literal[1] = 1
    agent_id: SafeId
    delegation_grant: DelegationGrantV1
    control_sublease: ControlSubleaseV1
    control_envelope: ControlEnvelopeV1
    control_envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    goal: ExecuteSkillGoalV1
    goal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    goal_signature: str = Field(min_length=16, max_length=8192)

    @model_validator(mode="after")
    def request_contracts_are_coherent(self) -> Self:
        validate_control_sublease(
            self.delegation_grant,
            self.control_sublease,
            self.control_envelope,
            self.control_envelope_sha256,
        )
        grant = self.delegation_grant
        lease = self.control_sublease
        goal = self.goal
        expected = (
            lease.run_id,
            lease.command_id,
            lease.asset_id,
            lease.execution_mode,
            lease.authorization_id,
            lease.parent_grant_id,
            lease.lease_id,
            lease.epoch,
            lease.orchestrator_boot_id,
            lease.control_envelope_id,
            lease.control_envelope_sha256,
            grant.manifest_sha256,
        )
        actual = (
            goal.run_id,
            goal.command_id,
            goal.asset_id,
            goal.execution_mode,
            goal.authorization_id,
            goal.parent_grant_id,
            goal.lease_id,
            goal.lease_epoch,
            goal.orchestrator_boot_id,
            goal.control_envelope_id,
            goal.control_envelope_sha256,
            goal.manifest_sha256,
        )
        if actual != expected:
            raise ValueError("ExecuteSkill goal does not match its grant and sublease")
        if goal.capability not in lease.capabilities:
            raise ValueError("ExecuteSkill capability is absent from its sublease")
        if goal.not_valid_after > lease.expires_at:
            raise ValueError("ExecuteSkill validity exceeds its sublease")
        goal_digest = hashlib.sha256(
            canonical_json(goal.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()
        if self.goal_sha256 != goal_digest:
            raise ValueError("ExecuteSkill goal digest does not match its payload")
        return self

    @property
    def command_id(self) -> str:
        return str(self.goal.command_id)

    @property
    def run_id(self) -> str:
        return self.goal.run_id

    @property
    def step_id(self) -> str:
        return self.goal.step_instance_id

    @property
    def capability_ref(self) -> str:
        return self.goal.capability

    @property
    def execution_mode(self) -> str:
        return self.goal.execution_mode

    @property
    def parameters(self) -> dict[str, JsonValue]:
        return self.goal.parameters


class ExecutionRecordV1(FleetModel):
    schema_version: Literal[1] = 1
    command_id: SafeId
    run_id: SafeId
    step_id: SafeId
    agent_id: SafeId
    agent_boot_id: UUID
    capability_ref: CapabilityRef
    execution_mode: ExecutionMode
    lease_epoch: int = Field(ge=1)
    state: ExecutionState
    accepted_at: AwareDatetime
    updated_at: AwareDatetime
    result: dict[str, JsonValue] = Field(default_factory=dict, max_length=256)
    evidence_ref: str | None = Field(default=None, min_length=3, max_length=1024)
    error_code: SafeId | None = None
    error_message: str | None = Field(default=None, min_length=1, max_length=1024)
    command_sink_present: Literal[False] = False


class CancelExecutionRequestV1(FleetModel):
    schema_version: Literal[1] = 1
    command_id: UUID
    run_id: SafeId
    agent_id: SafeId
    reason: str = Field(min_length=1, max_length=512)
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    signing_key_id: SafeId
    signature: str = Field(min_length=16, max_length=8192)

    @model_validator(mode="after")
    def validity_window_is_positive(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("CancelExecution expiry must follow its issue time")
        return self
