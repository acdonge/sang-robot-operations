"""Canonical ROS JSON transport codecs for signed control contracts."""

from __future__ import annotations

import hashlib
from typing import TypeVar

from pydantic import BaseModel

from fleet_control_plane.control_contracts import (
    CancelExecutionRequestV1,
    ExecuteSkillRequestV1,
)
from fleet_control_plane.demand_arbiter import SignedPolicyDemandV1
from fleet_control_plane.orchestration_store import canonical_json


class TransportIntegrityError(ValueError):
    pass


ContractT = TypeVar("ContractT", bound=BaseModel)


def encode_transport_contract(contract: BaseModel) -> tuple[str, str]:
    encoded = canonical_json(contract.model_dump(mode="json"))
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decode_contract(
    model_type: type[ContractT],
    encoded: str,
    expected_sha256: str,
) -> ContractT:
    actual_sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    if actual_sha256 != expected_sha256:
        raise TransportIntegrityError("transport contract digest does not match")
    return model_type.model_validate_json(encoded)


def decode_execute_skill_request(
    encoded: str,
    expected_sha256: str,
    *,
    agent_id: str,
    command_id: str,
    run_id: str,
    step_id: str,
) -> ExecuteSkillRequestV1:
    request = _decode_contract(ExecuteSkillRequestV1, encoded, expected_sha256)
    if (
        request.agent_id,
        request.command_id,
        request.run_id,
        request.step_id,
    ) != (agent_id, command_id, run_id, step_id):
        raise TransportIntegrityError("ExecuteSkill ROS header differs from its signed contract")
    return request


def decode_renewal_request(
    encoded: str,
    expected_sha256: str,
    *,
    command_id: str,
) -> ExecuteSkillRequestV1:
    request = _decode_contract(ExecuteSkillRequestV1, encoded, expected_sha256)
    if request.command_id != command_id:
        raise TransportIntegrityError("RenewLease command differs from its signed contract")
    return request


def decode_cancel_request(
    encoded: str,
    expected_sha256: str,
    *,
    command_id: str,
) -> CancelExecutionRequestV1:
    request = _decode_contract(CancelExecutionRequestV1, encoded, expected_sha256)
    if str(request.command_id) != command_id:
        raise TransportIntegrityError("CancelExecution command differs from its signed contract")
    return request


def decode_policy_demand(
    encoded: str,
    expected_sha256: str,
    *,
    command_id: str,
    session_id: str,
    run_id: str,
    sequence: int,
    active_request: ExecuteSkillRequestV1,
) -> SignedPolicyDemandV1:
    signed = _decode_contract(SignedPolicyDemandV1, encoded, expected_sha256)
    demand = signed.demand
    if str(demand.command_id) != active_request.command_id:
        raise TransportIntegrityError("PolicyDemand command differs from its active request")
    if str(demand.policy_session_id) != session_id or demand.sequence != sequence:
        raise TransportIntegrityError("PolicyDemand ROS header differs from its signed contract")
    if command_id != active_request.command_id or run_id != active_request.run_id:
        raise TransportIntegrityError("PolicyDemand ROS header differs from its signed contract")
    return signed
