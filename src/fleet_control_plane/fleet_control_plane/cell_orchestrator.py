"""Restart-safe local DAG Orchestrator for simulation and disconnected HIL."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import Field

from fleet_control_plane.agent_execution import SimulationAgent
from fleet_control_plane.control_contracts import (
    ExecuteSkillRequestV1,
)
from fleet_control_plane.control_signing import SigningKey
from fleet_control_plane.models import FleetModel, SafeId
from fleet_control_plane.orchestration_models import (
    ActionStepV2,
    CompensationStepV2,
    ControlEnvelopeV1,
    ControlSubleaseV1,
    DelegationGrantV1,
    ExecuteSkillGoalV1,
    ExecutionMode,
    RegistryRefV1,
    WorkflowDefinitionV2,
)
from fleet_control_plane.orchestration_store import canonical_json
from fleet_control_plane.status import isoformat_z, parse_datetime


ORCHESTRATOR_MIGRATION = """
CREATE TABLE IF NOT EXISTS cell_orchestrator_runs (
    run_id TEXT PRIMARY KEY,
    workflow_json TEXT NOT NULL,
    role_targets_json TEXT NOT NULL,
    execution_mode TEXT NOT NULL CHECK (execution_mode IN ('SIMULATION', 'REPLAY', 'HIL')),
    state TEXT NOT NULL CHECK (
        state IN ('DRAFT', 'ACTIVE', 'RECONCILING', 'HELD', 'COMPLETE', 'FAILED')
    ),
    lease_epoch INTEGER NOT NULL CHECK (lease_epoch >= 1),
    owner_boot_id TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cell_orchestrator_steps (
    run_id TEXT NOT NULL REFERENCES cell_orchestrator_runs(run_id) ON DELETE RESTRICT,
    step_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('PENDING', 'DISPATCHED', 'SUCCEEDED', 'FAILED', 'HELD')
    ),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, step_id)
);

