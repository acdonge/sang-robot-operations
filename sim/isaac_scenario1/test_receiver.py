"""Independent observation-only receiver and generated motion contract checks."""

import ast
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from isaac_scenario1 import receiver as receiver_module
from isaac_scenario1.contract import HERE, JOINTS, STEPS, atomic_json
from isaac_scenario1.receiver import SceneReceiver, read_message, validate_runtime_identity, validate_state


NOW = 1788979800.
SESSION = "a" * 32
SCENE_SHA = "b" * 64
JPEG = b"\xff\xd8test-frame\xff\xd9"


def snapshot(**changes):
    data = dict(schema_version=2, runtime_kind="isaac-sim-executor", profile_id="scenario1",
                scene_sha256=SCENE_SHA, session_id=SESSION, epoch=0, sequence=1,
                sim_time_ns=10, source_unix_ms=int(NOW * 1000), physical_output_connected=False,
                frame_id="cell", phase="mobile_delivery", plate_owner="carrier", assets=[
                    dict(asset_id=f"isaac-scenario1-{name}", joint_names=list(names),
                         joint_positions_rad=[0.] * len(names), position_m=[-.818, 2.5, 0.],
                         rotation_xyzw=[0., 0., 0., 1.]) for name, names in JOINTS.items()])
    return {**data, **changes}


def frame(epoch=0, session=SESSION):
    return f"isaac-scenario1/{session}/{epoch}"


@pytest.fixture
def receiver(tmp_path):
    return SceneReceiver(tmp_path, SCENE_SHA, SESSION)


@pytest.mark.parametrize("state_now,state_mono,image_now,image_mono", [
    (NOW, 1., NOW - 1., 1.1),
    (NOW, 1., NOW + .1, .9),
])
def test_image_rechecks_future_source_and_monotonic_rollback(
        receiver, state_now, state_mono, image_now, image_mono):
    receiver.state(snapshot(), state_now, state_mono)
    receiver.image(10, frame(), JPEG, image_now, image_mono)
    assert receiver.accepted == 1 and receiver.images == 0
    assert not (receiver.root / "frame.jpg").exists()


def test_duplicate_pending_image_cannot_renew_its_receipt_time(receiver):
    receiver.image(10, frame(), JPEG, NOW, 0.)
    receiver.image(10, frame(), JPEG, NOW + .2, .2)
    receiver.state(snapshot(source_unix_ms=int((NOW + .3) * 1000)), NOW + .3, .3)
    assert receiver.accepted == 1 and receiver.images == 0
    assert not (receiver.root / "frame.jpg").exists()


@pytest.mark.parametrize("image_first", [False, True])
def test_fresh_matching_image_preserves_source_and_earliest_receipt(receiver, image_first):
    state_delay, image_delay = (.1, .05) if image_first else (.05, .1)
    actions = [lambda: receiver.state(snapshot(), NOW + state_delay, state_delay),
               lambda: receiver.image(10, frame(), JPEG, NOW + image_delay, image_delay)]
    for action in reversed(actions) if image_first else actions:
        action()
    assert receiver.accepted == receiver.images == 1
    assert (receiver.root / "frame.jpg").read_bytes() == JPEG
    telemetry = json.loads((receiver.root / "telemetry.json").read_text())
    assert telemetry == {**{k: v for k, v in snapshot().items() if k != "source_unix_ms"},
                         "received_at_unix_ms": int((NOW + state_delay) * 1000)}
    assert json.loads((receiver.root / "source-provenance.json").read_text()) == snapshot()
    provenance = json.loads((receiver.root / "image-provenance.json").read_text())
    assert provenance["received_at_unix_ms"] == int((NOW + .05) * 1000)
    assert provenance["metadata_received_at_unix_ms"] == int((NOW + state_delay) * 1000)
    assert provenance["image_received_at_unix_ms"] == int((NOW + image_delay) * 1000)
    assert provenance["sha256"] == hashlib.sha256(JPEG).hexdigest()
    assert provenance["source_unix_ms"] == int(NOW * 1000)
    assert provenance["scene_sha256"] == SCENE_SHA and provenance["physical_output_connected"] is False


