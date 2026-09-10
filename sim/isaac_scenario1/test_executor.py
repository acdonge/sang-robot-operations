"""Guarded Scenario 1 tests use only a fake program and temporary bridge files."""

import json
import math
from pathlib import Path

import pytest

from isaac_twin import executor as pilot_module
from isaac_twin.contract import atomic_json
from isaac_twin.executor import PilotExecutor, utc_string
from isaac_scenario1.executor import ACTIONS, ROOT, ScenarioExecutor


NOW = 1788979800.0
PROFILE_SHA = "a" * 64
SCENE_SHA = "b" * 64


class FakeProgram:
    def __init__(self, duration=1.0):
        self.seconds = duration
        home = [0.] * 13 + [-.818, 2.5, 0.]
        dock = home[:13] + [-.818, 1.25, 0.]
        table = [.4] * 6 + [.6] + dock[7:]
        poses = [home, dock, table, table, dock, home]
        self.curves = {step: (poses[i], poses[i + 1]) for i, step in enumerate(ACTIONS)}
        self.samples = []

    def duration(self, step_id):
        return self.seconds

    def start(self, step_id):
        return list(self.curves[step_id][0])

    def end(self, step_id):
        return list(self.curves[step_id][1])

    def sample(self, step_id, elapsed):
        self.samples.append((step_id, elapsed))
        fraction = min(1., max(0., elapsed / self.seconds))
        values = [a + (b - a) * fraction
                  for a, b in zip(self.start(step_id), self.end(step_id))]
        if step_id == "rb5_waam":
            values[7:13] = [.4 * math.sin(math.pi * fraction)] * 6
        return values

    def phase(self, step_id, elapsed):
        return f"{step_id}:" + ("transfer" if elapsed >= self.seconds / 2 else "approach")

    def valid_measurement(self, values):
        return (abs(values[13] + .818) <= .05 and 1.2 <= values[14] <= 2.55
                and abs(values[15]) <= .03)


@pytest.fixture
def engine(tmp_path):
    return ScenarioExecutor(tmp_path, PROFILE_SHA, SCENE_SHA, FakeProgram())


def permit(engine, elapsed=0., run_id="run-a", **changes):
    payload = dict(schema_version=1, runtime_kind="isaac-sim-executor", worker_id="worker-a",
                   run_id=run_id, profile_id="scenario1", scene_sha256=SCENE_SHA,
                   profile_sha256=PROFILE_SHA, session_id=engine.session_id,
                   issued_at=utc_string(NOW + elapsed), not_valid_after=utc_string(NOW + elapsed + 2),
                   physical_output_connected=False)
    atomic_json(engine.root / "worker-permit.json", {**payload, **changes})


def command(engine, step="mobile_delivery", ident="command-a", elapsed=0., **changes):
    name, capability, operation = ACTIONS.get(step, ACTIONS["mobile_delivery"])
    payload = dict(schema_version=1, scenario_id="isaac-scenario1", command_id=ident,
                   run_id="run-a", step_id=step, agent_id=f"isaac-scenario1-{name}-agent",
                   capability=capability,
                   parameters={"operation": operation, "bound_asset_id": f"isaac-scenario1-{name}"},
                   issued_at=utc_string(NOW + elapsed), not_valid_after=utc_string(NOW + elapsed + 900),
                   execution_mode="SIMULATION", physical_output_connected=False)
    atomic_json(engine.root / "commands" / f"{ident}.json", {**payload, **changes})


def control(engine, action, ident="control-a", elapsed=0., **changes):
    payload = dict(schema_version=1, scenario_id="isaac-scenario1", control_id=ident,
                   run_id="run-a", action=action, issued_at=utc_string(NOW + elapsed),
                   not_valid_after=utc_string(NOW + elapsed + 10),
                   execution_mode="SIMULATION", physical_output_connected=False)
    atomic_json(engine.root / "controls" / f"{ident}.json", {**payload, **changes})


def result(engine, ident="command-a", directory="results"):
    return json.loads((engine.root / directory / f"{ident}.json").read_text())


def tick(engine, measured, elapsed, *, sim_time=None, ready=True, renew=True, run_id="run-a"):
    engine.set_world_ready(ready, "Fake scene predicates checked")
    if renew:
        permit(engine, elapsed, run_id)
    return engine.tick(measured, elapsed if sim_time is None else sim_time,
                       now=NOW + elapsed, mono=elapsed)


