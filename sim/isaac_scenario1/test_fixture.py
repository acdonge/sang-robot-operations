"""Fixture and runner guard regressions without launching Isaac or writing its root."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from . import scene as scene_module
from .scene import ScenarioScene


@pytest.fixture
def fixture_scene():
    scene = ScenarioScene.__new__(ScenarioScene)
    scene.carrier_position = np.array([-.818, 1.25, 0.])
    scene.plate_offset = np.array([0., 0., .91])
    scene.plate_position = scene.carrier_position + scene.plate_offset
    scene.grasp_offset = np.array([0., 0., -.04])
    scene.owner = "carrier"
    scene.last_phase = "pre_grasp"
    scene.events = []
    scene.tcp_checks = []
    scene.set_position = Mock()
    scene.tool_positions = Mock(return_value=(scene.plate_position - scene.grasp_offset,
                                             np.array([-.24, .819, .96])))
    return scene


def measured(gripper=.2):
    return [0.] * 6 + [gripper] + [0.] * 6 + [-.818, 1.25, 0.]


def prepare_transition(scene, prefix, action):
    goal = (np.array([-.24, .819, .91]) if (prefix, action) in
            (("post", "attach"), ("pre", "release"))
            else scene.carrier_position + scene.plate_offset)
    scene.plate_position = goal.copy()
    scene.tool_positions.return_value = (goal - scene.grasp_offset, np.zeros(3))
    if action == "attach":
        scene.owner = "carrier" if prefix == "pre" else "table"
        return prefix + "_transfer_continuous", measured(.2), "omy"
    scene.owner = "omy"
    return prefix + "_return_home", measured(0.), "table" if prefix == "pre" else "carrier"


@pytest.mark.parametrize("prefix", ["pre", "post"])
@pytest.mark.parametrize("action", ["attach", "release"])
def test_passive_phase_does_not_consume_transition_before_resume(fixture_scene, prefix, action):
    scene = fixture_scene
    phase, joints, expected_owner = prepare_transition(scene, prefix, action)
    old_owner, old_phase = scene.owner, scene.last_phase
    for kwargs in ({}, {"allow_transitions": False}):
        scene.update_fixture(phase, joints, **kwargs)
        assert scene.owner == old_owner and scene.last_phase == old_phase
        assert scene.events == []
    scene.update_fixture(phase, joints, allow_transitions=True)
    assert scene.owner == expected_owner and scene.last_phase == phase
    assert [event["event"] for event in scene.events] == [action]
    scene.update_fixture(phase, joints, allow_transitions=True)
    assert len(scene.events) == 1


@pytest.mark.parametrize("owner", ["carrier", "omy", "table"])
def test_passive_tracking_keeps_attachment_through_abort_and_reset(fixture_scene, owner):
    scene = fixture_scene
    scene.owner = owner
    initial = scene.plate_position.copy()
    grip = scene.tool_positions.return_value[0].copy()
    for phase, displacement in (("pre_transfer_continuous", .02),
                                ("pre_transfer_continuous", .06), ("READY", .1)):
        delta = np.array([displacement, 0., 0.])
        scene.carrier_position = np.array([-.818, 1.25, 0.]) + delta
        scene.tool_positions.return_value = (grip + delta, np.zeros(3))
        scene.update_fixture(phase, measured(), allow_transitions=False)
        expected = initial if owner == "table" else initial + delta
        assert scene.plate_position == pytest.approx(expected)
        assert scene.set_position.call_args.args[1] == pytest.approx(expected)
        assert scene.owner == owner and scene.events == []
        assert scene.last_phase == "pre_grasp"


@pytest.mark.parametrize("prefix", ["pre", "post"])
@pytest.mark.parametrize("action", ["attach", "release"])
@pytest.mark.parametrize("fault", ["owner", "gripper", "distance"])
def test_transition_requires_actual_owner_gripper_and_distance(fixture_scene, prefix, action, fault):
    scene = fixture_scene
    phase, joints, _ = prepare_transition(scene, prefix, action)
    if fault == "owner":
        scene.owner = "table" if action == "release" or prefix == "pre" else "carrier"
    elif fault == "gripper":
        joints[6] = .149 if action == "attach" else .041
    else:
        grip, tcp = scene.tool_positions.return_value
        scene.tool_positions.return_value = (grip + [.1, 0., 0.], tcp)
    old_owner, old_phase = scene.owner, scene.last_phase
    with pytest.raises(RuntimeError, match="fixture blocked"):
        scene.update_fixture(phase, joints, allow_transitions=True)
    assert scene.owner == old_owner and scene.last_phase == old_phase
    assert scene.events == [] and scene.set_position.call_count == 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("field", ["position", "quaternion"])
@pytest.mark.parametrize("body", [0, 4, 5, 6])
def test_nonfinite_body_pose_never_becomes_tool_feedback(fixture_scene, bad, field, body):
    scene = fixture_scene
    positions = np.zeros((7, 3))
    quaternions = np.tile([1., 0., 0., 0.], (7, 1))
    (positions if field == "position" else quaternions)[body, 0] = bad
    scene.bodies = SimpleNamespace(get_world_poses=lambda: (positions, quaternions))
    scene.finger_centers = [np.zeros(3) for _ in range(4)]
    with pytest.raises(RuntimeError, match="Non-finite measured rigid-body"):
        ScenarioScene.tool_positions(scene)
    assert scene.events == [] and scene.tcp_checks == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("field", ["joints", "plate", "grip"])
def test_nonfinite_fixture_evidence_cannot_attach(fixture_scene, bad, field):
    scene = fixture_scene
    phase, joints, _ = prepare_transition(scene, "post", "attach")
    if field == "joints":
        joints[6] = bad
    elif field == "plate":
        scene.plate_position[0] = bad
    else:
        scene.tool_positions.return_value[0][0] = bad
    with pytest.raises(RuntimeError):
        scene.update_fixture(phase, joints, allow_transitions=True)
    assert scene.owner == "table" and scene.events == []
    assert scene.last_phase == "pre_grasp" and scene.set_position.call_count == 0


@pytest.fixture
def path_scene(fixture_scene):
    scene = fixture_scene
    quaternion = Rotation.from_euler("x", 90, degrees=True).as_quat()
    rotations = np.tile([quaternion[3], *quaternion[:3]], (7, 1))
    scene.bodies = SimpleNamespace(get_world_poses=lambda: (np.zeros((7, 3)), rotations))
    target = scene.tool_positions.return_value[1].tolist()
    program = SimpleNamespace(curves={"rb5_waam": {"tcp_checks": [
        {"t": 1., "target_m": target}, {"t": 2., "target_m": target}]}})
    return scene, program


def test_tcp_checkpoints_are_measured_ordered_and_not_early(path_scene):
    scene, program = path_scene
    for elapsed, count in ((0., 0), (1., 1), (1., 1), (1.9, 1), (2., 2), (3., 2)):
        scene.check_path_point(program, elapsed)
        assert len(scene.tcp_checks) == count
    assert [point["index"] for point in scene.tcp_checks] == [0, 1]
    assert all(point["error_m"] == 0. and point["tool_angle_deg"] < 1e-5
               for point in scene.tcp_checks)


@pytest.mark.parametrize("fault", ["distance", "angle", "nan", "inf", "-inf"])
def test_bad_tcp_geometry_never_advances_checkpoint(path_scene, fault):
    scene, program = path_scene
    if fault == "angle":
        scene.bodies.get_world_poses = lambda: (np.zeros((7, 3)), np.tile([1., 0., 0., 0.], (7, 1)))
    else:
        scene.tool_positions.return_value[1][0] += .0121 if fault == "distance" else float(fault)
    with pytest.raises(RuntimeError, match="path point 0 missed"):
        scene.check_path_point(program, 1.)
    assert scene.tcp_checks == []


def test_nonfinite_tool_angle_never_advances_checkpoint(path_scene, monkeypatch):
    scene, program = path_scene
    monkeypatch.setattr(scene_module, "Rotation", SimpleNamespace(
        from_quat=lambda _: SimpleNamespace(apply=lambda _: np.full(3, np.nan))))
    with pytest.raises(RuntimeError, match="path point 0 missed"):
        scene.check_path_point(program, 1.)
    assert scene.tcp_checks == []


def test_world_completion_requires_owner_position_and_all_tcp_checks(fixture_scene):
    scene = fixture_scene
    scene.owner = "table"
    scene.plate_position = np.array([-.24, .819, .91])
    assert scene.world_ready("rb5_waam", False)[0] is True
    for count, expected in ((0, False), (24, False), (25, True), (26, False)):
        scene.tcp_checks = [{} for _ in range(count)]
        assert scene.world_ready("rb5_waam", True)[0] is expected
    scene.tcp_checks = [{} for _ in range(25)]
    scene.owner = "omy"
    assert scene.world_ready("rb5_waam", True)[0] is False
    scene.owner = "table"
    for position in ([0., 0., .91], [np.nan, .819, .91]):
        scene.plate_position = np.array(position)
        assert scene.world_ready("rb5_waam", True)[0] is False


@pytest.fixture(scope="module")
def runner_guard():
    # Execute the actual post-physics block, not a copied version of its policy.
    # Importing/starting main would require Isaac and could touch the live root.
    path = Path(scene_module.__file__).with_name("runner.py")
    tree = ast.parse(path.read_text(), filename=str(path))
    blocks = [node for node in ast.walk(tree) if isinstance(node, ast.Try) and any(
        isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Attribute)
        and statement.value.func.attr == "update_fixture" for statement in node.body)]
    assert len(blocks) == 1
    return compile(ast.Module(body=blocks, type_ignores=[]), str(path), "exec")


@pytest.mark.parametrize("state", ["READY", "RUNNING", "HELD", "ABORTED", "FAILED", "COMPLETE"])
@pytest.mark.parametrize("step", [None, "omy_pre_pick_place", "rb5_waam"])
def test_runner_tracks_every_state_but_only_running_active_can_transition(runner_guard, state, step):
    scene = SimpleNamespace(update_fixture=Mock(), check_path_point=Mock(), set_targets=Mock())
    engine = SimpleNamespace(state=state, active=None if step is None else SimpleNamespace(step_id=step),
                             phase="pre_transfer_continuous", elapsed_s=1., fail=Mock())
    joints, program = measured(), object()
    exec(runner_guard, {"engine": engine, "scene": scene, "measured": joints, "program": program})
    allowed = state == "RUNNING" and step is not None
    scene.update_fixture.assert_called_once_with(engine.phase, joints, allow_transitions=allowed)
    if allowed and step == "rb5_waam":
        scene.check_path_point.assert_called_once_with(program, engine.elapsed_s)
    else:
        scene.check_path_point.assert_not_called()
    engine.fail.assert_not_called()


@pytest.mark.parametrize("source", ["update_fixture", "check_path_point"])
def test_runner_passes_postphysics_measurement_to_failure_and_applies_held_target(runner_guard, source):
    scene = SimpleNamespace(update_fixture=Mock(), check_path_point=Mock(), set_targets=Mock())
    getattr(scene, source).side_effect = RuntimeError("Invalid measured geometry")
    fresh = measured()
    fresh[0] = .02
    engine = SimpleNamespace(state="RUNNING", active=SimpleNamespace(step_id="rb5_waam"),
                             phase="rb5_path", elapsed_s=1., target=measured())

    def fail(reason, value):
        assert reason == "Invalid measured geometry" and value is fresh
        engine.target = list(value)

    engine.fail = Mock(side_effect=fail)
    exec(runner_guard, {"engine": engine, "scene": scene, "measured": fresh, "program": object()})
    engine.fail.assert_called_once_with("Invalid measured geometry", fresh)
    scene.set_targets.assert_called_once_with(fresh)
    assert engine.target[0] == .02