@pytest.mark.parametrize("key,value", [
    ("schema_version", True), ("schema_version", 2.), ("schema_version", 1),
    ("runtime_kind", "isaac-scenario1"), ("profile_id", "rb5"),
    ("scene_sha256", "c" * 64), ("session_id", "c" * 32), ("frame_id", "world"),
    ("physical_output_connected", True), ("physical_output_connected", 0),
    ("phase", ""), ("phase", "x" * 81), ("phase", None), ("phase", "pre_grasp\n"),
    ("phase", "pre\x7fgrasp"), ("plate_owner", "rb5"), ("plate_owner", {}),
    ("assets", []), ("assets", {}),
])
def test_strict_snapshot_identity_and_fields(receiver, key, value):
    data = snapshot(**{key: value})
    with pytest.raises(ValueError):
        validate_state(data, SCENE_SHA, SESSION, NOW)
    receiver.state(data, NOW, 0.)
    assert receiver.accepted == 0 and receiver.rejected == 1
    assert not (receiver.root / "telemetry.json").exists()


@pytest.mark.parametrize("key", ["epoch", "sequence", "sim_time_ns", "source_unix_ms"])
@pytest.mark.parametrize("value", [True, -1, 1., "1", 2**63, None])
def test_counters_are_bounded_integers_not_booleans(receiver, key, value):
    receiver.state(snapshot(**{key: value}), NOW, 0.)
    assert receiver.accepted == 0 and receiver.rejected == 1


@pytest.mark.parametrize("change", ["extra", "missing", "asset-extra", "asset-missing", "duplicate"])
def test_exact_top_level_and_asset_schema(receiver, change):
    data = snapshot()
    if change == "extra":
        data["command"] = {"operation": "move"}
    elif change == "missing":
        del data["phase"]
    elif change == "asset-extra":
        data["assets"][0]["target"] = [0.] * 7
    elif change == "asset-missing":
        del data["assets"][0]["rotation_xyzw"]
    else:
        data["assets"][3] = deepcopy(data["assets"][0])
    receiver.state(data, NOW, 0.)
    assert receiver.accepted == 0 and not receiver.metadata


@pytest.mark.parametrize("key,value", [
    ("asset_id", None), ("asset_id", 12), ("asset_id", "physical-omy"),
    ("joint_names", ["wrong"] * 7), ("joint_names", []),
    ("joint_positions_rad", [0.] * 6), ("joint_positions_rad", [True] * 7),
    ("joint_positions_rad", [float("nan")] * 7), ("joint_positions_rad", [float("inf")] * 7),
    ("joint_positions_rad", [10**400] * 7), ("joint_positions_rad", ["0"] * 7),
    ("joint_positions_rad", [3.17] * 7), ("joint_positions_rad", [0.] * 6 + [-.031]),
    ("joint_positions_rad", [0.] * 6 + [1.171]),
    ("position_m", [0., 0.]), ("position_m", [11., 0., 0.]), ("position_m", [False, 0., 0.]),
    ("rotation_xyzw", [0., 0., 0., 0.]), ("rotation_xyzw", [0., 0., 0., 1.001]),
    ("rotation_xyzw", [0., 0., 0., float("nan")]),
])
def test_asset_values_are_named_finite_bounded_and_unit_quaternions(receiver, key, value):
    data = snapshot()
    data["assets"][0][key] = value
    with pytest.raises(ValueError):
        validate_state(data, SCENE_SHA, SESSION, NOW)
    receiver.state(data, NOW, 0.)
    assert receiver.accepted == 0 and receiver.rejected == 1