@pytest.fixture
def running(engine):
    command(engine)
    tick(engine, engine.program.start("mobile_delivery"), 0.)
    assert engine.state == "RUNNING"
    return engine


def test_fixed_identity_contract_and_shared_guard_implementation(engine):
    workflow = json.loads((Path(__file__).resolve().parents[2]
                           / "contracts/tasks/iitp-scenario1-orchestrated.v2.json").read_text())
    assert list(ACTIONS) == [step["step_id"] for step in workflow["steps"]]
    for step in workflow["steps"]:
        _, capability, operation = ACTIONS[step["step_id"]]
        assert capability == step["capability"]
        assert {"operation": operation} == step["parameters"]
    assert ROOT == Path("/tmp/sang-isaac-scenario1")
    assert engine.runtime_kind == "isaac-sim-executor" and engine.scenario_id == "isaac-scenario1"
    assert engine.profile_id == "scenario1" and engine.scene_sha256 == SCENE_SHA
    assert engine.heartbeat(NOW)["scenario_id"] == "isaac-scenario1"
    for method in ("permit_valid", "_accept", "_control", "_persist", "_finish", "shutdown"):
        assert getattr(ScenarioExecutor, method) is getattr(PilotExecutor, method)


def test_five_measured_steps_reset_and_repeat(engine):
    measured = engine.program.start("mobile_delivery")
    elapsed = 0.
    for run in ("run-a", "run-b"):
        for step in ACTIONS:
            ident = f"{run}-{step}"
            command(engine, step, ident, elapsed, run_id=run)
            assert engine.next_step == step
            for i in range(7):
                measured = tick(engine, measured, elapsed + i * .2, run_id=run)
            assert result(engine, ident)["state"] == "SUCCEEDED"
            assert result(engine, ident)["scenario_id"] == "isaac-scenario1"
            elapsed += 2.
        assert engine.state == "COMPLETE" and engine.next_step is None
        assert measured == pytest.approx(engine.program.start("mobile_delivery"))
        control(engine, "RESET", f"reset-{run}", elapsed, run_id=run)
        assert tick(engine, measured, elapsed, run_id=run) == measured
        assert engine.state == "READY" and engine.run_id is None
        assert engine.elapsed_s == 0. and engine.phase == "READY"
        elapsed += 1.
    assert engine.epoch == 2


@pytest.mark.parametrize("axis", range(16))
def test_completion_requires_every_actual_axis_with_unit_specific_tolerance(running, axis):
    goal = running.program.end("mobile_delivery")
    measured = list(goal)
    tolerance = .03 if axis < 13 else .005
    measured[axis] += tolerance + .0001
    tick(running, measured, 1.1)
    assert running.state == "RUNNING"
    assert not (running.root / "results/command-a.json").exists()
    measured[axis] = goal[axis] + tolerance - .0001
    tick(running, measured, 1.2)
    assert result(running)["state"] == "SUCCEEDED"


def test_time_and_commanded_target_alone_cannot_complete(running):
    start = running.program.start("mobile_delivery")
    target = tick(running, start, 1.1)
    assert target == running.program.end("mobile_delivery")
    assert running.state == "RUNNING"


def test_world_gate_defaults_false_and_blocks_admission(engine):
    measured = engine.program.start("mobile_delivery")
    permit(engine)
    command(engine)
    assert engine.world_ready is False
    assert engine.tick(measured, 0., now=NOW, mono=0.) == measured
    assert result(engine)["state"] == "REJECTED"
    assert "preconditions" in result(engine)["detail"]
    assert not engine.program.samples


def test_world_gate_blocks_completion_until_actual_object_ready(running):
    goal = running.program.end("mobile_delivery")
    tick(running, goal, 1.1, ready=False)
    assert running.state == "RUNNING"
    assert running.next_step == "mobile_delivery"
    tick(running, goal, 1.2)
    assert result(running)["state"] == "SUCCEEDED"


def test_world_gate_is_consumed_and_stale_true_cannot_admit(engine):
    measured = engine.program.start("mobile_delivery")
    tick(engine, measured, 0.)
    assert engine.world_ready is False
    command(engine, elapsed=.1)
    assert engine.tick(measured, .1, now=NOW + .1, mono=.1) == measured
    assert result(engine)["state"] == "REJECTED"
    assert "not refreshed" in result(engine)["detail"]


