"""G6a output-disconnected Device Agent and non-authorizing readiness report."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from fleet_control_plane.agent_execution import (
    DeferredExecution,
    LedgerCorruption,
    SimulationAgent,
)
from fleet_control_plane.control_contracts import (
    CancelExecutionRequestV1,
    ExecuteSkillRequestV1,
    ExecutionRecordV1,
)
from fleet_control_plane.control_signing import VerificationKeyRing
from fleet_control_plane.models import FleetModel, SafeId
from fleet_control_plane.orchestration_models import (
    CapabilityRef,
    CapabilityProfileV1,
    DeviceManifestV1,
    RegistryRefV1,
    SchemaReference,
)
from fleet_control_plane.orchestration_store import payload_digest
from fleet_control_plane.status import parse_datetime


FailAction = Literal["controlled_hold", "controlled_cancel", "safe_state", "inhibit"]


class FailBehaviorContractV1(FleetModel):
    schema_version: Literal[1] = 1
    contract_id: SafeId
    version: int = Field(ge=1)
    capability_ref: CapabilityRef
    on_lease_expiry: FailAction
    on_communication_loss: FailAction
    on_agent_restart: FailAction
    on_cancel: FailAction
    declared_time_to_safe_state_ms: float = Field(gt=0.0)


class OutputIsolationEvidenceV1(FleetModel):
    schema_version: Literal[1] = 1
    evidence_id: SafeId
    version: int = Field(ge=1)
    agent_id: SafeId
    controller_id: SafeId
    observed_at: AwareDatetime
    observer_id: SafeId
    command_sink_present: Literal[False] = False
    physical_cabling_connected: Literal[False] = False
    process_output_connected: Literal[False] = False
    artifact_refs: list[SchemaReference] = Field(min_length=1, max_length=64)


class SoftwareTimingEvidenceV1(FleetModel):
    schema_version: Literal[1] = 1
    evidence_id: SafeId
    version: int = Field(ge=1)
    capability_ref: CapabilityRef
    measured_at: AwareDatetime
    method: SafeId
    isolation_evidence_ref: RegistryRefV1
    safe_state_latency_ms: list[float] = Field(min_length=5, max_length=10_000)
    output_disconnected: Literal[True] = True

    @model_validator(mode="after")
    def samples_are_positive(self) -> Self:
        if any(sample < 0.0 for sample in self.safe_state_latency_ms):
            raise ValueError("safe-state latency samples cannot be negative")
        return self


class G6ReadinessInputV1(FleetModel):
    schema_version: Literal[1] = 1
    agent_id: SafeId
    manifest: DeviceManifestV1
    capability_profile: CapabilityProfileV1
    fail_behaviors: list[FailBehaviorContractV1] = Field(min_length=1, max_length=256)
    output_isolation: OutputIsolationEvidenceV1
    timing_evidence: list[SoftwareTimingEvidenceV1] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def evidence_sets_are_coherent(self) -> Self:
        fail_capabilities = [item.capability_ref for item in self.fail_behaviors]
        timing_capabilities = [item.capability_ref for item in self.timing_evidence]
        if len(fail_capabilities) != len(set(fail_capabilities)):
            raise ValueError("fail behavior capabilities must be unique")
        if len(timing_capabilities) != len(set(timing_capabilities)):
            raise ValueError("timing evidence capabilities must be unique")
        for evidence in self.timing_evidence:
            reference = evidence.isolation_evidence_ref
            if (
                reference.object_id != self.output_isolation.evidence_id
                or reference.version != self.output_isolation.version
            ):
                raise ValueError("timing evidence references different output isolation")
            if reference.sha256 != payload_digest(
                self.output_isolation.model_dump(mode="json")
            ):
                raise ValueError("timing evidence output-isolation digest does not match")
        return self


class ReadinessCheckV1(FleetModel):
    check_id: SafeId
    passed: bool
    detail: str = Field(min_length=1, max_length=512)


class G6ReadinessReportV1(FleetModel):
    schema_version: Literal[1] = 1
    agent_id: SafeId
    generated_at: AwareDatetime
    software_status: Literal["SOFTWARE_READY", "NOT_READY"]
    g6b_lab_evidence_status: Literal["UNVERIFIED"] = "UNVERIFIED"
    checks: list[ReadinessCheckV1]
    lab_blockers: list[str]
    command_sink_present: Literal[False] = False
    physical_motion_enabled: Literal[False] = False
    process_output_enabled: Literal[False] = False
    g7_authorized: Literal[False] = False


class OutputDisconnectedController:
    def __init__(self, controller_id: str = "output-disconnected-controller") -> None:
        self.controller_id = controller_id
        self.command_sink_present = False
        self.process_output_connected = False
        self.commands: list[str] = []
        self.safe_state_requests: list[str] = []
        self.fail_actions: list[dict[str, str]] = []
        self.fault_events: dict[str, str] = {}
        self._fault_once: str | None = None

    def inject_fault_once(
        self, fault: Literal["defer", "reject", "timeout", "restart"]
    ) -> None:
        self._fault_once = fault

    def execute(self, request: ExecuteSkillRequestV1) -> dict | DeferredExecution:
        fault = self._fault_once
        self._fault_once = None
        if fault == "defer":
            self.commands.append(request.command_id)
            return DeferredExecution(
                result={
                    "controller_id": self.controller_id,
                    "fixture_acknowledged": True,
                    "command_sink_present": False,
                }
            )
        if fault is not None:
            self.fault_events[request.command_id] = fault
        if fault == "reject":
            raise ValueError("disconnected controller rejected the fixture command")
        if fault == "timeout":
            raise TimeoutError("disconnected controller fixture timeout")
        if fault == "restart":
            raise RuntimeError("disconnected controller fixture restarted")
        self.commands.append(request.command_id)
        return {
            "controller_id": self.controller_id,
            "fixture_acknowledged": True,
            "command_sink_present": False,
        }

    def request_safe_state(self, reason: str) -> None:
        self.safe_state_requests.append(reason)

    def apply_fail_action(self, action: FailAction, reason: str) -> None:
        self.fail_actions.append({"action": action, "reason": reason})
        if action == "safe_state":
            self.request_safe_state(reason)


class DeviceAgentSkeleton(SimulationAgent):
    """Command-capable contract shell whose only controller has no output channel."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        agent_id: str,
        keyring: VerificationKeyRing,
        capabilities: set[str],
        controller: OutputDisconnectedController,
        fail_behaviors: list[FailBehaviorContractV1],
        boot_id: UUID | None = None,
    ) -> None:
        if controller.command_sink_present or controller.process_output_connected:
            raise ValueError("G6 Device Agent requires an output-disconnected controller")
        by_capability = {item.capability_ref: item for item in fail_behaviors}
        if len(by_capability) != len(fail_behaviors) or set(by_capability) != capabilities:
            raise ValueError("G6 Device Agent requires one fail behavior per capability")
        self.controller = controller
        self.fail_behaviors = by_capability
        super().__init__(
            database_path,
            agent_id=agent_id,
            keyring=keyring,
            capabilities=capabilities,
            boot_id=boot_id,
            executor=controller.execute,
            safe_state_handler=None,
        )

    def _apply_fail_behavior(
        self,
        capability_ref: CapabilityRef,
        event: Literal[
            "on_lease_expiry", "on_communication_loss", "on_agent_restart", "on_cancel"
        ],
        reason: str,
    ) -> None:
        contract = self.fail_behaviors[capability_ref]
        self.controller.apply_fail_action(getattr(contract, event), reason)

    def execute(self, request: ExecuteSkillRequestV1, *, now: datetime) -> ExecutionRecordV1:
        try:
            record = super().execute(request, now=now)
        except LedgerCorruption:
            self.controller.apply_fail_action(
                "safe_state", f"ledger corruption while executing {request.command_id}"
            )
            raise
        fault = self.controller.fault_events.pop(request.command_id, None)
        if record.state == "FAILED":
            event = {
                "reject": "on_cancel",
                "restart": "on_agent_restart",
                "timeout": "on_communication_loss",
            }.get(fault, "on_communication_loss")
            self._apply_fail_behavior(
                record.capability_ref,
                event,
                f"{event}: {record.command_id}",
            )
        return record

    def query(self, command_id: str) -> ExecutionRecordV1 | None:
        try:
            return super().query(command_id)
        except LedgerCorruption:
            self.controller.apply_fail_action(
                "safe_state", f"ledger corruption while querying {command_id}"
            )
            raise

    def cancel(
        self,
        cancellation: CancelExecutionRequestV1,
        *,
        now: datetime,
    ) -> ExecutionRecordV1:
        command_id = str(cancellation.command_id)
        previous = self.query(command_id)
        record = super().cancel(cancellation, now=now)
        if previous is not None and previous.state in {"ACCEPTED", "RUNNING"}:
            self._apply_fail_behavior(
                previous.capability_ref,
                "on_cancel",
                f"on_cancel: {command_id}",
            )
        return record

    def expire_due(self, *, now: datetime) -> list[ExecutionRecordV1]:
        try:
            records = super().expire_due(now=now)
        except LedgerCorruption:
            self.controller.apply_fail_action(
                "safe_state", "ledger corruption while expiring command leases"
            )
            raise
        for record in records:
            self._apply_fail_behavior(
                record.capability_ref,
                "on_lease_expiry",
                f"on_lease_expiry: {record.command_id}",
            )
        return records

    def handle_communication_loss(
        self, command_id: str, *, now: datetime
    ) -> ExecutionRecordV1:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_execution_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown command: {command_id}")
            record = self._decode_record(row)
            if record.state != "RUNNING":
                return record
            failed = record.model_copy(
                update={
                    "state": "FAILED",
                    "updated_at": parse_datetime(now),
                    "error_code": "communication-lost",
                    "error_message": "communication was lost while execution was in flight",
                }
            )
            self._replace_record(connection, failed)
        self._apply_fail_behavior(
            failed.capability_ref,
            "on_communication_loss",
            f"on_communication_loss: {failed.command_id}",
        )
        return failed

    def recover_after_restart(self, *, now: datetime) -> list[ExecutionRecordV1]:
        recovered: list[ExecutionRecordV1] = []
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT * FROM agent_execution_commands WHERE state = 'RUNNING'"
                ).fetchall()
                for row in rows:
                    record = self._decode_record(row).model_copy(
                        update={
                            "state": "FAILED",
                            "updated_at": parse_datetime(now),
                            "error_code": "agent-restarted",
                            "error_message": "Agent restarted while execution was in flight",
                        }
                    )
                    self._replace_record(connection, record)
                    recovered.append(record)
        except LedgerCorruption:
            self.controller.apply_fail_action(
                "safe_state", "ledger corruption during Agent restart recovery"
            )
            raise
        for record in recovered:
            self._apply_fail_behavior(
                record.capability_ref,
                "on_agent_restart",
                f"on_agent_restart: {record.command_id}",
            )
        return recovered


