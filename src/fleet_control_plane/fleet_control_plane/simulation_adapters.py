"""In-process Unity and Isaac Agent adapters with no physical command sink."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from fleet_control_plane.agent_execution import SimulationAgent
from fleet_control_plane.control_contracts import ExecuteSkillRequestV1, ExecutionRecordV1
from fleet_control_plane.control_signing import VerificationKeyRing


class SimulationBackend(Protocol):
    command_sink_present: bool
    process_output_connected: bool

    def execute_skill(self, request: ExecuteSkillRequestV1) -> dict:
        """Apply a skill to a simulator process and return simulation evidence."""

    def recover_skill(self, request: ExecuteSkillRequestV1) -> dict:
        """Recover the same idempotent simulator command after a Worker restart."""


class SimulationAgentAdapter(SimulationAgent):
    def __init__(
        self,
        database_path: str | Path,
        *,
        simulator_kind: str,
        agent_id: str,
        keyring: VerificationKeyRing,
        capabilities: set[str],
        backend: SimulationBackend,
        boot_id: UUID | None = None,
    ) -> None:
        if simulator_kind not in {"unity", "isaac"}:
            raise ValueError("simulator_kind must be unity or isaac")
        if (
            getattr(backend, "command_sink_present", None) is not False
            or getattr(backend, "process_output_connected", None) is not False
        ):
            raise ValueError("simulation backend must declare all physical outputs disconnected")
        self.simulator_kind = simulator_kind
        self.backend = backend
        super().__init__(
            database_path,
            agent_id=agent_id,
            keyring=keyring,
            capabilities=capabilities,
            boot_id=boot_id,
            executor=self._execute_simulation,
        )

    def _execute_simulation(self, request: ExecuteSkillRequestV1) -> dict:
        if request.execution_mode != "SIMULATION":
            raise ValueError(f"{self.simulator_kind} adapter accepts SIMULATION only")
        result = self.backend.execute_skill(request)
        return {
            "simulator": self.simulator_kind,
            "command_sink_present": False,
            "evidence": result,
        }

    def recover(self, command_id: str) -> ExecutionRecordV1 | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_execution_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
        if row is None:
            return None
        record = self._decode_record(row)
        if record.state not in {"ACCEPTED", "RUNNING"}:
            return record
        recover_skill = getattr(self.backend, "recover_skill", None)
        if not callable(recover_skill):
            return record
        request = ExecuteSkillRequestV1.model_validate_json(row["request_json"])
        try:
            evidence = recover_skill(request)
            recovered = record.model_copy(
                update={
                    "state": "SUCCEEDED",
                    "result": {
                        "simulator": self.simulator_kind,
                        "command_sink_present": False,
                        "evidence": evidence,
                    },
                    "updated_at": datetime.now(timezone.utc),
                }
            )
        except Exception as exc:
            recovered = record.model_copy(
                update={
                    "state": "FAILED",
                    "error_code": "simulation-recovery-failed",
                    "error_message": str(exc) or type(exc).__name__,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._replace_record(connection, recovered)
        return recovered


class UnitySimulationAgent(SimulationAgentAdapter):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, simulator_kind="unity", **kwargs)


class IsaacSimulationAgent(SimulationAgentAdapter):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, simulator_kind="isaac", **kwargs)