def test_world_gate_is_consumed_and_stale_true_cannot_complete(running):
    goal = running.program.end("mobile_delivery")
    assert running.world_ready is False
    running.tick(goal, 1.1, now=NOW + 1.1, mono=1.1)
    assert running.state == "RUNNING"
    tick(running, goal, 1.2)
    assert result(running)["state"] == "SUCCEEDED"
    assert running.world_ready is False


def test_world_gate_is_consumed_even_when_tick_raises(engine):
    engine.set_world_ready(True, "Checked before invalid measurement")
    with pytest.raises(RuntimeError, match="Invalid measured"):
        engine.tick([float("nan")] * 16, 0., now=NOW, mono=0.)
    assert engine.world_ready is False


def test_completed_step_world_predicate_cannot_admit_next_step(running):
    measured = running.program.end("mobile_delivery")
    tick(running, measured, 1.1)
    assert running.next_step == "omy_pre_pick_place"
    command(running, "omy_pre_pick_place", "next", 1.2)
    assert running.tick(measured, 1.2, now=NOW + 1.2, mono=1.2) == measured
    assert result(running, "next")["state"] == "REJECTED"
    assert "not refreshed" in result(running, "next")["detail"]


def test_hold_freezes_phase_elapsed_and_measured_targets_resume_does_not_restart(running):
    measured = tick(running, running.target, .4)
    control(running, "HOLD", elapsed=.6)
    held = tick(running, measured, .6)
    assert held == measured and running.state == "HELD"
    assert running.elapsed_s == pytest.approx(.6)
    phase, deadline = running.phase, running.command_deadline
    drifted = list(held)
    drifted[14] += .02
    sample_count = len(running.program.samples)
    assert tick(running, drifted, 5.) == held
    assert len(running.program.samples) == sample_count
    assert running.elapsed_s == pytest.approx(.6) and running.phase == phase
    control(running, "RESUME", "resume", elapsed=5.1)
    resumed = tick(running, drifted, 5.1)
    assert running.state == "RUNNING"
    assert running.elapsed_s == pytest.approx(.6)
    assert resumed == pytest.approx(running.program.sample("mobile_delivery", .6))
    assert running.command_deadline == deadline
    tick(running, resumed, 5.3)
    assert running.elapsed_s == pytest.approx(.8)


@pytest.mark.parametrize("axis", range(16))
def test_new_command_requires_recorded_start_on_every_axis(engine, axis):
    measured = engine.program.start("mobile_delivery")
    measured[axis] += .0501 if axis < 13 else -.0101
    command(engine)
    assert tick(engine, measured, 0.) == measured
    assert result(engine)["state"] == "REJECTED"
    assert "recorded start" in result(engine)["detail"]


def test_admission_allows_only_small_measured_start_error(engine):
    measured = engine.program.start("mobile_delivery")
    measured = [value + (.0499 if i < 13 else -.0099) for i, value in enumerate(measured)]
    command(engine)
    target = tick(engine, measured, 0.)
    assert engine.state == "RUNNING"
    assert target == engine.program.start("mobile_delivery")


def test_partial_abort_reset_keeps_pose_and_rejects_new_scene_start(running):
    measured = tick(running, running.target, .4)
    control(running, "ABORT", elapsed=.5)
    assert tick(running, measured, .5) == measured
    assert result(running)["state"] == "ABORTED"
    control(running, "RESET", "reset", elapsed=.6)
    assert tick(running, measured, .6) == measured
    assert running.state == "READY" and running.epoch == 1
    command(running, ident="new-command", elapsed=.7, run_id="run-new")
    assert tick(running, measured, .7, run_id="run-new") == measured
    assert result(running, "new-command")["state"] == "REJECTED"
    assert "scene recovery" in result(running, "new-command")["detail"]


@pytest.mark.parametrize("changes", [
    {"agent_id": "physical-carrier"},
    {"scenario_id": "isaac-rb5-twin-pilot"},
    {"capability": "simulate-waam-path@1"},
    {"parameters": {"operation": "deliver-base-plate", "bound_asset_id": "real-carrier"}},
    {"parameters": {"operation": "deliver-base-plate", "bound_asset_id": "isaac-scenario1-omy"}},
    {"parameters": {"operation": "return-with-base-plate", "bound_asset_id": "isaac-scenario1-carrier"}},
    {"parameters": {"operation": "deliver-base-plate", "bound_asset_id": "isaac-scenario1-carrier",
                    "source_path": "/tmp/untrusted.py"}},
    {"step_id": "not-registered"},
    {"issued_at": utc_string(NOW - 3)},
    {"issued_at": utc_string(NOW + 1)},
    {"not_valid_after": utc_string(NOW + 901)},
])
def test_unregistered_or_stale_command_never_moves(engine, changes):
    command(engine, **changes)
    measured = engine.program.start("mobile_delivery")
    assert tick(engine, measured, 0.) == measured
    assert result(engine)["state"] == "REJECTED"
    assert not engine.program.samples