def test_asset_order_may_vary_but_joint_order_cannot(receiver):
    data = snapshot()
    data["assets"].reverse()
    receiver.state(data, NOW, 0.)
    assert receiver.accepted == 1
    data = snapshot(sequence=2, sim_time_ns=20)
    data["assets"][0]["joint_names"].reverse()
    receiver.state(data, NOW, 0.)
    assert receiver.accepted == 1 and receiver.rejected == 1


@pytest.mark.parametrize("raw", [
    '{"epoch":0,"epoch":1}', '{"assets":[{"asset_id":"a","asset_id":"b"}]}',
    '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e999}', '{', "x" * 65537,
    '"' + "\u00e9" * 32768 + '"', "[" * 2000 + "]" * 2000, None,
])
def test_raw_json_rejects_duplicates_nonfinite_size_and_nesting(raw):
    with pytest.raises(ValueError):
        read_message(raw)


@pytest.mark.parametrize("delay", [-.101, .251, float("nan"), float("inf")])
def test_state_source_freshness_rejects_future_stale_and_nonfinite_time(receiver, delay):
    receiver.state(snapshot(), NOW + delay, 0.)
    assert receiver.accepted == 0


def test_rejected_snapshot_cannot_advance_epoch_or_poison_followup(receiver):
    receiver.state(snapshot(epoch=100, sequence=100, scene_sha256="c" * 64), NOW, 0.)
    receiver.state(snapshot(), NOW, 0.)
    assert receiver.accepted == 1 and receiver.last == (0, 1)


def test_session_is_pinned_not_learned_or_rebound(receiver):
    receiver.state(snapshot(session_id="c" * 32), NOW, 0.)
    receiver.state(snapshot(), NOW, 0.)
    receiver.state(snapshot(sequence=2, sim_time_ns=20, session_id="c" * 32), NOW, 0.)
    receiver.image(10, frame(session="c" * 32), JPEG, NOW, 0.)
    assert receiver.accepted == 1 and receiver.images == 0
    assert receiver.session == SESSION


@pytest.mark.parametrize("scene,session", [(SCENE_SHA, None), (SCENE_SHA, ""), (SCENE_SHA, "z" * 32),
                                          ("z" * 64, SESSION), (None, SESSION)])
def test_constructor_requires_exact_digest_and_session(tmp_path, scene, session):
    with pytest.raises(ValueError):
        SceneReceiver(tmp_path, scene, session)
    with pytest.raises(ValueError):
        validate_state(snapshot(scene_sha256=scene, session_id=session), scene, session, NOW)


@pytest.mark.parametrize("changes", [
    {"sequence": 10, "sim_time_ns": 110}, {"sequence": 9, "sim_time_ns": 110},
    {"sequence": 11, "sim_time_ns": 100}, {"sequence": 11, "sim_time_ns": 99},
    {"epoch": 1, "sequence": 999, "sim_time_ns": 999},
])
def test_sequence_epoch_and_simulation_stamp_never_roll_back(receiver, changes):
    receiver.state(snapshot(epoch=2, sequence=10, sim_time_ns=100), NOW, 0.)
    before = (receiver.root / "telemetry.json").read_bytes()
    data = snapshot(epoch=2, sequence=11, sim_time_ns=101)
    data.update(changes)
    receiver.state(data, NOW + .1, .1)
    assert receiver.accepted == 1 and (receiver.root / "telemetry.json").read_bytes() == before


@pytest.mark.parametrize("change", ["source", "wall", "mono"])
def test_new_sequence_cannot_hide_source_or_receiver_clock_rollback(receiver, change):
    receiver.state(snapshot(), NOW, 1.)
    data = snapshot(sequence=2, sim_time_ns=20)
    now, mono = NOW + .1, 1.1
    if change == "source":
        data["source_unix_ms"] -= 1
    elif change == "wall":
        now = NOW - .01
    else:
        mono = .99
    receiver.state(data, now, mono)
    assert receiver.accepted == 1


