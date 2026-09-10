"""Durable output-disconnected execution ledger used by simulation Agents."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable
from uuid import UUID, uuid4

from fleet_control_plane.control_contracts import (
    CancelExecutionRequestV1,
    ExecuteSkillRequestV1,
    ExecutionRecordV1,
)
from fleet_control_plane.control_signing import VerificationKeyRing
from fleet_control_plane.orchestration_store import canonical_json
from fleet_control_plane.status import isoformat_z, parse_datetime


AGENT_EXECUTION_MIGRATION = """
CREATE TABLE IF NOT EXISTS agent_lease_fences (
    run_id TEXT PRIMARY KEY,
    holder_id TEXT NOT NULL,
    holder_boot_id TEXT NOT NULL,
    max_epoch INTEGER NOT NULL CHECK (max_epoch >= 1),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_execution_commands (
    command_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    request_json TEXT NOT NULL,
    record_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
    state TEXT NOT NULL CHECK (
        state IN ('ACCEPTED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class AgentExecutionError(ValueError):
    pass


class CommandConflict(AgentExecutionError):
    pass


class LeaseRejected(AgentExecutionError):
    pass


class LedgerCorruption(AgentExecutionError):
    pass


@dataclass(frozen=True)
class DeferredExecution:
    result: dict


Executor = Callable[[ExecuteSkillRequestV1], dict | DeferredExecution]


class SimulationAgent:
    """An Agent with no device controller and no ROS command publisher."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        agent_id: str,
        keyring: VerificationKeyRing,
        capabilities: set[str],
        boot_id: UUID | None = None,
        executor: Executor | None = None,
        safe_state_handler: Callable[[str], None] | None = None,
    ) -> None:
        self.path = Path(database_path)
        self.agent_id = agent_id
        self.boot_id = boot_id or uuid4()
        self.keyring = keyring
        self.capabilities = frozenset(capabilities)
        self.executor = executor or self._fixture_executor
        self.safe_state_handler = safe_state_handler
        self.command_sink_present = False
        with self._connect() as connection:
            connection.executescript(AGENT_EXECUTION_MIGRATION)

    @staticmethod
    def _fixture_executor(request: ExecuteSkillRequestV1) -> dict:
        return {
            "fixture": True,
            "step_id": request.step_id,
            "parameters": request.parameters,
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _decode_record(self, row: sqlite3.Row) -> ExecutionRecordV1:
        if self._digest(row["record_json"]) != row["record_sha256"]:
            raise LedgerCorruption(f"execution record is corrupt: {row['command_id']}")
        return ExecutionRecordV1.model_validate_json(row["record_json"])

    def _store_record(
        self,
        connection: sqlite3.Connection,
        request_json: str,
        request_sha256: str,
        record: ExecutionRecordV1,
    ) -> None:
        encoded = canonical_json(record.model_dump(mode="json"))
        connection.execute(
            """
            INSERT INTO agent_execution_commands (
                command_id, request_sha256, request_json, record_json,
                record_sha256, state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.command_id,
                request_sha256,
                request_json,
                encoded,
                self._digest(encoded),
                record.state,
                isoformat_z(record.accepted_at),
                isoformat_z(record.updated_at),
            ),
        )

    def _replace_record(
        self,
        connection: sqlite3.Connection,
        record: ExecutionRecordV1,
    ) -> None:
        encoded = canonical_json(record.model_dump(mode="json"))
        connection.execute(
            """
            UPDATE agent_execution_commands
            SET record_json = ?, record_sha256 = ?, state = ?, updated_at = ?
            WHERE command_id = ?
            """,
            (
                encoded,
                self._digest(encoded),
                record.state,
                isoformat_z(record.updated_at),
                record.command_id,
            ),
        )

    def _apply_fence(
        self,
        connection: sqlite3.Connection,
        request: ExecuteSkillRequestV1,
        now: datetime,
    ) -> None:
        lease = request.control_sublease
        row = connection.execute(
            "SELECT * FROM agent_lease_fences WHERE run_id = ?",
            (request.run_id,),
        ).fetchone()
        if row is not None:
            if lease.epoch < row["max_epoch"]:
                raise LeaseRejected("lease epoch is fenced by a newer epoch")
            if lease.epoch == row["max_epoch"] and (
                lease.orchestrator_id != row["holder_id"]
                or str(lease.orchestrator_boot_id) != row["holder_boot_id"]
            ):
                raise LeaseRejected("same-epoch competing holder incarnation rejected")
        connection.execute(
            """
            INSERT INTO agent_lease_fences (
                run_id, holder_id, holder_boot_id, max_epoch, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                holder_id = excluded.holder_id,
                holder_boot_id = excluded.holder_boot_id,
                max_epoch = excluded.max_epoch,
                updated_at = excluded.updated_at
            """,
            (
                request.run_id,
                lease.orchestrator_id,
                str(lease.orchestrator_boot_id),
                lease.epoch,
                isoformat_z(now),
            ),
        )

    def execute(self, request: ExecuteSkillRequestV1, *, now: datetime) -> ExecutionRecordV1:
        if request.agent_id != self.agent_id:
            raise AgentExecutionError("request targets a different Agent")
        if request.execution_mode not in {"SIMULATION", "REPLAY", "HIL"}:
            raise LeaseRejected("physical execution modes are unavailable before G7")
        if request.capability_ref not in self.capabilities:
            raise LeaseRejected("Agent does not declare the requested capability")

        request_json = canonical_json(request.model_dump(mode="json"))
        request_sha256 = self._digest(request_json)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM agent_execution_commands WHERE command_id = ?",
                (request.command_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha256:
                    raise CommandConflict("command_id was reused with different content")
                return self._decode_record(existing)

            self.keyring.verify_contract(request.delegation_grant, at=now)
            self.keyring.verify_contract(request.control_sublease, at=now)
            self.keyring.verify_payload(
                key_id=request.control_sublease.issuer_key_id,
                signature_text=request.goal_signature,
                payload=request.goal,
                at=now,
            )
            lease = request.control_sublease
            grant = request.delegation_grant
            current = parse_datetime(now)
            if current >= parse_datetime(grant.expires_at):
                raise LeaseRejected("delegation grant has expired")
            if current >= parse_datetime(lease.expires_at):
                raise LeaseRejected("lease has expired")
            if current >= parse_datetime(request.goal.not_valid_after):
                raise LeaseRejected("ExecuteSkill goal validity has expired")
            self._apply_fence(connection, request, current)
            accepted = ExecutionRecordV1(
                command_id=request.command_id,
                run_id=request.run_id,
                step_id=request.step_id,
                agent_id=self.agent_id,
                agent_boot_id=self.boot_id,
                capability_ref=request.capability_ref,
                execution_mode=request.execution_mode,
                lease_epoch=lease.epoch,
                state="ACCEPTED",
                accepted_at=current,
                updated_at=current,
            )
            self._store_record(connection, request_json, request_sha256, accepted)
            # ACCEPTED must survive a process death while a simulator command is in flight.
            connection.commit()
        try:
            result = self.executor(request)
            completed_at = datetime.now(timezone.utc)
            if isinstance(result, DeferredExecution):
                completed = accepted.model_copy(
                    update={
                        "state": "RUNNING",
                        "result": result.result,
                        "updated_at": completed_at,
                    }
                )
            else:
                completed = accepted.model_copy(
                    update={
                        "state": "SUCCEEDED",
                        "result": result,
                        "updated_at": completed_at,
                    }
                )
        except Exception as exc:
            completed = accepted.model_copy(
                update={
                    "state": "FAILED",
                    "error_code": "fixture-executor-failed",
                    "error_message": str(exc) or type(exc).__name__,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._replace_record(connection, completed)
        return completed

    def query(self, command_id: str) -> ExecutionRecordV1 | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_execution_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
        return None if row is None else self._decode_record(row)

    def cancel(
        self,
        cancellation: CancelExecutionRequestV1,
        *,
        now: datetime,
    ) -> ExecutionRecordV1:
        current = parse_datetime(now)
        if cancellation.agent_id != self.agent_id:
            raise LeaseRejected("cancellation targets a different Agent")
        if current < parse_datetime(cancellation.issued_at):
            raise LeaseRejected("cancellation is future-dated")
        if current >= parse_datetime(cancellation.expires_at):
            raise LeaseRejected("cancellation has expired")
        self.keyring.verify_payload(
            key_id=cancellation.signing_key_id,
            signature_text=cancellation.signature,
            payload=cancellation,
            at=current,
        )
        command_id = str(cancellation.command_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_execution_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if row is None:
                raise AgentExecutionError(f"unknown command: {command_id}")
            record = self._decode_record(row)
            original = ExecuteSkillRequestV1.model_validate_json(row["request_json"])
            if cancellation.run_id != record.run_id:
                raise LeaseRejected("cancellation Run identity does not match")
            if cancellation.signing_key_id != original.control_sublease.issuer_key_id:
                raise LeaseRejected("cancellation signer differs from the command lease issuer")
            if record.state in {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}:
                return record
            cancelled = record.model_copy(
                update={"state": "CANCELLED", "updated_at": current}
            )
            self._replace_record(connection, cancelled)
            return cancelled

    def renew(
        self,
        command_id: str,
        renewed: ExecuteSkillRequestV1,
        *,
        now: datetime,
    ) -> ExecutionRecordV1:
        if renewed.command_id != command_id:
            raise LeaseRejected("renewal command identity does not match")
        self.keyring.verify_contract(renewed.delegation_grant, at=now)
        self.keyring.verify_contract(renewed.control_sublease, at=now)
        self.keyring.verify_payload(
            key_id=renewed.control_sublease.issuer_key_id,
            signature_text=renewed.goal_signature,
            payload=renewed.goal,
            at=now,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_execution_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if row is None:
                raise AgentExecutionError(f"unknown command: {command_id}")
            record = self._decode_record(row)
            if record.state != "RUNNING":
                raise LeaseRejected("only a RUNNING command lease can be renewed")
            original = ExecuteSkillRequestV1.model_validate_json(row["request_json"])
            old_lease = original.control_sublease
            new_lease = renewed.control_sublease
            immutable = (
                original.agent_id,
                original.run_id,
                original.step_id,
                original.goal.command_id,
                original.goal.asset_id,
                original.capability_ref,
                old_lease.lease_id,
                old_lease.parent_grant_id,
                old_lease.epoch,
                old_lease.orchestrator_id,
                old_lease.orchestrator_boot_id,
            )
            proposed = (
                renewed.agent_id,
                renewed.run_id,
                renewed.step_id,
                renewed.goal.command_id,
                renewed.goal.asset_id,
                renewed.capability_ref,
                new_lease.lease_id,
                new_lease.parent_grant_id,
                new_lease.epoch,
                new_lease.orchestrator_id,
                new_lease.orchestrator_boot_id,
            )
            if proposed != immutable:
                raise LeaseRejected("renewal changed immutable command or lease identity")
            current = parse_datetime(now)
            if current >= parse_datetime(old_lease.expires_at):
                raise LeaseRejected("cannot renew an expired lease")
            if parse_datetime(new_lease.expires_at) <= parse_datetime(old_lease.expires_at):
                raise LeaseRejected("renewal must extend the lease expiry")
            encoded = canonical_json(renewed.model_dump(mode="json"))
            connection.execute(
                """
                UPDATE agent_execution_commands
                SET request_json = ?, request_sha256 = ?, updated_at = ?
                WHERE command_id = ?
                """,
                (encoded, self._digest(encoded), isoformat_z(current), command_id),
            )
            return record

    def expire_due(self, *, now: datetime) -> list[ExecutionRecordV1]:
        current = parse_datetime(now)
        expired: list[ExecutionRecordV1] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM agent_execution_commands WHERE state = 'RUNNING'"
            ).fetchall()
            for row in rows:
                request = ExecuteSkillRequestV1.model_validate_json(row["request_json"])
                if current < parse_datetime(request.control_sublease.expires_at):
                    continue
                record = self._decode_record(row).model_copy(
                    update={
                        "state": "EXPIRED",
                        "updated_at": current,
                        "error_code": "lease-expired",
                        "error_message": "lease expired while execution was in flight",
                    }
                )
                self._replace_record(connection, record)
                expired.append(record)
        if self.safe_state_handler is not None:
            for record in expired:
                self.safe_state_handler(f"lease expired: {record.command_id}")
        return expired