def test_wrong_step_or_run_cannot_take_ownership(running):
    for step, changes in (("mobile_return", {}), ("mobile_delivery", {"run_id": "other"})):
        command(running, step, step, .1, **changes)
    tick(running, running.target, .1)
    assert running.active.command_id == "command-a"
    assert result(running, "mobile_return")["state"] == "REJECTED"
    assert result(running, "mobile_delivery")["state"] == "REJECTED"


@pytest.mark.parametrize("changes", [
    {"session_id": "old"}, {"profile_sha256": "c" * 64}, {"scene_sha256": "c" * 64},
    {"profile_id": "rb5"}, {"runtime_kind": "isaac-scenario1"}, {"run_id": "other"},
    {"worker_id": ""}, {"physical_output_connected": True},
    {"not_valid_after": utc_string(NOW + 2.1)},
])
def test_wrong_permit_identity_or_duration_blocks_admission(engine, changes):
    command(engine)
    permit(engine, **changes)
    measured = engine.program.start("mobile_delivery")
    assert tick(engine, measured, 0., renew=False) == measured
    assert result(engine)["state"] == "REJECTED"


@pytest.mark.parametrize("held", [False, True])
def test_permit_loss_aborts_and_cannot_rearm(running, held):
    measured = tick(running, running.target, .4)
    if held:
        control(running, "HOLD", elapsed=.5)
        tick(running, measured, .5)
    running.tick(measured, .6, now=NOW + .5, mono=2.6)
    assert running.state == "ABORTED" and running.target == measured
    tick(running, measured, 3.)
    assert running.active is None and running.state == "ABORTED"


def test_old_permit_replay_cannot_extend_monotonic_authority(engine):
    permit(engine, -.5)
    old = json.loads((engine.root / "worker-permit.json").read_text())
    assert engine.permit_valid("run-a", NOW, 0.)
    permit(engine)
    new = json.loads((engine.root / "worker-permit.json").read_text())
    assert engine.permit_valid("run-a", NOW, .5)
    for mono, payload in ((1.6, old), (2.6, new), (3.6, old)):
        atomic_json(engine.root / "worker-permit.json", payload)
        assert not engine.permit_valid("run-a", NOW, mono)


@pytest.mark.parametrize("duration,budget", [(1., 22.), (250., 420.)])
def test_held_wall_cap_is_captured_and_never_extended(tmp_path, duration, budget):
    engine = ScenarioExecutor(tmp_path, PROFILE_SHA, SCENE_SHA, FakeProgram(duration))
    command(engine)
    measured = engine.program.start("mobile_delivery")
    tick(engine, measured, 0.)
    assert engine.command_deadline == budget
    control(engine, "HOLD", elapsed=.1)
    tick(engine, measured, .1)
    tick(engine, measured, budget, sim_time=.1)
    assert result(engine)["state"] == "FAILED"
    assert "deadline" in result(engine)["detail"]
    assert engine.target == measured


def test_command_expiry_uses_captured_monotonic_time_if_wall_clock_stalls(engine):
    command(engine, not_valid_after=utc_string(NOW + .5))
    measured = engine.program.start("mobile_delivery")
    tick(engine, measured, 0.)
    engine.tick(measured, .1, now=NOW, mono=.5)
    assert result(engine)["state"] == "FAILED"


def test_motion_budget_is_specific_to_each_recorded_step(tmp_path):
    durations = dict(zip(ACTIONS, [10., 63., 100., 57., 10.]))
    program = FakeProgram()
    program.duration = durations.__getitem__
    engine = ScenarioExecutor(tmp_path, PROFILE_SHA, SCENE_SHA, program)
    for step, duration in durations.items():
        assert engine._motion_budget(step) == duration * 2 + 20