def test_new_epoch_allows_sim_clock_reset_but_not_old_epoch_image(receiver):
    receiver.state(snapshot(epoch=1, sequence=10, sim_time_ns=100), NOW, 0.)
    receiver.image(100, frame(1), JPEG, NOW, 0.)
    receiver.state(snapshot(epoch=2, sequence=0, sim_time_ns=0), NOW + .1, .1)
    assert receiver.last == (2, 0) and receiver.accepted == 2
    before = (receiver.root / "image-provenance.json").read_bytes()
    receiver.image(100, frame(1), b"\xff\xd8old\xff\xd9", NOW + .1, .1)
    assert receiver.images == 1
    assert (receiver.root / "image-provenance.json").read_bytes() == before
    receiver.image(0, frame(2), JPEG, NOW + .1, .1)
    assert receiver.images == 2 and receiver.image_last == (2, 0)


def test_image_waits_for_exact_validated_epoch_and_timestamp(receiver):
    receiver.image(10, frame(2), JPEG, NOW, 0.)
    receiver.state(snapshot(epoch=1), NOW, 0.)
    receiver.state(snapshot(epoch=2, scene_sha256="c" * 64), NOW, 0.)
    assert receiver.images == 0
    receiver.state(snapshot(epoch=2), NOW + .1, .1)
    assert receiver.images == 1


def test_image_sequence_and_stamp_cannot_replace_newer_frame(receiver):
    receiver.state(snapshot(), NOW, 0.)
    receiver.state(snapshot(sequence=2, sim_time_ns=20), NOW, 0.)
    receiver.image(20, frame(), JPEG, NOW, 0.)
    receiver.image(10, frame(), b"\xff\xd8old\xff\xd9", NOW, 0.)
    receiver.image(20, frame(), b"\xff\xd8duplicate\xff\xd9", NOW, 0.)
    assert receiver.images == 1 and (receiver.root / "frame.jpg").read_bytes() == JPEG


@pytest.mark.parametrize("stamp", [-1, 1., True, "10", 2**63, None, [], float("nan")])
def test_image_stamp_is_a_strict_bounded_integer(receiver, stamp):
    receiver.image(stamp, frame(), JPEG, NOW, 0.)
    assert not receiver.pending and receiver.rejected == 1


@pytest.mark.parametrize("frame_id", [frame("-1"), frame("+1"), frame("01"), frame(" 1"),
                                         frame("1.0"), frame("\u0661"), frame(2**63), frame("1" * 5000),
                                         "other/" + SESSION + "/0", frame() + "/extra", None])
def test_image_frame_is_canonical_and_pinned(receiver, frame_id):
    receiver.image(10, frame_id, JPEG, NOW, 0.)
    assert not receiver.pending and receiver.rejected == 1


@pytest.mark.parametrize("jpeg", [b"", b"not-jpeg", b"\xff\xd8missing-end", "text", bytearray(JPEG),
                                    b"\xff\xd8" + b"x" * (2 * 1024 * 1024) + b"\xff\xd9"])
def test_image_envelope_is_bounded_immutable_jpeg_bytes(receiver, jpeg):
    receiver.image(10, frame(), jpeg, NOW, 0.)
    assert not receiver.pending and receiver.rejected == 1


@pytest.mark.parametrize("source_age,state_age,image_age", [
    (.251, .1, 0.), (-.101, .1, 0.), (0., .251, 0.), (0., 0., .251),
    (0., -.01, 0.), (0., 0., -.01),
])
def test_image_join_itself_checks_both_receipts_and_source_age(receiver, source_age, state_age, image_age):
    key = (0, 10)
    meta = snapshot(source_unix_ms=int((NOW - source_age) * 1000))
    receiver.metadata[key] = (meta, NOW - max(state_age, 0.), 1. - state_age)
    receiver.pending[key] = (JPEG, NOW - max(image_age, 0.), 1. - image_age)
    receiver._image(key, NOW, 1.)
    assert receiver.images == 0 and not (receiver.root / "frame.jpg").exists()


