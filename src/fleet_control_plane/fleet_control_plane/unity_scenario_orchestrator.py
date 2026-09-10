"""Scenario-specific loopback bridge for the Unity Scenario 1 simulation.

The generic Cell Orchestrator and Simulation Agent still own authorization,
leases, idempotency, and journaling.  This module only adapts an accepted
SIMULATION skill to the currently open Unity Editor process.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from threading import Event
import time
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import AwareDatetime, Field, model_validator

from fleet_control_plane.cell_orchestrator import (
    AgentTargetV1,
    CellOrchestrator,
    OrchestratorRunCreateV1,
    SimulationTransport,
)
from fleet_control_plane.control_contracts import ExecuteSkillRequestV1
from fleet_control_plane.control_signing import SigningKey, VerificationKeyRing
from fleet_control_plane.models import FleetModel, SafeId
from fleet_control_plane.orchestration_models import (
    ControlEnvelopeV1,
    WorkflowDefinitionV2,
)
from fleet_control_plane.orchestration_store import canonical_json, payload_digest
from fleet_control_plane.simulation_adapters import UnitySimulationAgent
from fleet_control_plane.asset_models import UnityBindingId, UnitySceneInventoryV1
from fleet_control_plane.orchestration_models import CapabilityRef, Sha256Digest
from fleet_control_plane.unity_dynamic_binding import (
    ResolvedUnityAction, UnityDynamicBindingV1,
)


class UnityBridgeError(RuntimeError):
    """The Unity bridge is unavailable or returned an invalid result."""


class UnityRunAborted(UnityBridgeError):
    """Unity acknowledged a terminal simulation-only abort."""


class UnityRoleBindingV1(FleetModel):
    asset_id: SafeId
    agent_id: SafeId


class UnityScenarioConfigV1(FleetModel):
    schema_version: Literal[1] = 1
    scenario_id: SafeId
    workflow_path: str = Field(min_length=1, max_length=2048)
    bridge_root: str = Field(min_length=1, max_length=2048)
    heartbeat_max_age_s: float = Field(gt=0.0, le=60.0)
    result_timeout_s: float = Field(gt=0.0, le=3600.0)
    roles: dict[SafeId, UnityRoleBindingV1] = Field(min_length=1, max_length=32)


class UnityBridgeCommandV1(FleetModel):
    schema_version: Literal[1] = 1
    scenario_id: SafeId
    command_id: SafeId
    run_id: SafeId
    step_id: SafeId
    agent_id: SafeId
    capability: str = Field(min_length=3, max_length=96)
    parameters: dict = Field(default_factory=dict, max_length=256)
    issued_at: AwareDatetime
    not_valid_after: AwareDatetime
    execution_mode: Literal["SIMULATION"] = "SIMULATION"
    physical_output_connected: Literal[False] = False

    @model_validator(mode="after")
    def validity_window_is_positive(self):
        if self.not_valid_after <= self.issued_at:
            raise ValueError("Unity command expiry must follow its issue time")
        return self


class UnityBridgeResultV1(FleetModel):
    schema_version: Literal[1] = 1
    scenario_id: SafeId
    command_id: SafeId
    run_id: SafeId
    step_id: SafeId
    state: Literal["SUCCEEDED", "FAILED", "REJECTED", "ABORTED"]
    detail: str = Field(min_length=1, max_length=2048)
    observed_phase: str = Field(min_length=1, max_length=128)
    completed_at: AwareDatetime
    physical_output_connected: Literal[False] = False
    output_isolation_confirmed: Literal[True] = True


class UnityBoundCommandV2(UnityBridgeCommandV1):
    schema_version: Literal[2] = 2
    unity_binding_id: UnityBindingId
    controller_component_binding_id: UnityBindingId
    action_key: CapabilityRef
    scene_manifest_sha256: Sha256Digest
    action_source_sha256: Sha256Digest


class UnityBoundResultV2(UnityBridgeResultV1):
    schema_version: Literal[2] = 2
    unity_binding_id: UnityBindingId
    controller_component_binding_id: UnityBindingId
    action_key: CapabilityRef
    scene_manifest_sha256: Sha256Digest
    action_source_sha256: Sha256Digest


class UnityInstalledActionV1(FleetModel):
    unity_binding_id: UnityBindingId
    controller_component_binding_id: UnityBindingId
    action_key: CapabilityRef
    capability: CapabilityRef
    operation: SafeId
    source_sha256: Sha256Digest
    legacy_step_id: SafeId


class UnityBridgeHeartbeatV1(FleetModel):
    schema_version: Literal[1] = 1
    scenario_id: SafeId
    updated_at: AwareDatetime
    ready: bool
    active_command_id: SafeId | None = None
    expected_step: SafeId | None = None
    observed_step: SafeId | None = None
    observed_phase: str = Field(min_length=1, max_length=128)
    current_run_id: SafeId | None = None
    lifecycle_state: Literal[
        "INITIALIZING", "READY", "RUNNING", "HELD", "ABORTED", "COMPLETE", "FAILED",
        "RESETTING"
    ] = "INITIALIZING"
    physical_output_connected: Literal[False] = False
    scene_inventory: UnitySceneInventoryV1 | None = None
    scene_manifest_sha256: Sha256Digest | None = None
    inventory_valid: bool | None = None
    inventory_errors: list[str] = Field(default_factory=list, max_length=512)
    action_bindings: list[UnityInstalledActionV1] = Field(default_factory=list, max_length=512)


class UnityBridgeControlV1(FleetModel):
    schema_version: Literal[1] = 1
    scenario_id: SafeId
    control_id: SafeId
    run_id: SafeId
    action: Literal["HOLD", "RESUME", "ABORT", "RESET"]
    issued_at: AwareDatetime
    not_valid_after: AwareDatetime
    execution_mode: Literal["SIMULATION"] = "SIMULATION"
    physical_output_connected: Literal[False] = False


class UnityBridgeControlResultV1(FleetModel):
    schema_version: Literal[1] = 1
    scenario_id: SafeId
    control_id: SafeId
    run_id: SafeId
    action: Literal["HOLD", "RESUME", "ABORT", "RESET"]
    state: Literal["ACKNOWLEDGED", "REJECTED"]
    detail: str = Field(min_length=1, max_length=2048)
    observed_phase: str = Field(min_length=1, max_length=128)
    completed_at: AwareDatetime
    physical_output_connected: Literal[False] = False
    output_isolation_confirmed: Literal[True] = True


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UnityBridgeError(f"cannot read valid bridge JSON: {path}") from exc


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json(payload)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)


class UnityScenarioFileBackend:
    """Synchronous simulation backend over an atomic local file protocol."""

    command_sink_present = False
    process_output_connected = False

    def __init__(
        self,
        *,
        scenario_id: str,
        bridge_root: str | Path,
        allowed_steps: dict[str, str],
        heartbeat_max_age_s: float,
        result_timeout_s: float,
        poll_interval_s: float = 0.05,
        abort_event: Event | None = None,
        dynamic_binding: UnityDynamicBindingV1 | None = None,
        dispatches: dict[str, ResolvedUnityAction] | None = None,
    ) -> None:
        self.scenario_id = scenario_id
        self.bridge_root = Path(bridge_root).expanduser().resolve()
        self.allowed_steps = dict(allowed_steps)
        self.heartbeat_max_age_s = heartbeat_max_age_s
        self.result_timeout_s = result_timeout_s
        self.poll_interval_s = poll_interval_s
        self.abort_event = abort_event
        self.dynamic_binding = dynamic_binding
        self.dispatches = dispatches or {}
        self.command_fence = nullcontext
        self.commands_dir = self.bridge_root / "commands"
        self.results_dir = self.bridge_root / "results"
        self.controls_dir = self.bridge_root / "controls"
        self.control_results_dir = self.bridge_root / "control-results"
        self.heartbeat_path = self.bridge_root / "heartbeat.json"

    def read_heartbeat(self) -> UnityBridgeHeartbeatV1:
        heartbeat = UnityBridgeHeartbeatV1.model_validate(
            _read_json(self.heartbeat_path)
        )
        if heartbeat.scenario_id != self.scenario_id:
            raise UnityBridgeError("Unity heartbeat belongs to a different scenario")
        age = datetime.now(timezone.utc) - heartbeat.updated_at
        if age.total_seconds() < -1.0 or age.total_seconds() > self.heartbeat_max_age_s:
            raise UnityBridgeError(
                f"Unity heartbeat is stale ({age.total_seconds():.2f}s)"
            )
        if self.dynamic_binding is not None:
            try:
                self.dynamic_binding.validate_runtime(heartbeat)
            except ValueError as exc:
                raise UnityBridgeError(str(exc)) from exc
        return heartbeat

    def assert_ready(self) -> UnityBridgeHeartbeatV1:
        heartbeat = self.read_heartbeat()
        if not heartbeat.ready:
            raise UnityBridgeError(
                f"Unity scenario is not ready (phase={heartbeat.observed_phase})"
            )
        return heartbeat

    def _wait_until_ready(self) -> UnityBridgeHeartbeatV1:
        timeout_s = min(10.0, self.result_timeout_s)
        deadline = time.monotonic() + timeout_s
        last_error: UnityBridgeError | None = None
        while time.monotonic() < deadline:
            if self.abort_event is not None and self.abort_event.is_set():
                raise UnityRunAborted("Simulation Run aborted while waiting for Unity readiness")
            try:
                return self.assert_ready()
            except UnityBridgeError as exc:
                last_error = exc
            time.sleep(self.poll_interval_s)
        raise UnityBridgeError(
            f"Unity did not become ready within {timeout_s:.1f}s: {last_error}"
        )

    def _command_for(
        self, request: ExecuteSkillRequestV1, *, require_ready: bool
    ) -> tuple[UnityBridgeCommandV1, Path]:
        if request.execution_mode != "SIMULATION":
            raise UnityBridgeError("Unity Scenario 1 accepts SIMULATION only")
        expected_capability = self.allowed_steps.get(request.step_id)
        if expected_capability != request.capability_ref:
            raise UnityBridgeError(
                f"step/capability is not allowlisted: "
                f"{request.step_id}/{request.capability_ref}"
            )
        if require_ready:
            self._wait_until_ready()
        elif self.dynamic_binding is not None:
            self.read_heartbeat()
        command_path = self.commands_dir / f"{request.command_id}.json"
        issued_at = datetime.now(timezone.utc)
        command_model = UnityBoundCommandV2 if self.dynamic_binding is not None else UnityBridgeCommandV1
        if command_path.exists():
            existing = command_model.model_validate(_read_json(command_path))
            issued_at = existing.issued_at
        binding_fields = {}
        action_parameters = dict(request.parameters)
        if self.dynamic_binding is not None:
            selected = self.dispatches.get(request.step_id)
            bound_asset_id = action_parameters.pop("bound_asset_id", None)
            if (selected is None or selected.agent_id != request.agent_id
                    or selected.binding.identity.asset_id != request.goal.asset_id
                    or bound_asset_id != request.goal.asset_id
                    or selected.action.capability != request.capability_ref
                    or canonical_json(selected.parameters) != canonical_json(action_parameters)):
                raise UnityBridgeError("request differs from resolved Unity asset/Action intent")
            binding_fields = dict(
                unity_binding_id=selected.binding.unity_binding_id,
                controller_component_binding_id=selected.binding.controller.component_binding_id,
                action_key=selected.action.action_ref,
                action_source_sha256=selected.action.script.sha256,
                scene_manifest_sha256=self.dynamic_binding.scene_sha256,
            )
        command = command_model(
            scenario_id=self.scenario_id,
            command_id=request.command_id,
            run_id=request.run_id,
            step_id=request.step_id,
            agent_id=request.agent_id,
            capability=request.capability_ref,
            parameters=action_parameters,
            issued_at=issued_at,
            not_valid_after=request.goal.not_valid_after,
            **binding_fields,
        )
        encoded = canonical_json(command.model_dump(mode="json"))
        with self.command_fence():
            if self.abort_event is not None and self.abort_event.is_set():
                raise UnityRunAborted("Simulation command emission is fenced")
            if command_path.exists():
                if canonical_json(_read_json(command_path)) != encoded:
                    raise UnityBridgeError("Unity command ID was reused with different content")
            else:
                _write_json_atomic(command_path, command.model_dump(mode="json"))
        return command, self.results_dir / f"{request.command_id}.json"

    def _wait_for_result(
        self, request: ExecuteSkillRequestV1, result_path: Path
    ) -> dict:
        deadline = time.monotonic() + self.result_timeout_s
        while time.monotonic() < deadline:
            if self.abort_event is not None and self.abort_event.is_set():
                raise UnityRunAborted(
                    f"Simulation Run aborted while waiting for {request.step_id}"
                )
            if result_path.exists():
                result_model = UnityBoundResultV2 if self.dynamic_binding is not None else UnityBridgeResultV1
                result = result_model.model_validate(_read_json(result_path))
                identity = (result.scenario_id, result.command_id, result.run_id, result.step_id)
                expected = (
                    self.scenario_id,
                    request.command_id,
                    request.run_id,
                    request.step_id,
                )
                if identity != expected:
                    raise UnityBridgeError("Unity result identity does not match its command")
                if self.dynamic_binding is not None:
                    selected = self.dispatches[request.step_id]
                    if (result.unity_binding_id != selected.binding.unity_binding_id
                            or result.controller_component_binding_id != selected.binding.controller.component_binding_id
                            or result.action_key != selected.action.action_ref
                            or result.action_source_sha256 != selected.action.script.sha256
                            or result.scene_manifest_sha256 != self.dynamic_binding.scene_sha256):
                        raise UnityBridgeError("Unity result asset/Action evidence differs from the command")
                if result.state == "ABORTED":
                    raise UnityRunAborted(result.detail)
                if result.state != "SUCCEEDED":
                    raise UnityBridgeError(
                        f"Unity rejected or failed {request.step_id}: {result.detail}"
                    )
                return result.model_dump(mode="json")
            time.sleep(self.poll_interval_s)
        raise UnityBridgeError(
            f"Unity result timed out after {self.result_timeout_s:.1f}s: "
            f"{request.step_id}"
        )

    def execute_skill(self, request: ExecuteSkillRequestV1) -> dict:
        _, result_path = self._command_for(request, require_ready=True)
        return self._wait_for_result(request, result_path)

    def recover_skill(self, request: ExecuteSkillRequestV1) -> dict:
        _, result_path = self._command_for(request, require_ready=False)
        return self._wait_for_result(request, result_path)

    def send_control(
        self,
        *,
        control_id: str,
        run_id: str,
        action: Literal["HOLD", "RESUME", "ABORT", "RESET"],
        timeout_s: float = 10.0,
    ) -> UnityBridgeControlResultV1:
        now = datetime.now(timezone.utc)
        control = UnityBridgeControlV1(
            scenario_id=self.scenario_id,
            control_id=control_id,
            run_id=run_id,
            action=action,
            issued_at=now,
            not_valid_after=now + timedelta(seconds=max(5.0, timeout_s)),
        )
        path = self.controls_dir / f"{control_id}.json"
        result_path = self.control_results_dir / f"{control_id}.json"
        if path.exists():
            existing = UnityBridgeControlV1.model_validate(_read_json(path))
            control = control.model_copy(
                update={
                    "issued_at": existing.issued_at,
                    "not_valid_after": existing.not_valid_after,
                }
            )
            if canonical_json(existing.model_dump(mode="json")) != canonical_json(
                control.model_dump(mode="json")
            ):
                raise UnityBridgeError("Unity control ID was reused with different content")
        else:
            _write_json_atomic(path, control.model_dump(mode="json"))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if result_path.exists():
                result = UnityBridgeControlResultV1.model_validate(
                    _read_json(result_path)
                )
                if (
                    result.scenario_id,
                    result.control_id,
                    result.run_id,
                    result.action,
                ) != (self.scenario_id, control_id, run_id, action):
                    raise UnityBridgeError("Unity control result identity mismatch")
                if result.state != "ACKNOWLEDGED":
                    raise UnityBridgeError(result.detail)
                return result
            time.sleep(self.poll_interval_s)
        raise UnityBridgeError(f"Unity control timed out after {timeout_s:.1f}s: {action}")


def load_scenario_configuration(
    config_path: str | Path,
    *,
    bridge_root: str | Path | None = None,
    result_timeout_s: float | None = None,
) -> UnityScenarioConfigV1:
    path = Path(config_path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if bridge_root is not None:
        raw["bridge_root"] = str(bridge_root)
    if result_timeout_s is not None:
        raw["result_timeout_s"] = result_timeout_s
    return UnityScenarioConfigV1.model_validate(raw)


def load_workflow(config_path: str | Path, config: UnityScenarioConfigV1) -> WorkflowDefinitionV2:
    config_file = Path(config_path).resolve()
    repository_root = config_file.parent.parent
    workflow_path = Path(config.workflow_path)
    if not workflow_path.is_absolute():
        workflow_path = repository_root / workflow_path
    return WorkflowDefinitionV2.model_validate(_read_json(workflow_path))


def allowed_step_capabilities(workflow: WorkflowDefinitionV2) -> dict[str, str]:
    return {
        step.step_id: step.capability
        for step in workflow.steps
        if step.kind in {"action", "compensation"}
    }


def _simulation_envelope(
    workflow: WorkflowDefinitionV2,
    *,
    now: datetime,
) -> ControlEnvelopeV1:
    capabilities = sorted(set(allowed_step_capabilities(workflow).values()))
    return ControlEnvelopeV1(
        envelope_id="iitp-scenario1-simulation-envelope",
        version=1,
        capabilities=capabilities,
        max_rate_hz=20.0,
        max_demand_age_ms=100.0,
        max_clock_uncertainty_ms=5.0,
        declared_time_to_safe_state_ms=100.0,
        max_delta_by_axis={"simulation-step": 1.0},
        approval={
            "state": "approved",
            "approver_id": "simulation-owner",
            "approved_at": now - timedelta(seconds=1),
        },
    )


def execute_scenario(
    *,
    config: UnityScenarioConfigV1,
    workflow: WorkflowDefinitionV2,
    backend: UnityScenarioFileBackend,
    runtime_dir: str | Path,
    run_id: str,
    now: datetime | None = None,
) -> dict:
    current = now or datetime.now(timezone.utc)
    if set(config.roles) != {role.role for role in workflow.roles}:
        raise ValueError("Unity role bindings must exactly match workflow roles")

    runtime = Path(runtime_dir).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    authority = SigningKey.generate("unity-central-authority")
    delegated = SigningKey.generate("unity-cell-orchestrator")
    keyring = VerificationKeyRing()
    for key in (authority, delegated):
        keyring.add(
            key_id=key.key_id,
            public_key_b64=key.public_key_b64(),
            valid_from=current - timedelta(hours=1),
            valid_until=current + timedelta(days=1),
        )

    role_index = {role.role: role for role in workflow.roles}
    agents: dict[str, UnitySimulationAgent] = {}
    for role, binding in config.roles.items():
        capabilities = set(role_index[role].selector.requires)
        existing = agents.get(binding.agent_id)
        if existing is not None:
            if existing.capabilities != capabilities:
                raise ValueError("one Unity Agent ID cannot declare conflicting role capabilities")
            continue
        agents[binding.agent_id] = UnitySimulationAgent(
            runtime / f"agent-{binding.agent_id}.sqlite3",
            agent_id=binding.agent_id,
            keyring=keyring,
            capabilities=capabilities,
            backend=backend,
        )

    envelope = _simulation_envelope(workflow, now=current)
    envelope_sha256 = payload_digest(envelope.model_dump(mode="json"))
    role_targets = {}
    for role, binding in config.roles.items():
        manifest_sha256 = hashlib.sha256(
            f"{config.scenario_id}:{binding.asset_id}:{binding.agent_id}".encode("utf-8")
        ).hexdigest()
        if getattr(backend, "dynamic_binding", None) is not None:
            manifest_sha256 = backend.dynamic_binding.scene_sha256
        role_targets[role] = [
            AgentTargetV1(
                asset_id=binding.asset_id,
                agent_id=binding.agent_id,
                manifest_sha256=manifest_sha256,
                authorization_id="unity-editor-simulation",
                control_envelope=envelope,
                control_envelope_sha256=envelope_sha256,
            )
        ]

    transport = SimulationTransport(agents)
    lease_seconds = max(900.0, config.result_timeout_s + 60.0)
    orchestrator = CellOrchestrator(
        runtime / "orchestrator.sqlite3",
        orchestrator_id="unity-scenario1-orchestrator",
        authority_signer=authority,
        delegated_signer=delegated,
        transport=transport,
        lease_ttl=timedelta(seconds=lease_seconds),
        lease_renewal_margin=timedelta(seconds=60),
    )
    orchestrator.create_run(
        OrchestratorRunCreateV1(
            run_id=run_id,
            workflow=workflow,
            execution_mode="SIMULATION",
            role_targets=role_targets,
        ),
        now=current,
    )
    result = orchestrator.tick(run_id, now=current)
    for _ in range(len(workflow.steps) + 2):
        if result["state"] not in {"ACTIVE", "RECONCILING"}:
            break
        result = orchestrator.tick(run_id, now=datetime.now(timezone.utc))
    else:
        raise RuntimeError("Unity orchestration did not reach a terminal or held state")
    summary = {
        "schema_version": 1,
        "scenario_id": config.scenario_id,
        "run": result,
        "dispatch_order": transport.dispatch_order,
        "physical_output_connected": False,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json_atomic(runtime / "run_summary.json", summary)
    return summary


def _default_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"iitp-s1-sim-{timestamp}-{uuid4().hex[:8]}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Unity IITP Scenario 1 through the SANG Cell Orchestrator"
    )
    parser.add_argument(
        "--config",
        default="config/unity_scenario1_simulation.yaml",
    )
    parser.add_argument("--bridge-root")
    parser.add_argument("--result-timeout-s", type=float)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--runtime-root",
        default="runtime/unity-scenario1-orchestrator",
    )
    args = parser.parse_args()

    run_id = args.run_id or _default_run_id()
    config = load_scenario_configuration(
        args.config,
        bridge_root=args.bridge_root,
        result_timeout_s=args.result_timeout_s,
    )
    workflow = load_workflow(args.config, config)
    backend = UnityScenarioFileBackend(
        scenario_id=config.scenario_id,
        bridge_root=config.bridge_root,
        allowed_steps=allowed_step_capabilities(workflow),
        heartbeat_max_age_s=config.heartbeat_max_age_s,
        result_timeout_s=config.result_timeout_s,
    )
    backend.assert_ready()
    runtime_dir = Path(args.runtime_root) / run_id
    summary = execute_scenario(
        config=config,
        workflow=workflow,
        backend=backend,
        runtime_dir=runtime_dir,
        run_id=run_id,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