def test_acceptance_is_durable_before_any_sample_is_requested(engine, monkeypatch):
    original = engine.program.sample

    def checked_sample(step, elapsed):
        journal = json.loads(engine.journal_path.read_text())
        assert journal["last_run_id"] == "run-a"
        assert journal["commands"]["command-a"]["result"] is None
        return original(step, elapsed)

    monkeypatch.setattr(engine.program, "sample", checked_sample)
    command(engine)
    tick(engine, engine.program.start("mobile_delivery"), 0.)
    assert engine.program.samples


def test_duplicate_command_cannot_restart_or_replace_active_motion(running):
    tick(running, running.target, .3)
    command(running, elapsed=.4, parameters={"source_path": "/tmp/injected.py"})
    tick(running, running.target, .4)
    assert running.active.parameters["operation"] == "deliver-base-plate"
    assert running.elapsed_s == pytest.approx(.4)
    assert running.motion_start_mono == 0.


@pytest.mark.parametrize("finish", ["restart", "shutdown", "fail"])
def test_terminal_abort_is_durable_idempotent_and_holds_measurement(running, finish):
    measured = tick(running, running.target, .4)
    tick(running, measured, .5)
    if finish == "shutdown":
        running.shutdown(NOW + .6)
    elif finish == "fail":
        running.fail("Plate ownership lost")
        assert running.state == "ABORTED" and running.target == measured
    restarted = ScenarioExecutor(running.root, PROFILE_SHA, SCENE_SHA, FakeProgram())
    assert restarted.session_id != running.session_id
    terminal = result(restarted)
    assert terminal["state"] == "ABORTED" and terminal["scenario_id"] == "isaac-scenario1"
    command(restarted, parameters={"injected": True})
    assert tick(restarted, measured, 0.) == measured
    assert restarted.active is None and restarted.state == "ABORTED"
    assert result(restarted) == terminal


def test_fail_while_idle_latches_admission(engine):
    measured = engine.program.start("mobile_delivery")
    tick(engine, measured, 0.)
    engine.fail("Plate missing")
    assert engine.state == "ABORTED" and engine.target == measured
    command(engine, elapsed=.1)
    tick(engine, measured, .1)
    assert result(engine)["state"] == "REJECTED"


def test_postphysics_failure_holds_fresh_measurement_on_later_ticks(running):
    fresh = list(running.last_measured)
    fresh[0], fresh[7], fresh[14] = .02, -.02, 2.49
    expected = list(fresh)
    running.fail("Post-physics grasp predicate failed", fresh)
    assert running.state == "ABORTED" and running.active is None
    assert running.last_measured == expected and running.target == expected
    assert running.world_ready is False
    terminal = result(running)
    assert terminal["state"] == "ABORTED" and "Post-physics" in terminal["detail"]
    fresh[0] = .5
    assert running.last_measured == expected and running.target == expected
    for elapsed in (.1, .2):
        assert tick(running, expected, elapsed) == expected
        assert running.state == "ABORTED" and result(running) == terminal


def test_repeated_passive_failure_refreshes_hold_without_rewriting_terminal_result(running):
    running.fail("Initial world failure")
    terminal = result(running)
    fresh = list(running.last_measured)
    fresh[0] = .03
    running.fail("Failure during passive tracking", fresh)
    assert running.target == fresh and running.last_measured == fresh
    assert running.state == "ABORTED" and result(running) == terminal
    assert tick(running, fresh, .1) == fresh


def test_idle_failure_always_replaces_old_target_with_fresh_valid_measurement(engine):
    measured = engine.program.start("mobile_delivery")
    tick(engine, measured, 0.)
    fresh = list(measured)
    fresh[0] = .02
    engine.fail("Passive body predicate failed", fresh)
    assert engine.active is None and engine.state == "ABORTED"
    assert engine.target == fresh and engine.last_measured == fresh
    assert tick(engine, fresh, .1) == fresh


@pytest.mark.parametrize("fault", ["short", "nan", "inf", "joint_bound", "carrier_bound"])
def test_postphysics_failure_never_replaces_hold_with_invalid_measurement(running, fault):
    expected = list(running.last_measured)
    fresh = list(expected)
    if fault == "short":
        fresh.pop()
    elif fault == "carrier_bound":
        fresh[14] = 9.
    else:
        fresh[0] = 3.16 if fault == "joint_bound" else float(fault)
    running.fail("Invalid post-physics feedback", fresh)
    assert running.last_measured == expected and running.target == expected
    assert running.state == "ABORTED" and result(running)["state"] == "ABORTED"
    assert tick(running, expected, .1) == expected


