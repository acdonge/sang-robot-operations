"""Bounded virtual-device state machine, independent of Isaac imports."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from uuid import uuid4

from fleet_control_plane.unity_scenario_orchestrator import (
    UnityBridgeCommandV1, UnityBridgeControlV1,
)
from .contract import (
    AGENT_ID, ASSET_ID, AUTHORITY_TTL_S, CAPABILITY, DELTA_RAD, MAX_ERROR_RAD,
    MAX_MOTION_S, MOTION_DURATION_S, OPERATIONS, PROFILE_ID, RUNTIME_KIND,
    SCENARIO_ID, SCENE_SHA256, atomic_json,
)


def utc_string(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    if path.stat().st_size > 65536:
        raise ValueError("bridge message exceeds limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("bridge message must be an object")
    return data


def digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class PilotExecutor:
    scenario_id = SCENARIO_ID
    runtime_kind = RUNTIME_KIND
    profile_id = PROFILE_ID
    scene_sha256 = SCENE_SHA256
    steps = tuple(OPERATIONS)
    measurement_size = 6
    envelope_error = "Measured state left the bounded pilot envelope"

    def __init__(self, root: Path, profile_sha256: str):
        self.root = root
        self.profile_sha256 = profile_sha256
        self.session_id = uuid4().hex
        self.epoch = 0
        self.state = "READY"
        self.run_id = None
        self.active = None
        self.next_step = self.steps[0]
        self.target = [0.0] * self.measurement_size
        self.start = list(self.target)
        self.motion_start_sim = 0.0
        self.motion_start_mono = 0.0
        self.permit_fingerprint = None
        self.permit_deadline = 0.0
        self.permit_issued_highwater = float("-inf")
        self.command_deadline = 0.0
        self.initialized_target = False
        self.last_measured = list(self.target)
        self.last_sim = 0.0
        self.journal_path = root / "executor-journal.json"
        self.journal = {"commands": {}, "controls": {}}
        for directory in ("commands", "results", "controls", "control-results"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        if self.journal_path.exists():
            self.journal = json.loads(self.journal_path.read_text())
            for command_id, row in self.journal["commands"].items():
                if row["result"] is None:
                    command = UnityBridgeCommandV1.model_validate(row["payload"])
                    row["result"] = self._result(command, "ABORTED", "Executor restarted; never replayed", time.time())
                atomic_json(root / "results" / f"{command_id}.json", row["result"])
            for control_id, row in self.journal["controls"].items():
                atomic_json(root / "control-results" / f"{control_id}.json", row["result"])
            # Explicit reset required after any previous runtime, even if it was idle.
            self.run_id = self.journal.get("last_run_id")
            self.state = "ABORTED" if self.run_id else "READY"
        self.journal.setdefault("cancelled_runs", [])
        self._persist()

    def _persist(self):
        self.journal["last_run_id"] = self.run_id
        atomic_json(self.journal_path, self.journal)

    def _result(self, command, state, detail, now):
        return dict(schema_version=1, scenario_id=self.scenario_id,
                    command_id=command.command_id, run_id=command.run_id,
                    step_id=command.step_id, state=state, detail=detail,
                    observed_phase=state, completed_at=utc_string(now),
                    physical_output_connected=False, output_isolation_confirmed=True)

    def permit_valid(self, run_id: str, now: float, mono: float) -> bool:
        try:
            data = read_json(self.root / "worker-permit.json")
            expected = dict(schema_version=1, runtime_kind=self.runtime_kind,
                            profile_id=self.profile_id, scene_sha256=self.scene_sha256,
                            profile_sha256=self.profile_sha256, session_id=self.session_id,
                            run_id=run_id, physical_output_connected=False)
            if any(type(data.get(k)) is not type(v) or data.get(k) != v for k, v in expected.items()):
                return False
            if not isinstance(data.get("worker_id"), str) or not data["worker_id"]:
                return False
            issued = datetime.fromisoformat(data["issued_at"].replace("Z", "+00:00"))
            expiry = datetime.fromisoformat(data["not_valid_after"].replace("Z", "+00:00"))
            if issued.tzinfo is None or expiry.tzinfo is None:
                return False
            lifetime = (expiry - issued).total_seconds()
            remaining = expiry.timestamp() - now
            if not 0 < lifetime <= AUTHORITY_TTL_S + 0.001 or issued.timestamp() > now + 0.1 or remaining <= 0:
                return False
            fingerprint = digest(data)
            if fingerprint != self.permit_fingerprint:
                if issued.timestamp() <= self.permit_issued_highwater:
                    return False
                self.permit_issued_highwater = issued.timestamp()
                self.permit_fingerprint = fingerprint
                self.permit_deadline = mono + min(remaining, AUTHORITY_TTL_S)
            return mono < self.permit_deadline
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def _finish(self, state, detail, now, measured):
        command = self.active
        self.target = list(measured)
        self.active = None
        if command is None:
            return
        result = self._result(command, state, detail, now)
        self.journal["commands"][command.command_id]["result"] = result
        self.state = state
        if state == "SUCCEEDED":
            index = self.steps.index(command.step_id) + 1
            self.next_step = self.steps[index] if index < len(self.steps) else None
            self.state = "READY" if self.next_step else "COMPLETE"
        self._persist()
        atomic_json(self.root / "results" / f"{command.command_id}.json", result)

    def _registered_command(self, command):
        parameters = dict(command.parameters)
        bound_asset = parameters.pop("bound_asset_id", None)
        return (command.scenario_id == self.scenario_id and command.agent_id == AGENT_ID
                and command.capability == CAPABILITY and bound_asset == ASSET_ID
                and parameters == {"operation": OPERATIONS.get(command.step_id)}
                and command.step_id in OPERATIONS)

    def _admission_error(self, command, measured):
        return None

    def _motion_budget(self, step_id):
        return MAX_MOTION_S

    def _begin_motion(self, measured, sim_time):
        self.start = list(measured)
        self.motion_start_sim = sim_time

    def _hold_motion(self, measured, sim_time):
        self.target = list(measured)

    def _resume_motion(self, measured, sim_time):
        self.start = list(measured)
        self.motion_start_sim = sim_time

    def _reset_motion(self, measured):
        self.target = list(measured)

    def _measurement_in_bounds(self, measured):
        return (-0.03 <= measured[0] <= DELTA_RAD + 0.03
                and all(abs(x) <= 0.03 for x in measured[1:]))

    def _advance_motion(self, measured, sim_time, now):
        goal = [DELTA_RAD if self.active.step_id == "move_offset" else 0.0, 0., 0., 0., 0., 0.]
        fraction = min(1.0, max(0., (sim_time - self.motion_start_sim) / MOTION_DURATION_S))
        blend = fraction * fraction * (3.0 - 2.0 * fraction)
        self.target = [a + (b - a) * blend for a, b in zip(self.start, goal)]
        error = max(abs(a - b) for a, b in zip(measured, goal))
        if fraction == 1 and error <= MAX_ERROR_RAD:
            self._finish("SUCCEEDED", f"Measured goal reached; max error {error:.6f} rad", now, measured)
            self.target = goal

    def _accept(self, path, measured, sim_time, now, mono):
        try:
            payload = read_json(path)
            command = UnityBridgeCommandV1.model_validate(payload)
            if path.stem != command.command_id:
                return
        except (OSError, ValueError, TypeError):
            return
        fingerprint = digest(payload)
        previous = self.journal["commands"].get(command.command_id)
        if previous is not None:
            # A reused ID never moves again, even when its payload was changed.
            return
        if len(self.journal["commands"]) >= 10000:
            self.state = "FAILED"
            return
        reason = None
        if not self._registered_command(command):
            reason = "Unregistered simulation asset/action/parameters"
        elif (command.not_valid_after.timestamp() <= now
              or not -0.1 <= now-command.issued_at.timestamp() <= 2.0
              or (command.not_valid_after-command.issued_at).total_seconds() > 900):
            reason = "Command expired or issued in the future"
        elif command.run_id in self.journal["cancelled_runs"]:
            reason = "Run was cancelled before command admission"
        elif self.state != "READY" or self.active or command.step_id != self.next_step:
            reason = "Executor not ready for this step; explicit Reset may be required"
        elif self.run_id not in (None, command.run_id):
            reason = "Another Run owns the executor"
        elif not self.permit_valid(command.run_id, now, mono):
            reason = "Worker execution permit missing, mismatched or expired"
        else:
            reason = self._admission_error(command, measured)
        row = {"sha256": fingerprint, "payload": payload, "result": None}
        self.journal["commands"][command.command_id] = row
        if reason:
            row["result"] = self._result(command, "REJECTED", reason, now)
            self._persist()
            atomic_json(self.root / "results" / path.name, row["result"])
            return
        self.active = command
        self.run_id = command.run_id
        self.state = "RUNNING"
        self._begin_motion(measured, sim_time)
        self.motion_start_mono = mono
        self.command_deadline = mono + min(self._motion_budget(command.step_id),
                                           command.not_valid_after.timestamp()-now)
        self._persist()  # Acceptance is durable before any drive target changes.

    def _control(self, path, measured, sim_time, now, mono):
        try:
            payload = read_json(path)
            control = UnityBridgeControlV1.model_validate(payload)
            if path.stem != control.control_id:
                return
        except (OSError, ValueError, TypeError):
            return
        if control.control_id in self.journal["controls"]:
            return
        acknowledged = False
        detail = "Control rejected for current state or Run"
        valid_time = (control.scenario_id == self.scenario_id
                 and control.issued_at.timestamp() <= now + 0.1
                 and control.not_valid_after.timestamp() > now)
        valid = valid_time and self.run_id == control.run_id
        if (valid_time and self.run_id in (None, control.run_id) and control.action == "ABORT"
                and self.state != "COMPLETE"):
            if control.run_id not in self.journal["cancelled_runs"]:
                self.journal["cancelled_runs"].append(control.run_id)
            self._finish("ABORTED", "Explicit simulation Abort", now, measured)
            self.run_id = control.run_id
            self.state = "ABORTED"
            self.target = list(measured)
            acknowledged = True
        elif valid and control.action == "HOLD" and self.state == "RUNNING":
            self.state = "HELD"
            self._hold_motion(measured, sim_time)
            acknowledged = True
        elif (valid and control.action == "RESUME" and self.state == "HELD"
              and self.active and self.permit_valid(control.run_id, now, mono)):
            self.state = "RUNNING"
            self._resume_motion(measured, sim_time)
            acknowledged = True
        elif valid and control.action == "RESET" and self.active is None and self.state in ("COMPLETE", "FAILED", "ABORTED"):
            # Reset clears orchestration state, not the physical pose. The next
            # bounded action starts from the measured held pose, never teleports.
            self.epoch += 1
            self.run_id = None
            self.state, self.next_step = "READY", self.steps[0]
            self._reset_motion(measured)
            acknowledged = True
        if acknowledged:
            detail = f"Isaac simulation {control.action}; no physical output"
        result = dict(schema_version=1, scenario_id=self.scenario_id,
                      control_id=control.control_id, run_id=control.run_id,
                      action=control.action, state="ACKNOWLEDGED" if acknowledged else "REJECTED",
                      detail=detail, observed_phase=self.state, completed_at=utc_string(now),
                      physical_output_connected=False, output_isolation_confirmed=True)
        self.journal["controls"][control.control_id] = {"sha256": digest(payload), "result": result}
        self._persist()
        atomic_json(self.root / "control-results" / path.name, result)

    def tick(self, measured, sim_time, *, now=None, mono=None):
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono
        if len(measured) != self.measurement_size or any(not math.isfinite(x) for x in measured):
            raise RuntimeError("Invalid measured joint state; simulation must stop")
        self.last_measured = list(measured)
        if not self.initialized_target:
            self.target = list(measured)
            self.initialized_target = True
        if sim_time < self.last_sim:
            self._finish("ABORTED", "Unexpected simulation clock regression", now, measured)
            self.state = "ABORTED"
        self.last_sim = sim_time
        if not self._measurement_in_bounds(measured):
            self._finish("FAILED", self.envelope_error, now, measured)
            self.state = "FAILED"
            raise RuntimeError(self.envelope_error)
        controls = (p for p in sorted((self.root / "controls").glob("*.json"))
                    if p.stem not in self.journal["controls"])
        commands = (p for p in sorted((self.root / "commands").glob("*.json"))
                    if p.stem not in self.journal["commands"])
        from itertools import islice
        for path in islice(controls, 1024):
            self._control(path, measured, sim_time, now, mono)
        for path in islice(commands, 1024):
            self._accept(path, measured, sim_time, now, mono)
        if self.active:
            if not self.permit_valid(self.run_id, now, mono):
                self._finish("ABORTED", "Worker permit lost; explicit Reset required", now, measured)
            elif (now >= self.active.not_valid_after.timestamp()
                  or mono >= self.command_deadline):
                self._finish("FAILED", "Wall-clock motion deadline exceeded", now, measured)
            elif self.state == "RUNNING":
                self._advance_motion(measured, sim_time, now)
        return list(self.target)

    def shutdown(self, now=None):
        self._finish("ABORTED", "Executor stopped; command will not replay",
                     time.time() if now is None else now, self.last_measured)
        self.state = "ABORTED"

    def heartbeat(self, now=None):
        now = time.time() if now is None else now
        return dict(schema_version=1, scenario_id=self.scenario_id, updated_at=utc_string(now),
                    ready=self.state == "READY", active_command_id=self.active.command_id if self.active else None,
                    expected_step=self.next_step, observed_step=self.active.step_id if self.active else None,
                    observed_phase=self.state, current_run_id=self.run_id, lifecycle_state=self.state,
                    physical_output_connected=False)