def test_cached_metadata_cannot_be_mutated_after_validation(receiver):
    data = snapshot()
    receiver.state(data, NOW, 0.)
    data["epoch"] = 99
    data["source_unix_ms"] = int((NOW + 10) * 1000)
    data["assets"][0]["joint_positions_rad"][0] = 99.
    receiver.image(10, frame(), JPEG, NOW, 0.)
    assert receiver.images == 1 and receiver.image_last == (0, 1)
    assert receiver.metadata[(0, 10)][0] == snapshot()


def test_pending_metadata_and_image_buffers_are_bounded(receiver):
    for i in range(95):
        receiver.state(snapshot(sequence=i, sim_time_ns=i), NOW, 0.)
        receiver.image(1000 + i, frame(), JPEG, NOW, 0.)
    assert receiver.accepted == 95 and receiver.images == 0
    assert len(receiver.metadata) == len(receiver.pending) == 90


def identity(**changes):
    return {"schema_version": 1, "runtime_kind": "isaac-sim-executor", "profile_id": "scenario1",
            "scene_sha256": SCENE_SHA, "session_id": SESSION, "physical_output_connected": False,
            "updated_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat(), **changes}


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 2}, {"runtime_kind": "other"},
    {"profile_id": "rb5"}, {"scene_sha256": "c" * 64}, {"session_id": "malformed"},
    {"physical_output_connected": 0}, {"physical_output_connected": True}, {"extra": "command"},
    {"updated_at": "2026-09-09T00:00:00"}, {"updated_at": "invalid"},
    {"updated_at": datetime.fromtimestamp(NOW - 1.1, timezone.utc).isoformat()},
    {"updated_at": datetime.fromtimestamp(NOW + .2, timezone.utc).isoformat()},
])
def test_runtime_bootstrap_requires_strict_fresh_isolated_identity(changes):
    with pytest.raises(ValueError):
        validate_runtime_identity(identity(**changes), SCENE_SHA, NOW)


def test_main_has_only_fixed_subscriptions_and_observation_writes(tmp_path, monkeypatch):
    atomic_json(tmp_path / "runtime-identity.json", identity())
    protected = ["runtime-identity.json", "worker-permit.json", "commands/sentinel.json",
                 "results/sentinel.json", "controls/sentinel.json", "heartbeat.json"]
    for name in protected[1:]:
        atomic_json(tmp_path / name, {"untouched": True})
    original = {name: (tmp_path / name).read_bytes() for name in protected}
    modules = {}
    for name in ("rclpy", "rclpy.node", "rclpy.qos", "std_msgs", "std_msgs.msg",
                 "sensor_msgs", "sensor_msgs.msg"):
        modules[name] = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, modules[name])
    subscriptions, lifecycle = [], []
    clock = [0.]

    class ReadOnlyNode:
        def __init__(self, name, **kwargs):
            assert kwargs == dict(use_global_arguments=False, enable_rosout=False,
                                  start_parameter_services=False)

        def create_subscription(self, message_type, topic, callback, qos):
            subscriptions.append((topic, callback))

        def destroy_node(self):
            lifecycle.append("destroy")

    modules["rclpy.node"].Node = ReadOnlyNode
    modules["rclpy.qos"].qos_profile_sensor_data = object()
    modules["std_msgs.msg"].String = object
    modules["sensor_msgs.msg"].CompressedImage = object
    modules["rclpy"].init = lambda **kwargs: lifecycle.append(("init", kwargs))
    modules["rclpy"].shutdown = lambda: lifecycle.append("shutdown")

    def spin_once(node, timeout_sec):
        if clock[0] == 0.:
            subscriptions[0][1](SimpleNamespace(data='{"epoch":0,"epoch":1}'))
            subscriptions[0][1](SimpleNamespace(data=json.dumps(snapshot())))
            subscriptions[1][1](SimpleNamespace(format="jpeg", data=JPEG,
                header=SimpleNamespace(frame_id=frame(), stamp=SimpleNamespace(sec=0, nanosec=10))))
        clock[0] += .025

    modules["rclpy"].spin_once = spin_once
    monkeypatch.setattr(receiver_module, "time", SimpleNamespace(
        time=lambda: NOW + clock[0], monotonic=lambda: clock[0]))
    monkeypatch.setattr(receiver_module, "ROOT", tmp_path)
    monkeypatch.setattr(receiver_module, "load_manifest", lambda: ({}, SCENE_SHA))
    monkeypatch.setattr(receiver_module, "require_isolation", lambda: lifecycle.append("isolation"))
    monkeypatch.setattr(sys, "argv", ["receiver", "--run-seconds", ".05"])
    receiver_module.main()
    assert [topic for topic, _ in subscriptions] == [receiver_module.STATE_TOPIC, receiver_module.CAMERA_TOPIC]
    assert lifecycle == ["isolation", ("init", {"args": []}), "destroy", "shutdown"]
    assert {name: (tmp_path / name).read_bytes() for name in protected} == original
    assert {p.name for p in (tmp_path / "observer").iterdir()} == {
        "telemetry.json", "source-provenance.json", "frame.jpg", "image-provenance.json", "receiver-status.json"}
    status = json.loads((tmp_path / "observer/receiver-status.json").read_text())
    assert status["samples"] == status["images"] == status["rejected"] == 1
    assert status["physical_output_connected"] is False
    tree = ast.parse(Path(receiver_module.__file__).read_text())
    calls = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not calls & {"create_publisher", "publish", "create_client", "create_service",
                        "set_parameters", "send_goal_async", "call_async", "system", "Popen"}