def test_reset_after_postphysics_failure_preserves_partial_pose_and_requires_recovery(running):
    fresh = list(running.last_measured)
    fresh[0] = .1
    running.fail("Fixture lost during transfer", fresh)
    epoch = running.epoch
    control(running, "RESET", "reset-fresh", elapsed=.1)
    assert tick(running, fresh, .1) == fresh
    assert running.epoch == epoch + 1 and running.state == "READY"
    assert running.target == fresh and running.phase == "READY"
    command(running, ident="new-scene-required", elapsed=.2, run_id="run-b")
    assert tick(running, fresh, .2, run_id="run-b") == fresh
    assert result(running, "new-scene-required")["state"] == "REJECTED"
    assert "recorded start" in result(running, "new-scene-required")["detail"]


def test_cancel_before_admission_survives_reset_and_restart(engine):
    control(engine, "ABORT", "cancel")
    control(engine, "RESET", "reset")
    command(engine)
    measured = engine.program.start("mobile_delivery")
    tick(engine, measured, 0.)
    assert result(engine)["state"] == "REJECTED"
    assert engine.epoch == 1
    restarted = ScenarioExecutor(engine.root, PROFILE_SHA, SCENE_SHA, FakeProgram())
    command(restarted, ident="retry", elapsed=.1)
    tick(restarted, measured, .1)
    assert result(restarted, "retry")["state"] == "REJECTED"
    assert "cancelled" in result(restarted, "retry")["detail"]


@pytest.mark.parametrize("action", ["ABORT", "RESET"])
def test_control_result_publication_recovers_from_durable_journal(running, monkeypatch, action):
    measured = running.target
    if action == "RESET":
        control(running, "ABORT", "prepare")
        tick(running, measured, .1)
    control(running, action, "interrupted", .2)
    output = running.root / "control-results/interrupted.json"

    def interrupted_write(path, data):
        if path == output:
            raise OSError("interrupted result publication")
        atomic_json(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(pilot_module, "atomic_json", interrupted_write)
        with pytest.raises(OSError, match="interrupted result"):
            tick(running, measured, .2)
    expected = running.journal["controls"]["interrupted"]["result"]
    assert expected["state"] == "ACKNOWLEDGED" and not output.exists()
    restarted = ScenarioExecutor(running.root, PROFILE_SHA, SCENE_SHA, FakeProgram())
    assert result(restarted, "interrupted", "control-results") == expected


@pytest.mark.parametrize("axis,value", [(0, 3.151), (12, -3.151), (6, 1.171), (6, -.031),
                                        (13, 0.), (14, 3.), (15, .04)])
def test_measured_joint_or_program_scene_envelope_fails_closed(running, axis, value):
    measured = list(running.target)
    measured[axis] = value
    with pytest.raises(RuntimeError, match="envelope"):
        tick(running, measured, .1)
    assert result(running)["state"] == "FAILED"
    assert running.target == measured


def test_gripper_tolerance_and_full_joint_range_do_not_use_pilot_envelope(engine):
    measured = engine.program.start("mobile_delivery")
    measured[:13] = [-3.15, 3.15] * 6 + [-3.15]
    measured[6] = 1.16
    assert tick(engine, measured, 0.) == measured


@pytest.mark.parametrize("measured", [[0.] * 15, [float("nan")] * 16, [float("inf")] * 16])
def test_invalid_measurement_requires_simulation_stop(engine, measured):
    with pytest.raises(RuntimeError, match="Invalid measured"):
        tick(engine, measured, 0.)


def test_simulation_clock_regression_aborts(running):
    tick(running, running.target, .5)
    measured = list(running.target)
    assert tick(running, measured, .6, sim_time=.4) == measured
    assert result(running)["state"] == "ABORTED"


def test_invalid_recorded_target_is_not_emitted(running, monkeypatch):
    monkeypatch.setattr(running.program, "sample", lambda step, elapsed: [0.] * 15)
    measured = running.target
    with pytest.raises(RuntimeError, match="setpoint"):
        tick(running, measured, .1)
    assert running.target == measured and result(running)["state"] == "FAILED"


@pytest.mark.parametrize("duration", [0., -1., float("nan"), float("inf")])
def test_invalid_program_duration_rejected_before_inbox_creation(tmp_path, duration):
    with pytest.raises(ValueError, match="duration"):
        ScenarioExecutor(tmp_path, PROFILE_SHA, SCENE_SHA, FakeProgram(duration))
    assert not (tmp_path / "executor-journal.json").exists()