CREATE TABLE IF NOT EXISTS cell_orchestrator_commands (
    command_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    last_state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, step_id, asset_id)
);
"""


RunState = Literal["DRAFT", "ACTIVE", "RECONCILING", "HELD", "COMPLETE", "FAILED"]


class AgentTargetV1(FleetModel):
    asset_id: SafeId
    agent_id: SafeId
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_id: SafeId
    control_envelope: ControlEnvelopeV1
    control_envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_isolation_evidence_ref: RegistryRefV1 | None = None


class OrchestratorRunCreateV1(FleetModel):
    run_id: SafeId
    workflow: WorkflowDefinitionV2
    execution_mode: ExecutionMode
    role_targets: dict[SafeId, list[AgentTargetV1]] = Field(min_length=1, max_length=128)


class LostReply(ConnectionError):
    """The Agent may have accepted the request but its reply was not observed."""


class SimulatedProcessCrash(BaseException):
    """Test-only abrupt termination after the Agent accepts a durable command intent."""


class SimulationTransport:
    def __init__(self, agents: dict[str, SimulationAgent]) -> None:
        self.agents = dict(agents)
        self.drop_next_reply_for: set[str] = set()
        self.crash_after_accept_for: set[str] = set()
        self.dispatch_count: dict[str, int] = {}
        self.dispatch_order: list[str] = []

    def execute(self, request: ExecuteSkillRequestV1, *, now: datetime):
        agent = self.agents.get(request.agent_id)
        if agent is None:
            raise ConnectionError(f"simulation Agent is unavailable: {request.agent_id}")
        result = agent.execute(request, now=now)
        self.dispatch_count[request.command_id] = self.dispatch_count.get(request.command_id, 0) + 1
        self.dispatch_order.append(request.command_id)
        if request.command_id in self.crash_after_accept_for:
            self.crash_after_accept_for.remove(request.command_id)
            raise SimulatedProcessCrash(
                f"process terminated after Agent accepted {request.command_id}"
            )
        if request.command_id in self.drop_next_reply_for:
            self.drop_next_reply_for.remove(request.command_id)
            raise LostReply(f"reply dropped after Agent accepted {request.command_id}")
        return result

    def query(self, agent_id: str, command_id: str):
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ConnectionError(f"simulation Agent is unavailable: {agent_id}")
        recover = getattr(agent, "recover", None)
        return recover(command_id) if callable(recover) else agent.query(command_id)


class CellOrchestrator:
    def __init__(
        self,
        database_path: str | Path,
        *,
        orchestrator_id: str,
        authority_signer: SigningKey,
        delegated_signer: SigningKey,
        transport: SimulationTransport,
        boot_id: UUID | None = None,
        lease_ttl: timedelta = timedelta(seconds=30),
        lease_renewal_margin: timedelta = timedelta(seconds=10),
    ) -> None:
        if lease_renewal_margin <= timedelta(0) or lease_renewal_margin >= lease_ttl:
            raise ValueError("lease renewal margin must be positive and below the TTL")
        self.path = Path(database_path)
        self.orchestrator_id = orchestrator_id
        self.boot_id = boot_id or uuid4()
        self.authority_signer = authority_signer
        self.delegated_signer = delegated_signer
        self.transport = transport
        self.lease_ttl = lease_ttl
        self.lease_renewal_margin = lease_renewal_margin
        with self._connect() as connection:
            connection.executescript(ORCHESTRATOR_MIGRATION)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _command_id(run_id: str, step_id: str, asset_id: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"sang:{run_id}:{step_id}:{asset_id}"))

    def create_run(self, body: OrchestratorRunCreateV1, *, now: datetime) -> dict:
        if body.execution_mode not in {"SIMULATION", "REPLAY", "HIL"}:
            raise ValueError("local Orchestrator is output-disconnected before G7")
        workflow_roles = {role.role for role in body.workflow.roles}
        if set(body.role_targets) != workflow_roles:
            raise ValueError("role targets must exactly match workflow roles")
        if any(not targets for targets in body.role_targets.values()):
            raise ValueError("every workflow role requires at least one Agent target")
        timestamp = isoformat_z(now)
        workflow_json = canonical_json(body.workflow.model_dump(mode="json"))
        targets_json = canonical_json(
            {
                role: [target.model_dump(mode="json") for target in targets]
                for role, targets in body.role_targets.items()
            }
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM cell_orchestrator_runs WHERE run_id = ?",
                (body.run_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["workflow_json"] != workflow_json
                    or existing["role_targets_json"] != targets_json
                    or existing["execution_mode"] != body.execution_mode
                ):
                    raise ValueError("orchestration run identity is immutable")
                return self.get_run(body.run_id) or {}
            connection.execute(
                """
                INSERT INTO cell_orchestrator_runs (
                    run_id, workflow_json, role_targets_json, execution_mode,
                    state, lease_epoch, owner_boot_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'DRAFT', 1, ?, ?, ?)
                """,
                (
                    body.run_id,
                    workflow_json,
                    targets_json,
                    body.execution_mode,
                    str(self.boot_id),
                    timestamp,
                    timestamp,
                ),
            )
            connection.executemany(
                """
                INSERT INTO cell_orchestrator_steps (run_id, step_id, state, updated_at)
                VALUES (?, ?, 'PENDING', ?)
                """,
                [(body.run_id, step.step_id, timestamp) for step in body.workflow.steps],
            )
        return self.get_run(body.run_id) or {}

    def _set_run_state(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        state: RunState,
        now: datetime,
        reason: str | None = None,
    ) -> None:
        connection.execute(
            """
            UPDATE cell_orchestrator_runs SET state = ?, reason = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (state, reason, isoformat_z(now), run_id),
        )

    def _set_step_state(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        step_id: str,
        state: str,
        now: datetime,
    ) -> None:
        connection.execute(
            """
            UPDATE cell_orchestrator_steps SET state = ?, updated_at = ?
            WHERE run_id = ? AND step_id = ?
            """,
            (state, isoformat_z(now), run_id, step_id),
        )

    def _request_for(
        self,
        *,
        run: sqlite3.Row,
        step: ActionStepV2 | CompensationStepV2,
        target: AgentTargetV1,
        now: datetime,
    ) -> ExecuteSkillRequestV1:
        command_id = UUID(self._command_id(run["run_id"], step.step_id, target.asset_id))
        grant_id = uuid5(
            NAMESPACE_URL,
            f"sang:grant:{run['run_id']}:{target.asset_id}:{run['lease_epoch']}",
        )
        lease_id = uuid5(NAMESPACE_URL, f"sang:lease:{command_id}")
        expires_at = now + self.lease_ttl
        grant_expires_at = now + (self.lease_ttl * 10)
        unsigned_grant = DelegationGrantV1(
            grant_id=grant_id,
            run_id=run["run_id"],
            orchestrator_id=self.orchestrator_id,
            orchestrator_boot_id=self.boot_id,
            asset_id=target.asset_id,
            execution_mode=run["execution_mode"],
            authorization_id=target.authorization_id,
            manifest_sha256=target.manifest_sha256,
            control_envelope_id=target.control_envelope.envelope_id,
            control_envelope_sha256=target.control_envelope_sha256,
            epoch=run["lease_epoch"],
            capabilities=[step.capability],
            delegated_key_id=self.delegated_signer.key_id,
            delegated_public_key_ref=f"key://{self.delegated_signer.key_id}",
            issued_at=now,
            expires_at=grant_expires_at,
            renewable_until=grant_expires_at,
            max_sublease_ttl_ms=int(self.lease_ttl.total_seconds() * 1000),
            output_isolation_evidence_ref=target.output_isolation_evidence_ref,
            issuer_key_id=self.authority_signer.key_id,
            signature="unsigned-grant-0",
        )
        grant = self.authority_signer.sign_contract(unsigned_grant)
        unsigned_sublease = ControlSubleaseV1(
            lease_id=lease_id,
            lease_kind="auto",
            parent_grant_id=grant_id,
            run_id=run["run_id"],
            orchestrator_id=self.orchestrator_id,
            orchestrator_boot_id=self.boot_id,
            command_id=command_id,
            subject={"kind": "service", "principal_id": self.orchestrator_id},
            asset_id=target.asset_id,
            capabilities=[step.capability],
            execution_mode=run["execution_mode"],
            authorization_id=target.authorization_id,
            control_envelope_id=target.control_envelope.envelope_id,
            control_envelope_sha256=target.control_envelope_sha256,
            epoch=run["lease_epoch"],
            issued_at=now,
            expires_at=expires_at,
            renewable_until=grant_expires_at,
            output_isolation_evidence_ref=target.output_isolation_evidence_ref,
            issuer_key_id=self.delegated_signer.key_id,
            signature="unsigned-lease-0",
        )
        sublease = self.delegated_signer.sign_contract(unsigned_sublease)
        parameters = dict(step.parameters)
        parameters["bound_asset_id"] = target.asset_id
        goal = ExecuteSkillGoalV1(
            command_id=command_id,
            run_id=run["run_id"],
            step_instance_id=step.step_id,
            attempt=1,
            asset_id=target.asset_id,
            capability=step.capability,
            parameters=parameters,
            control_envelope_id=target.control_envelope.envelope_id,
            control_envelope_sha256=target.control_envelope_sha256,
            authorization_id=target.authorization_id,
            parent_grant_id=grant_id,
            lease_id=lease_id,
            lease_epoch=run["lease_epoch"],
            orchestrator_boot_id=self.boot_id,
            execution_mode=run["execution_mode"],
            manifest_sha256=target.manifest_sha256,
            not_valid_after=expires_at,
        )
        return ExecuteSkillRequestV1(
            agent_id=target.agent_id,
            delegation_grant=grant,
            control_sublease=sublease,
            control_envelope=target.control_envelope,
            control_envelope_sha256=target.control_envelope_sha256,
            goal=goal,
            goal_sha256=hashlib.sha256(
                canonical_json(goal.model_dump(mode="json")).encode("utf-8")
            ).hexdigest(),
            goal_signature=self.delegated_signer.sign_payload(goal),
        )

    def tick(self, run_id: str, *, now: datetime) -> dict:
        current = parse_datetime(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM cell_orchestrator_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"unknown orchestration run: {run_id}")
            if (
                run["owner_boot_id"] != str(self.boot_id)
                and run["state"] not in {"HELD", "COMPLETE", "FAILED"}
            ):
                connection.execute(
                    """
                    UPDATE cell_orchestrator_runs
                    SET owner_boot_id = ?, lease_epoch = lease_epoch + 1,
                        state = 'RECONCILING', reason = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        str(self.boot_id),
                        "Orchestrator incarnation changed; query before dispatch",
                        isoformat_z(current),
                        run_id,
                    ),
                )
                run = connection.execute(
                    "SELECT * FROM cell_orchestrator_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            if run["state"] == "RECONCILING":
                return self._reconcile(connection, run, current)
            if run["state"] in {"HELD", "COMPLETE", "FAILED"}:
                return self._decode_run(connection, run)
            if run["state"] == "DRAFT":
                self._set_run_state(connection, run_id, "ACTIVE", current)
                run = connection.execute(
                    "SELECT * FROM cell_orchestrator_runs WHERE run_id = ?", (run_id,)
                ).fetchone()

            workflow = WorkflowDefinitionV2.model_validate_json(run["workflow_json"])
            targets = {
                role: [AgentTargetV1.model_validate(item) for item in items]
                for role, items in json.loads(run["role_targets_json"]).items()
            }
            states = {
                row["step_id"]: row["state"]
                for row in connection.execute(
                    "SELECT * FROM cell_orchestrator_steps WHERE run_id = ?", (run_id,)
                ).fetchall()
            }
            progressed = False
            for step in workflow.steps:
                if states[step.step_id] == "SUCCEEDED":
                    continue
                if any(states[dependency] != "SUCCEEDED" for dependency in step.depends_on):
                    continue
                if step.kind == "barrier":
                    self._set_step_state(connection, run_id, step.step_id, "SUCCEEDED", current)
                    states[step.step_id] = "SUCCEEDED"
                    progressed = True
                    continue
                if not isinstance(step, (ActionStepV2, CompensationStepV2)):
                    reason = f"step {step.step_id} requires external evidence or approval"
                    self._set_step_state(connection, run_id, step.step_id, "HELD", current)
                    self._set_run_state(connection, run_id, "HELD", current, reason)
                    updated = connection.execute(
                        "SELECT * FROM cell_orchestrator_runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    return self._decode_run(connection, updated)

                existing_commands = connection.execute(
                    """
                    SELECT * FROM cell_orchestrator_commands
                    WHERE run_id = ? AND step_id = ? ORDER BY asset_id
                    """,
                    (run_id, step.step_id),
                ).fetchall()
                if existing_commands:
                    if self._query_commands(connection, existing_commands, current):
                        self._set_step_state(
                            connection, run_id, step.step_id, "SUCCEEDED", current
                        )
                        states[step.step_id] = "SUCCEEDED"
                        progressed = True
                        continue
                    self._set_run_state(
                        connection,
                        run_id,
                        "RECONCILING",
                        current,
                        f"step {step.step_id} response is uncertain",
                    )
                    updated = connection.execute(
                        "SELECT * FROM cell_orchestrator_runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    return self._decode_run(connection, updated)

                selected_targets = targets[step.role]
                if step.fan_out == "first_n":
                    selected_targets = selected_targets[: step.fan_out_count]
                elif step.fan_out == "explicit_binding_set":
                    selected_targets = [
                        selected_targets[index] for index in step.binding_ordinals
                    ]
                for target in selected_targets:
                    request = self._request_for(run=run, step=step, target=target, now=current)
                    encoded = canonical_json(request.model_dump(mode="json"))
                    connection.execute(
                        """
                        INSERT INTO cell_orchestrator_commands (
                            command_id, run_id, step_id, asset_id, agent_id,
                            request_json, last_state, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'DISPATCHING', ?, ?)
                        """,
                        (
                            request.command_id,
                            run_id,
                            step.step_id,
                            target.asset_id,
                            target.agent_id,
                            encoded,
                            isoformat_z(current),
                            isoformat_z(current),
                        ),
                    )
                    self._set_step_state(
                        connection, run_id, step.step_id, "DISPATCHED", current
                    )
                    # The intent must survive a process death after the Agent accepts it.
                    connection.commit()
                    try:
                        result = self.transport.execute(request, now=current)
                    except ConnectionError as exc:
                        connection.execute("BEGIN IMMEDIATE")
                        self._set_run_state(
                            connection,
                            run_id,
                            "RECONCILING",
                            current,
                            f"uncertain reply for {request.command_id}: {exc}",
                        )
                        updated = connection.execute(
                            "SELECT * FROM cell_orchestrator_runs WHERE run_id = ?", (run_id,)
                        ).fetchone()
                        return self._decode_run(connection, updated)
                    completed_at = parse_datetime(result.updated_at)
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        UPDATE cell_orchestrator_commands
                        SET last_state = ?, updated_at = ? WHERE command_id = ?
                        """,
                        (result.state, isoformat_z(completed_at), request.command_id),
                    )
                    if result.state != "SUCCEEDED":
                        self._set_run_state(
                            connection,
                            run_id,
                            "HELD",
                            completed_at,
                            f"Agent returned {result.state} for {request.command_id}",
                        )
                        updated = connection.execute(
                            "SELECT * FROM cell_orchestrator_runs WHERE run_id = ?", (run_id,)
                        ).fetchone()
                        return self._decode_run(connection, updated)
                step_completed_at = max(
                    (
                        parse_datetime(row["updated_at"])
                        for row in connection.execute(
                            """
                            SELECT updated_at FROM cell_orchestrator_commands
                            WHERE run_id = ? AND step_id = ?
                            """,
                            (run_id, step.step_id),
                        ).fetchall()
                    ),
                    default=current,
                )
                self._set_step_state(
                    connection, run_id, step.step_id, "SUCCEEDED", step_completed_at
                )
                states[step.step_id] = "SUCCEEDED"
                progressed = True

            if all(state == "SUCCEEDED" for state in states.values()):
                completion = max(
                    (parse_datetime(row["updated_at"]) for row in connection.execute(
                        "SELECT updated_at FROM cell_orchestrator_steps WHERE run_id = ?",
                        (run_id,),
                    ).fetchall()),
                    default=current,
                )
                self._set_run_state(connection, run_id, "COMPLETE", completion)
            elif not progressed:
                self._set_run_state(
                    connection, run_id, "HELD", current, "workflow made no progress"
                )
            updated = connection.execute(
                "SELECT * FROM cell_orchestrator_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return self._decode_run(connection, updated)

    def _query_commands(
        self,
        connection: sqlite3.Connection,
        commands: list[sqlite3.Row],
        now: datetime,
    ) -> bool:
        complete = True
        for command in commands:
            result = self.transport.query(command["agent_id"], command["command_id"])
            if result is None:
                return False
            result_time = parse_datetime(result.updated_at)
            connection.execute(
                """
                UPDATE cell_orchestrator_commands
                SET last_state = ?, updated_at = ? WHERE command_id = ?
                """,
                (result.state, isoformat_z(result_time), command["command_id"]),
            )
            if result.state != "SUCCEEDED":
                complete = False
        return complete

    def _reconcile(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        now: datetime,
    ) -> dict:
        commands = connection.execute(
            """
            SELECT * FROM cell_orchestrator_commands
            WHERE run_id = ? AND last_state != 'SUCCEEDED'
            ORDER BY created_at
            """,
            (run["run_id"],),
        ).fetchall()
        if not commands:
            self._set_run_state(connection, run["run_id"], "ACTIVE", now)
        elif self._query_commands(connection, commands, now):
            for command in commands:
                self._set_step_state(
                    connection,
                    run["run_id"],
                    command["step_id"],
                    "SUCCEEDED",
                    now,
                )
            self._set_run_state(connection, run["run_id"], "ACTIVE", now)
        else:
            self._set_run_state(
                connection,
                run["run_id"],
                "HELD",
                now,
                "reconciliation could not prove prior command completion",
            )
        updated = connection.execute(
            "SELECT * FROM cell_orchestrator_runs WHERE run_id = ?", (run["run_id"],)
        ).fetchone()
        return self._decode_run(connection, updated)

    def get_run(self, run_id: str) -> dict | None:
        with self._connect() as connection:
            run = connection.execute(
                "SELECT * FROM cell_orchestrator_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return None if run is None else self._decode_run(connection, run)

    @staticmethod
    def _decode_run(connection: sqlite3.Connection, run: sqlite3.Row) -> dict:
        steps = [
            dict(row)
            for row in connection.execute(
                """
                SELECT step_id, state, updated_at FROM cell_orchestrator_steps
                WHERE run_id = ? ORDER BY rowid
                """,
                (run["run_id"],),
            ).fetchall()
        ]
        commands = [
            {
                "command_id": row["command_id"],
                "step_id": row["step_id"],
                "asset_id": row["asset_id"],
                "agent_id": row["agent_id"],
                "state": row["last_state"],
            }
            for row in connection.execute(
                """
                SELECT * FROM cell_orchestrator_commands
                WHERE run_id = ? ORDER BY created_at, command_id
                """,
                (run["run_id"],),
            ).fetchall()
        ]
        return {
            "run_id": run["run_id"],
            "execution_mode": run["execution_mode"],
            "state": run["state"],
            "lease_epoch": run["lease_epoch"],
            "reason": run["reason"],
            "steps": steps,
            "commands": commands,
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
        }
