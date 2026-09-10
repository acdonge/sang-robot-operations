"""Independent fail-closed PolicyDemand validation with an output-disconnected sink."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Literal
from uuid import UUID

from pydantic import Field

from fleet_control_plane.control_authority import ControlAuthority
from fleet_control_plane.control_contracts import ExecuteSkillRequestV1
from fleet_control_plane.control_signing import VerificationKeyRing
from fleet_control_plane.models import FleetModel, SafeId
from fleet_control_plane.orchestration_models import PolicyDemandV1
from fleet_control_plane.orchestration_store import canonical_json
from fleet_control_plane.status import isoformat_z, parse_datetime


DEMAND_ARBITER_MIGRATION = """
CREATE TABLE IF NOT EXISTS demand_arbiter_state (
    policy_session_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL,
    max_sequence INTEGER NOT NULL CHECK (max_sequence >= 0),
    sampled_at TEXT NOT NULL,
    demand_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class SignedPolicyDemandV1(FleetModel):
    schema_version: Literal[1] = 1
    demand: PolicyDemandV1
    signing_key_id: SafeId
    signature: str = Field(min_length=16, max_length=8192)


class DemandAuthorizationContextV1(FleetModel):
    schema_version: Literal[1] = 1
    expected_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_frame_id: SafeId
    expected_units: dict[SafeId, SafeId] = Field(min_length=1, max_length=64)
    xr_session_id: UUID | None = None
    xr_principal_id: SafeId | None = None


class DemandRejected(ValueError):
    pass


class OutputDisconnectedDemandSink:
    def __init__(self) -> None:
        self.command_sink_present = False
        self.accepted: list[dict] = []

    def accept(self, demand: PolicyDemandV1) -> dict:
        evidence = {
            "policy_session_id": str(demand.policy_session_id),
            "sequence": demand.sequence,
            "command_sink_present": False,
        }
        self.accepted.append(evidence)
        return evidence


class DemandArbiter:
    def __init__(
        self,
        database_path: str | Path,
        *,
        keyring: VerificationKeyRing,
        sink: OutputDisconnectedDemandSink,
        authority: ControlAuthority | None = None,
    ) -> None:
        if sink.command_sink_present:
            raise ValueError("G5 arbiter requires an output-disconnected sink")
        self.path = Path(database_path)
        self.keyring = keyring
        self.sink = sink
        self.authority = authority
        with self._connect() as connection:
            connection.executescript(DEMAND_ARBITER_MIGRATION)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def accept(
        self,
        signed: SignedPolicyDemandV1,
        *,
        request: ExecuteSkillRequestV1,
        context: DemandAuthorizationContextV1,
        received_at: datetime,
        measured_clock_uncertainty_ms: float | None,
    ) -> dict:
        now = parse_datetime(received_at)
        demand = signed.demand
        lease = request.control_sublease
        envelope = request.control_envelope
        if signed.signing_key_id != lease.issuer_key_id:
            raise DemandRejected("PolicyDemand signer differs from the sublease issuer")
        self.keyring.verify_payload(
            key_id=signed.signing_key_id,
            signature_text=signed.signature,
            payload=demand,
            at=now,
        )
        if (
            demand.command_id != request.goal.command_id
            or demand.lease_id != lease.lease_id
            or demand.lease_epoch != lease.epoch
        ):
            raise DemandRejected("PolicyDemand does not match the active command lease")
        if now >= parse_datetime(lease.expires_at) or now >= parse_datetime(
            request.goal.not_valid_after
        ):
            raise DemandRejected("PolicyDemand lease or goal has expired")
        if demand.policy_sha256 != context.expected_policy_sha256:
            raise DemandRejected("PolicyDemand policy digest does not match authorization")
        if demand.frame_id != context.expected_frame_id or demand.units != context.expected_units:
            raise DemandRejected("PolicyDemand frame or units do not match authorization")
        sampled = parse_datetime(demand.sampled_at)
        age_ms = (now - sampled).total_seconds() * 1000.0
        if age_ms < 0 or age_ms > envelope.max_demand_age_ms:
            raise DemandRejected("PolicyDemand is stale or future-dated")
        if measured_clock_uncertainty_ms is None:
            raise DemandRejected("trusted clock uncertainty is missing")
        if measured_clock_uncertainty_ms > envelope.max_clock_uncertainty_ms:
            raise DemandRejected("trusted clock uncertainty exceeds the envelope")
        if context.xr_session_id is not None:
            if self.authority is None or context.xr_principal_id is None:
                raise DemandRejected("XR intervention context is incomplete")
            try:
                self.authority.require_active_xr_session(
                    context.xr_session_id,
                    principal_id=context.xr_principal_id,
                    run_id=request.run_id,
                    now=now,
                )
            except ValueError as exc:
                raise DemandRejected(str(exc)) from exc

        numeric: dict[str, float] = {}
        for axis, value in demand.demand.items():
            if axis not in envelope.max_delta_by_axis:
                raise DemandRejected(f"PolicyDemand axis is outside the envelope: {axis}")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DemandRejected(f"PolicyDemand axis is not numeric: {axis}")
            numeric[axis] = float(value)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM demand_arbiter_state WHERE policy_session_id = ?",
                (str(demand.policy_session_id),),
            ).fetchone()
            if previous is not None:
                if previous["command_id"] != str(demand.command_id):
                    raise DemandRejected("PolicyDemand session changed command identity")
                if demand.sequence <= previous["max_sequence"]:
                    raise DemandRejected("PolicyDemand sequence regression or duplicate")
                interval = sampled - parse_datetime(previous["sampled_at"])
                minimum = timedelta(seconds=1.0 / envelope.max_rate_hz)
                if interval < minimum:
                    raise DemandRejected("PolicyDemand exceeds the envelope maximum rate")
                previous_values = json.loads(previous["demand_json"])
                for axis, value in numeric.items():
                    prior = previous_values.get(axis)
                    if prior is None:
                        raise DemandRejected("PolicyDemand added an axis mid-session")
                    if abs(value - float(prior)) > envelope.max_delta_by_axis[axis]:
                        raise DemandRejected(
                            f"PolicyDemand delta exceeds the envelope for {axis}"
                        )
            evidence = self.sink.accept(demand)
            connection.execute(
                """
                INSERT INTO demand_arbiter_state (
                    policy_session_id, command_id, max_sequence, sampled_at,
                    demand_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(policy_session_id) DO UPDATE SET
                    max_sequence = excluded.max_sequence,
                    sampled_at = excluded.sampled_at,
                    demand_json = excluded.demand_json,
                    updated_at = excluded.updated_at
                """,
                (
                    str(demand.policy_session_id),
                    str(demand.command_id),
                    demand.sequence,
                    isoformat_z(sampled),
                    canonical_json(numeric),
                    isoformat_z(now),
                ),
            )
        return {
            "accepted": True,
            "sequence": demand.sequence,
            "evidence": evidence,
            "command_sink_present": False,
        }

