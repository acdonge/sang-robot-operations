"""Isaac executor with V1 file-wire compatibility, not a Unity executor.

The runtime must check worker-permit.json before dispatch and every motion tick,
using a monotonic expiry capped at two seconds. Re-reading the same payload must
not extend that expiry. Missing/invalid permission freezes motion without resume.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
from pathlib import Path
import time
from typing import Literal

from pydantic import AwareDatetime, model_validator

from fleet_control_plane.models import FleetModel, SafeId
from fleet_control_plane.orchestration_models import Sha256Digest
from fleet_control_plane.unity_scenario_orchestrator import (
    UnityBridgeError,
    UnityBridgeControlV1,
    UnityBridgeHeartbeatV1,
    UnityRoleBindingV1,
    UnityRunAborted,
    UnityScenarioFileBackend,
    _read_json,
    _write_json_atomic,
)


PERMIT_TTL_SECONDS = 2.0
PERMIT_REFRESH_SECONDS = 0.5

# Fixed Scenario 1 intent, shared by profile validation and per-command routing.
ISAAC_SCENARIO1_STEPS = (
    ("mobile_delivery", "mobile-carrier", "simulate-material-delivery@1", "deliver-base-plate"),
    ("omy_pre_pick_place", "omy-material-handler", "simulate-pick-place@1", "place-on-waam-table"),
    ("rb5_waam", "rb5-waam-path", "simulate-waam-path@1", "follow-simulated-waam-path"),
    ("omy_post_pick_place", "omy-material-handler", "simulate-pick-place@1", "return-to-carrier"),
    ("mobile_return", "mobile-carrier", "simulate-material-return@1", "return-with-base-plate"),
)
ISAAC_SCENARIO1_BINDINGS = {
    "mobile-carrier": ("isaac-scenario1-carrier", "isaac-scenario1-carrier-agent"),
    "omy-material-handler": ("isaac-scenario1-omy", "isaac-scenario1-omy-agent"),
    "rb5-waam-path": ("isaac-scenario1-rb5", "isaac-scenario1-rb5-agent"),
}


class IsaacRuntimeIdentityV1(FleetModel):
    schema_version: Literal[1]
    runtime_kind: Literal["isaac-sim-executor"]
    profile_id: Literal["rb5", "omy", "scenario1"]
    scene_sha256: Sha256Digest
    session_id: SafeId
    updated_at: AwareDatetime
    physical_output_connected: Literal[False]


class IsaacWorkerPermitV1(FleetModel):
    schema_version: Literal[1] = 1
    runtime_kind: Literal["isaac-sim-executor"] = "isaac-sim-executor"
    worker_id: SafeId
    run_id: SafeId
    profile_id: Literal["rb5", "omy", "scenario1"]
    scene_sha256: Sha256Digest
    profile_sha256: Sha256Digest
    session_id: SafeId
    issued_at: AwareDatetime
    not_valid_after: AwareDatetime
    physical_output_connected: Literal[False] = False

    @model_validator(mode="after")
    def bounded_validity(self) -> "IsaacWorkerPermitV1":
        if not 0 < (self.not_valid_after - self.issued_at).total_seconds() <= PERMIT_TTL_SECONDS:
            raise ValueError("Isaac Worker permit validity must be within two seconds")
        return self


@contextmanager
def _isaac_errors():
    # Historical model/exception names are retained only for wire compatibility.
    try:
        yield
    except UnityRunAborted as exc:
        raise UnityRunAborted(str(exc).replace("Unity", "Isaac")) from exc
    except UnityBridgeError as exc:
        raise UnityBridgeError(str(exc).replace("Unity", "Isaac")) from exc


class IsaacSimulationFileBackend(UnityScenarioFileBackend):
    """Fixed virtual-asset file backend with independent runtime identity."""

    def __init__(self, *, profile_id: str, scene_sha256: str, profile_sha256: str,
                 asset_id: str | None = None, agent_id: str | None = None,
                 bindings: dict[str, UnityRoleBindingV1] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        if (self.bridge_root.parent != Path("/tmp")
                or not self.bridge_root.name.startswith("sang-isaac-")):
            raise ValueError("Isaac bridge root must remain below /tmp/sang-isaac-*")
        if self.dynamic_binding is not None:
            raise ValueError("Isaac uses only the fixed V1 wire protocol")
        if profile_id == "scenario1":
            if self.scenario_id != "isaac-scenario1":
                raise ValueError("Isaac Scenario 1 requires its fixed scenario identity")
            if not 0 < self.result_timeout_s <= 420:
                raise ValueError("Isaac Scenario 1 result timeout must be within 420 seconds")
            if asset_id is not None or agent_id is not None or bindings is None:
                raise ValueError("Isaac Scenario 1 requires exact role bindings, not a single asset")
            self.bindings = {role: UnityRoleBindingV1.model_validate(binding).model_copy()
                             for role, binding in bindings.items()}
            if {role: (binding.asset_id, binding.agent_id)
                    for role, binding in self.bindings.items()} != ISAAC_SCENARIO1_BINDINGS:
                raise ValueError("Isaac Scenario 1 requires its exact role/asset/agent bindings")
            self.fixed_steps = {step: (role, capability, operation)
                                for step, role, capability, operation in ISAAC_SCENARIO1_STEPS}
        else:
            if profile_id not in {"rb5", "omy"}:
                raise ValueError("Isaac requires an installed fixed profile")
            if bindings is not None or asset_id is None or agent_id is None:
                raise ValueError("Isaac pilot requires its single asset and agent")
            self.bindings = {"virtual-robot": UnityRoleBindingV1(asset_id=asset_id, agent_id=agent_id)}
            self.fixed_steps = {
                "move_offset": ("virtual-robot", "simulate-bounded-joint@1", "move-offset"),
                "return_ready": ("virtual-robot", "simulate-bounded-joint@1", "return-ready"),
            }
        if self.allowed_steps != {step: capability for step, (_, capability, _) in self.fixed_steps.items()}:
            raise ValueError("Isaac requires the fixed profile step/capability allowlist")
        self.profile_id = profile_id
        self.scene_sha256 = scene_sha256
        self.profile_sha256 = profile_sha256
        self.asset_id = asset_id
        self.agent_id = agent_id
        self.session_id: str | None = None
        self.identity_path = self.bridge_root / "runtime-identity.json"
        self.permit_path = self.bridge_root / "worker-permit.json"
        self._last_permit: dict | None = None
        self.command_fence = self._missing_fence

    @contextmanager
    def _missing_fence(self):
        raise UnityRunAborted("Isaac command emission requires a live Worker lease fence")
        yield

    def read_runtime_identity(self) -> IsaacRuntimeIdentityV1:
        with _isaac_errors():
            raw = _read_json(self.identity_path)
        if raw.get("physical_output_connected") is not False:
            raise UnityBridgeError("Isaac runtime identity must confirm disconnected physical output")
        identity = IsaacRuntimeIdentityV1.model_validate(raw)
        if (identity.profile_id != self.profile_id or identity.scene_sha256 != self.scene_sha256):
            raise UnityBridgeError("Isaac runtime identity differs from the exact installed profile")
        age = (datetime.now(timezone.utc) - identity.updated_at).total_seconds()
        if age < -1.0 or age > self.heartbeat_max_age_s:
            raise UnityBridgeError("Isaac runtime identity is stale or future-dated")
        if self.session_id is not None and identity.session_id != self.session_id:
            raise UnityBridgeError("Isaac runtime session changed during Worker execution")
        self.session_id = identity.session_id
        return identity

    def read_heartbeat(self) -> UnityBridgeHeartbeatV1:
        self.read_runtime_identity()
        with _isaac_errors():
            return super().read_heartbeat()

    def assert_ready(self) -> UnityBridgeHeartbeatV1:
        with _isaac_errors():
            return super().assert_ready()

    @contextmanager
    def _permit_lock(self):
        # Serialize conditional cleanup with another Worker taking over this root.
        with (self.bridge_root / ".worker-permit.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def refresh_permit(self, *, worker_id: str, run_id: str, lease_deadline: float) -> None:
        """Called only inside the durable dispatch fence, before command emission."""
        with self._permit_lock():
            self.read_heartbeat()
            remaining = min(PERMIT_TTL_SECONDS, lease_deadline - time.monotonic())
            if remaining <= 0 or (self.abort_event is not None and self.abort_event.is_set()):
                raise UnityRunAborted("Isaac Worker permission expired")
            now = datetime.now(timezone.utc)
            permit = IsaacWorkerPermitV1(
                worker_id=worker_id, run_id=run_id, profile_id=self.profile_id,
                scene_sha256=self.scene_sha256, profile_sha256=self.profile_sha256,
                session_id=self.session_id, issued_at=now,
                not_valid_after=now + timedelta(seconds=remaining),
            ).model_dump(mode="json")
            _write_json_atomic(self.permit_path, permit)
            self._last_permit = permit

    def revoke_permit(self) -> None:
        if self._last_permit is None:
            return
        with self._permit_lock():
            if self.permit_path.exists() and _read_json(self.permit_path) == self._last_permit:
                self.permit_path.unlink()
            self._last_permit = None

    def _command_for(self, request, *, require_ready: bool):
        expected = self.fixed_steps.get(request.step_id)
        if expected is None:
            raise UnityBridgeError("Isaac step is not in the fixed profile")
        role, capability, operation = expected
        binding = self.bindings[role]
        if (request.capability_ref != capability or request.agent_id != binding.agent_id
                or request.goal.asset_id != binding.asset_id
                or request.parameters != {"operation": operation, "bound_asset_id": binding.asset_id}):
            raise UnityBridgeError("Isaac request differs from the fixed step role/asset/agent/operation")
        self.read_heartbeat()
        with _isaac_errors():
            return super()._command_for(request, require_ready=require_ready)

    def _wait_for_result(self, request, result_path):
        with _isaac_errors():
            result = super()._wait_for_result(request, result_path)
        self.read_heartbeat()
        return result

    def send_control(self, *, control_id, run_id, action, timeout_s=10.0):
        self.read_heartbeat()
        # STOP controls and state-only RESET do not authorize motion. RESUME does.
        if action == "RESUME":
            with self.command_fence():
                path = self.controls_dir / f"{control_id}.json"
                if not path.exists():
                    now = datetime.now(timezone.utc)
                    control = UnityBridgeControlV1(
                        scenario_id=self.scenario_id, control_id=control_id,
                        run_id=run_id, action=action, issued_at=now,
                        not_valid_after=now + timedelta(seconds=max(5.0, timeout_s)),
                    )
                    _write_json_atomic(path, control.model_dump(mode="json"))
        with _isaac_errors():
            return super().send_control(
                control_id=control_id, run_id=run_id, action=action, timeout_s=timeout_s,
            )