def evaluate_g6_readiness(
    body: G6ReadinessInputV1,
    *,
    generated_at: datetime,
) -> G6ReadinessReportV1:
    checks: list[ReadinessCheckV1] = []

    def add(check_id: str, passed: bool, detail: str) -> None:
        checks.append(ReadinessCheckV1(check_id=check_id, passed=passed, detail=detail))

    execution = body.manifest.execution_contract
    add(
        "output-disconnected",
        not execution.command_sink_present
        and not body.output_isolation.command_sink_present
        and not body.output_isolation.physical_cabling_connected
        and not body.output_isolation.process_output_connected,
        "Manifest, controller, physical cable, and process output must all remain disconnected.",
    )
    modes = set(execution.supported_modes)
    add(
        "g6-modes-only",
        "HIL" in modes
        and not modes.intersection({"PHYSICAL_AUTO", "PHYSICAL_TELEOP", "MAINTENANCE"}),
        "G6 requires HIL support and forbids all commissioned physical modes.",
    )
    profile_capabilities = {
        capability.ref: capability for capability in body.capability_profile.capabilities
    }
    manifest_capabilities = set(body.manifest.capability_refs)
    add(
        "manifest-profile-match",
        manifest_capabilities.issubset(profile_capabilities),
        "Every manifest capability must resolve in the pinned profile.",
    )
    effectful = {
        ref
        for ref in manifest_capabilities
        if ref in profile_capabilities and profile_capabilities[ref].effect != "observation"
    }
    fail_by_capability = {item.capability_ref: item for item in body.fail_behaviors}
    add(
        "fail-behavior-complete",
        effectful == set(fail_by_capability),
        "Every effectful capability requires one explicit fail-behavior contract.",
    )
    timing_by_capability = {item.capability_ref: item for item in body.timing_evidence}
    timing_complete = (
        effectful == set(timing_by_capability) == set(fail_by_capability)
    )
    if timing_complete:
        timing_complete = all(
            max(timing_by_capability[capability].safe_state_latency_ms)
            <= fail_by_capability[capability].declared_time_to_safe_state_ms
            for capability in effectful
        )
    add(
        "software-timing-evidence",
        timing_complete,
        "At least five isolated software samples must stay within the declared safe-state time.",
    )
    add(
        "agent-identity-match",
        body.agent_id == body.output_isolation.agent_id,
        "Output-isolation evidence must identify this Device Agent.",
    )

    ready = all(check.passed for check in checks)
    return G6ReadinessReportV1(
        agent_id=body.agent_id,
        generated_at=generated_at,
        software_status="SOFTWARE_READY" if ready else "NOT_READY",
        checks=checks,
        lab_blockers=[
            "Measured physical stop time and stop distance for each robot/tool/payload",
            "Witnessed hardwired E-stop and safety-chain isolation behavior",
            "Approved canonical cell-frame calibration and tolerance evidence",
            "Named lab Safety Officer approval for attended G7 motion",
        ],
    )