@pytest.fixture(scope="module")
def generated_program():
    if not (HERE / "manifest.json").exists() or not (HERE / "motion.json").exists():
        pytest.skip("Main has not generated the pinned motion/manifest contracts yet")
    from isaac_scenario1.motion import MotionProgram
    return MotionProgram()


def test_generated_manifest_pins_source_and_output_contracts(generated_program):
    manifest = generated_program.manifest
    assert manifest["schema_version"] == 1 and manifest["physical_output_connected"] is False
    assert manifest["carrier_dynamics"] == "kinematic_fixture"
    assert manifest["grasp_model"] == "measured_tool_attachment_fixture" and manifest["frame"] == "cell_ros"
    paths = [row["path"] for row in manifest["sources"]]
    assert len(paths) == len(set(paths))
    assert {"prepare.py", "scene.py", "motion.py", "motion.json", "layout.json"} <= set(paths)
    assert generated_program.scene_sha256 == hashlib.sha256((HERE / "manifest.json").read_bytes()).hexdigest()
    for row in manifest["sources"]:
        assert set(row) == {"path", "sha256"}
        assert hashlib.sha256((HERE / row["path"]).read_bytes()).hexdigest() == row["sha256"]


def test_generated_five_step_paths_are_continuous_bounded_and_measured(generated_program):
    program = generated_program
    assert tuple(program.curves) == STEPS
    workflow = json.loads((HERE.parents[1] / "contracts/tasks/iitp-scenario1-orchestrated.v2.json").read_text())
    assert tuple(step["step_id"] for step in workflow["steps"]) == STEPS
    previous = program.start(STEPS[0])
    for step in STEPS:
        start, end, duration = program.start(step), program.end(step), program.duration(step)
        assert 0. < duration <= 390.
        assert start == pytest.approx(previous, abs=.01)
        assert program.sample(step, 0.) == pytest.approx(start)
        assert program.sample(step, duration) == pytest.approx(end)
        assert program.sample(step, duration + 100.) == pytest.approx(end)
        points = program.curves[step]["points"]
        for i, point in enumerate(points):
            measured = point["positions"]
            assert len(measured) == 16 and all(math.isfinite(v) for v in measured)
            assert program.valid_measurement(measured)
            assert all(abs(v) <= 3.15 for j, v in enumerate(measured[:13]) if j != 6)
            if i:
                before = points[i - 1]
                dt = point["t"] - before["t"]
                assert dt > 0.
                assert max(abs(a - b) / dt for a, b in zip(measured[:13], before["positions"][:13])) <= .61
                midpoint = (point["t"] + before["t"]) / 2
                assert program.sample(step, midpoint) == pytest.approx(
                    [(a + b) / 2 for a, b in zip(measured, before["positions"])], abs=1e-9)
        phases = program.curves[step]["phases"]
        assert phases[0]["t"] == 0.
        for phase in phases:
            assert 0. <= phase["t"] <= duration
            assert program.phase(step, phase["t"]) == phase["name"]
        previous = end
    assert previous == pytest.approx(program.start(STEPS[0]), abs=.01)


