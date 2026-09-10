"""Recorded virtual-path geometry contracts; not physical safety validation."""

import json
import numpy as np
import pytest

from .contract import HERE, STEPS
from .motion import MotionProgram
from .omy_path import OmyGeometry
from .rb5_path import forward


@pytest.fixture(scope="module")
def program():
    return MotionProgram()


@pytest.mark.parametrize("step", STEPS)
def test_recorded_path_is_finite_continuous_and_clamps(program, step):
    assert program.sample(step, -1) == program.start(step)
    assert program.sample(step, program.duration(step)+1) == pytest.approx(program.end(step))
    previous = program.start(step)
    for t in np.arange(0, program.duration(step), 1/30):
        point = program.sample(step, t)
        assert program.valid_measurement(point)
        assert max(abs(a-b) for a,b in zip(point[:13],previous[:13])) < .61/30+.0001
        previous = point


def test_five_steps_are_pose_continuous_and_return_to_initial_state(program):
    for before, after in zip(STEPS, STEPS[1:]):
        assert program.end(before) == pytest.approx(program.start(after), abs=.01)
    assert program.end(STEPS[-1]) == pytest.approx(program.start(STEPS[0]), abs=.01)


@pytest.mark.parametrize("prefix,step,pick,place", [
    ("pre", STEPS[1], [-.818,1.25,.95], [-.24,.819,.95]),
    ("post", STEPS[3], [-.24,.819,.95], [-.818,1.25,.95]),
])
def test_omy_own_visual_attachment_endpoints(program, prefix, step, pick, place):
    geometry = OmyGeometry(json.loads((HERE/"layout.json").read_text()))
    phases = {p["name"]:p["t"] for p in program.curves[step]["phases"]}
    for suffix, target in (("transfer_continuous",pick),("return_home",place)):
        measured = program.sample(step,phases[prefix+"_"+suffix])[:7]
        assert geometry.forward(measured) == pytest.approx(target, abs=.001)


def test_rb5_world_path_retarget_matches_all_25_recorded_targets(program):
    from .contract import unity_rotation_to_ros
    layout = json.loads((HERE/"layout.json").read_text())
    frame = next(f for f in layout["frames"] if f["path"] == "rb5_850e")
    curve = program.curves[STEPS[2]]
    assert len(curve["tcp_checks"]) == 25
    for check in curve["tcp_checks"]:
        q = program.sample(STEPS[2],check["t"])[7:13]
        tcp, direction = forward(q,frame["position_ros_world_m"],unity_rotation_to_ros(frame["rotation_unity_world_xyzw"]))
        assert tcp == pytest.approx(check["target_m"], abs=.002)
        assert direction == pytest.approx([0,0,-1],abs=.003)


def test_contact_and_mobile_limits_are_explicit_in_pinned_manifest(program):
    assert program.manifest["physical_output_connected"] is False
    assert program.manifest["carrier_dynamics"] == "kinematic_fixture"
    assert program.manifest["gripper_table_contacts"] == "excluded_for_kinematic_fixture"
