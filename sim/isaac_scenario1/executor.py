"""Pinned Scenario 1 motion hooks on the pilot's durable guarded executor.

The trusted scene runner supplies MotionProgram, never command-provided paths.
Measurements/setpoints are OMY7 + RB5 six joints + carrier ROS position XYZ.
"""

import math
from pathlib import Path
import time
from typing import Protocol

from isaac_twin.executor import PilotExecutor


ROOT = Path("/tmp/sang-isaac-scenario1")
RUNTIME_KIND = "isaac-sim-executor"
SCENARIO_ID = "isaac-scenario1"
PROFILE_ID = "scenario1"
# Matches contracts/tasks/iitp-scenario1-orchestrated.v2.json, not user metadata.
ACTIONS = {
    "mobile_delivery": ("carrier", "simulate-material-delivery@1", "deliver-base-plate"),
    "omy_pre_pick_place": ("omy", "simulate-pick-place@1", "place-on-waam-table"),
    "rb5_waam": ("rb5", "simulate-waam-path@1", "follow-simulated-waam-path"),
    "omy_post_pick_place": ("omy", "simulate-pick-place@1", "return-to-carrier"),
    "mobile_return": ("carrier", "simulate-material-return@1", "return-with-base-plate"),
}


class MotionProgram(Protocol):
    def duration(self, step_id: str) -> float: ...
    def sample(self, step_id: str, elapsed: float) -> list[float]: ...
    def phase(self, step_id: str, elapsed: float) -> str: ...
    def start(self, step_id: str) -> list[float]: ...
    def end(self, step_id: str) -> list[float]: ...
    def valid_measurement(self, measured: list[float]) -> bool: ...


class ScenarioExecutor(PilotExecutor):
    scenario_id = SCENARIO_ID
    runtime_kind = RUNTIME_KIND
    profile_id = PROFILE_ID
    steps = tuple(ACTIONS)
    measurement_size = 16
    envelope_error = "Measured state left the pinned Scenario 1 envelope"

    def __init__(self, root: Path, profile_sha256: str, scene_sha256: str,
                 program: MotionProgram):
        self.scene_sha256 = scene_sha256
        self.program = program
        self.durations = {step: float(program.duration(step)) for step in self.steps}
        if any(not math.isfinite(value) or value <= 0 for value in self.durations.values()):
            raise ValueError("Motion durations must be finite and positive")
        self.elapsed_s = 0.0
        self.phase = "READY"
        self.world_ready = False
        self.world_detail = "Scene object preconditions have not been checked"
        super().__init__(root, profile_sha256)

    def set_world_ready(self, ready: bool, detail: str):
        """Supply admission/completion predicates for exactly one tick."""
        self.world_ready = ready is True
        self.world_detail = detail

    def tick(self, measured, sim_time, *, now=None, mono=None):
        try:
            return super().tick(measured, sim_time, now=now, mono=mono)
        finally:
            self.set_world_ready(False, "Scene object predicates were not refreshed for this tick")

    def fail(self, reason: str, measured=None):
        """Latch a runtime world-predicate failure at the last measured pose."""
        self.set_world_ready(False, reason)
        if measured is not None and self._measurement_in_bounds(measured):
            self.last_measured = list(measured)
        self._finish("ABORTED", reason, time.time(), self.last_measured)
        self.target = list(self.last_measured)
        self.state = "ABORTED"
        self._persist()

    def _registered_command(self, command):
        action = ACTIONS.get(command.step_id)
        if action is None:
            return False
        name, capability, operation = action
        asset = f"isaac-scenario1-{name}"
        return (command.scenario_id == self.scenario_id
                and command.agent_id == f"{asset}-agent"
                and command.capability == capability
                and command.parameters == {"operation": operation, "bound_asset_id": asset})

    def _measurement_in_bounds(self, measured):
        return (len(measured) == 16 and all(math.isfinite(x) for x in measured)
                and all(-3.15 <= x <= 3.15 for i, x in enumerate(measured[:13]) if i != 6)
                and -0.03 <= measured[6] <= 1.14 + 0.03
                and self.program.valid_measurement(measured))

    @staticmethod
    def _pose_matches(measured, expected, joint_tolerance, carrier_tolerance):
        return all(abs(a - b) <= (joint_tolerance if i < 13 else carrier_tolerance)
                   for i, (a, b) in enumerate(zip(measured, expected)))

    def _admission_error(self, command, measured):
        if not self.world_ready:
            return f"Scene object preconditions not ready: {self.world_detail}"
        start = self.program.start(command.step_id)
        if not self._measurement_in_bounds(start):
            return "Recorded start is outside the pinned measurement envelope"
        if not self._pose_matches(measured, start, 0.05, 0.01):
            return "Measured pose does not match recorded start; explicit scene recovery required"
        return None

    def _motion_budget(self, step_id):
        return min(self.durations[step_id] * 2 + 20, 420)

    def _begin_motion(self, measured, sim_time):
        super()._begin_motion(measured, sim_time)
        self.elapsed_s = 0.0
        self.phase = self.program.phase(self.active.step_id, self.elapsed_s)

    def _update_elapsed(self, sim_time):
        step = self.active.step_id
        self.elapsed_s = min(self.durations[step], max(0.0, sim_time - self.motion_start_sim))
        self.phase = self.program.phase(step, self.elapsed_s)

    def _hold_motion(self, measured, sim_time):
        super()._hold_motion(measured, sim_time)
        self._update_elapsed(sim_time)

    def _resume_motion(self, measured, sim_time):
        # Shift only the simulation origin, never the captured wall deadline.
        self.motion_start_sim = sim_time - self.elapsed_s

    def _reset_motion(self, measured):
        super()._reset_motion(measured)
        self.elapsed_s = 0.0
        self.phase = "READY"
        self.set_world_ready(False, "Scene object preconditions must be rechecked after Reset")

    def _advance_motion(self, measured, sim_time, now):
        self._update_elapsed(sim_time)
        step = self.active.step_id
        goal = list(self.program.end(step))
        target = list(self.program.sample(step, self.elapsed_s))
        if not self._measurement_in_bounds(goal) or not self._measurement_in_bounds(target):
            self._finish("FAILED", "Recorded setpoint left the pinned envelope", now, measured)
            raise RuntimeError("Recorded setpoint left the pinned envelope")
        self.target = target
        if (self.elapsed_s >= self.durations[step] and self.world_ready
                and self._pose_matches(measured, goal, 0.03, 0.005)):
            self._finish("SUCCEEDED", "All 16 measured axes and scene object predicates reached", now, measured)
            self.target = goal