def test_generated_omy_segments_preserve_timing_gripper_and_bounded_retarget(generated_program):
    from isaac_scenario1.prepare import SOURCE
    for prefix, step in (("pre", STEPS[1]), ("post", STEPS[3])):
        expected_points, offset = [], 0.
        names = [f"{prefix}_{suffix}" for suffix in (
            "open_gripper", "approach_retimed", "grasp", "transfer_continuous", "release", "return_home")]
        assert [p["name"] for p in generated_program.curves[step]["phases"]] == names
        for name in names:
            data = json.loads((SOURCE / "omy-main/trajectories" / f"{name}.json").read_text())
            assert data["names"] == list(JOINTS["omy"])
            for point in data["points"]:
                if expected_points and point["t"] == 0.:
                    continue
                expected_points.append((offset + point["t"], point["positions"]))
            offset += data["points"][-1]["t"]
        actual = generated_program.curves[step]["points"]
        retarget = generated_program.curves[step]["endpoint_retarget"]
        assert retarget["model"] == "virtual_visual_attachment_not_contact_grasp"
        deltas = [retarget["grasp_delta_rad"], retarget["release_delta_rad"]]
        for delta in deltas:
            assert len(delta) == 7 and all(math.isfinite(v) and abs(v) <= .25 for v in delta)
            assert delta[6] == 0.
        assert len(actual) == len(expected_points)
        for point, (elapsed, values) in zip(actual, expected_points):
            assert point["t"] == pytest.approx(elapsed)
            for axis, (current, source) in enumerate(zip(point["positions"][:7], values)):
                assert abs(current - source) <= max(abs(delta[axis]) for delta in deltas) + 1e-9
            assert point["positions"][6] == values[6]
            assert point["positions"][7:13] == [0.] * 6
            assert point["positions"][13:] == generated_program.dock


def test_generated_rb5_path_preserves_declared_retarget_order_and_ros_targets(generated_program):
    from isaac_scenario1.prepare import SOURCE
    data = json.loads((SOURCE / "Scenario1_app/Assets/StreamingAssets/scenario1/rb5_waam_trajectory.json").read_text())
    assert data["joint_names"] == [f"link{i}" for i in range(1, 7)]
    curve = generated_program.curves[STEPS[2]]
    phases, evidence, checks = curve["phases"], curve["retarget_evidence"], curve["tcp_checks"]
    assert len(evidence) == len(checks) == len(data["points"])
    assert len(phases) == len(evidence) + 1
    assert {p["name"] for p in phases} == {"rb5_approach", "rb5_path", "rb5_return"}
    assert phases[0]["name"] == "rb5_approach" and phases[-1]["name"] == "rb5_return"
    for i, (declared, check, source) in enumerate(zip(evidence, checks, data["points"])):
        assert check["t"] == phases[i + 1]["t"]
        x, y, z = source["world_target_m"]
        assert check["target_m"] == declared["target_m"] == [z, -x, y]
        measured = generated_program.sample(STEPS[2], check["t"])
        assert measured[:7] == [0.] * 7
        assert measured[7:13] == pytest.approx(declared["positions_rad"], abs=1e-9)
        assert measured[13:] == generated_program.dock
    assert generated_program.end(STEPS[2])[7:13] == pytest.approx([0.] * 6, abs=1e-9)
