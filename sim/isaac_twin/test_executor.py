from datetime import datetime, timezone
import json

import pytest

from isaac_twin.contract import *
from isaac_twin.executor import PilotExecutor, utc_string
from isaac_twin.receiver import ObservationJoin

NOW = 1788979800.0


def permit(engine, *, now=NOW, run_id="run-a", **changes):
    data = dict(schema_version=1, runtime_kind=RUNTIME_KIND, worker_id="worker-a",
                run_id=run_id, profile_id=PROFILE_ID, scene_sha256=SCENE_SHA256,
                profile_sha256="a" * 64, session_id=engine.session_id,
                issued_at=utc_string(now), not_valid_after=utc_string(now+2),
                physical_output_connected=False)
    data.update(changes)
    atomic_json(engine.root / "worker-permit.json", data)


def command(engine, *, step="move_offset", command_id="command-a", **changes):
    data = dict(schema_version=1, scenario_id=SCENARIO_ID, command_id=command_id,
                run_id="run-a", step_id=step, agent_id=AGENT_ID, capability=CAPABILITY,
                parameters={"operation": OPERATIONS[step], "bound_asset_id": ASSET_ID},
                issued_at=utc_string(NOW), not_valid_after=utc_string(NOW+60),
                execution_mode="SIMULATION", physical_output_connected=False)
    data.update(changes)
    atomic_json(engine.root / "commands" / f"{command_id}.json", data)


def control(engine, action, ident="control-a", now=NOW, **changes):
    data = dict(schema_version=1, scenario_id=SCENARIO_ID, control_id=ident,
                run_id="run-a", action=action, issued_at=utc_string(now),
                not_valid_after=utc_string(now+10), execution_mode="SIMULATION",
                physical_output_connected=False)
    data.update(changes)
    atomic_json(engine.root / "controls" / f"{ident}.json", data)


def result(engine, command_id="command-a"):
    return json.loads((engine.root / "results" / f"{command_id}.json").read_text())


def test_measured_two_step_completion_and_reset(tmp_path):
    engine = PilotExecutor(tmp_path, "a"*64)
    measured = [0.]*6
    for step, ident in (("move_offset", "command-a"), ("return_ready", "command-b")):
        command(engine, step=step, command_id=ident,
                issued_at=utc_string(NOW+(4 if step == "return_ready" else 0)))
        for i in range(35):
            now = NOW + i*.1 + (4 if step == "return_ready" else 0)
            permit(engine, now=now)
            measured = engine.tick(measured, now-NOW, now=now, mono=now-NOW)
        assert result(engine, ident)["state"] == "SUCCEEDED"
    assert engine.state == "COMPLETE"
    assert measured == [0.]*6
    control(engine, "RESET", now=NOW+8)
    engine.tick(measured, 8, now=NOW+8, mono=8)
    assert engine.epoch == 1 and engine.state == "READY" and engine.run_id is None
    assert engine.target == measured


@pytest.mark.parametrize("changes", [
    {"agent_id": "physical-rb5"}, {"scenario_id": "other"},
    {"parameters": {"operation": "move-offset", "bound_asset_id": "real-rb5"}},
    {"parameters": {"operation": "move-offset", "bound_asset_id": ASSET_ID, "delta": 2}},
    {"issued_at": utc_string(NOW-2), "not_valid_after": utc_string(NOW-.1)},
])
def test_invalid_command_rejected_without_motion(tmp_path, changes):
    engine = PilotExecutor(tmp_path, "a"*64)
    permit(engine)
    command(engine, **changes)
    assert engine.tick([0.]*6, 0, now=NOW, mono=0) == [0.]*6
    assert result(engine)["state"] == "REJECTED"


@pytest.mark.parametrize("changes", [
    {"session_id": "wrong"}, {"profile_sha256": "b"*64},
    {"not_valid_after": utc_string(NOW+5)}, {"physical_output_connected": True},
    {"issued_at": utc_string(NOW+1), "not_valid_after": utc_string(NOW+2)},
])
def test_bad_permit_never_dispatches(tmp_path, changes):
    engine = PilotExecutor(tmp_path, "a"*64)
    permit(engine, **changes)
    command(engine)
    engine.tick([0.]*6, 0, now=NOW, mono=0)
    assert result(engine)["state"] == "REJECTED"


def test_permit_expiry_monotonic_even_if_wall_clock_stalls(tmp_path):
    engine = PilotExecutor(tmp_path, "a"*64)
    permit(engine)
    command(engine)
    engine.tick([0.]*6, 0, now=NOW, mono=0)
    engine.tick([.02, 0, 0, 0, 0, 0], 1, now=NOW, mono=2.1)
    assert engine.state == "ABORTED"
    assert engine.target[0] == .02
    permit(engine, now=NOW+3)
    engine.tick(engine.target, 2, now=NOW+3, mono=3)
    assert engine.state == "ABORTED"


def test_restart_never_reexecutes_accepted_command(tmp_path):
    engine = PilotExecutor(tmp_path, "a"*64)
    permit(engine)
    command(engine)
    engine.tick([0.]*6, 0, now=NOW, mono=0)
    restarted = PilotExecutor(tmp_path, "a"*64)
    assert restarted.session_id != engine.session_id
    assert result(restarted)["state"] == "ABORTED"
    permit(restarted)
    restarted.tick([0.]*6, 0, now=NOW, mono=0)
    assert restarted.active is None


def test_abort_and_reset_do_not_teleport(tmp_path):
    engine = PilotExecutor(tmp_path, "a"*64)
    permit(engine)
    command(engine)
    engine.tick([0.]*6, 0, now=NOW, mono=0)
    measured = [.04, 0, 0, 0, 0, 0]
    control(engine, "ABORT")
    assert engine.tick(measured, .2, now=NOW+.2, mono=.2) == measured
    control(engine, "RESET", "control-b")
    assert engine.tick(measured, .3, now=NOW+.3, mono=.3) == measured
    assert engine.state == "READY"
    command(engine, command_id="command-new", run_id="run-new")
    permit(engine, run_id="run-new", now=NOW+.4)
    engine.tick(measured, .4, now=NOW+.4, mono=.4)
    assert engine.active.command_id == "command-new"


def test_hold_and_resume_require_matching_run_and_live_permit(tmp_path):
    engine = PilotExecutor(tmp_path, "a"*64)
    permit(engine)
    command(engine)
    engine.tick([0.]*6, 0, now=NOW, mono=0)
    control(engine, "HOLD")
    measured = [.02, 0, 0, 0, 0, 0]
    assert engine.tick(measured, .1, now=NOW+.1, mono=.1) == measured
    assert engine.state == "HELD"
    control(engine, "RESUME", "control-b", run_id="other")
    engine.tick(measured, .2, now=NOW+.2, mono=.2)
    assert engine.state == "HELD"
    control(engine, "RESUME", "control-c")
    engine.tick(measured, .3, now=NOW+.3, mono=.3)
    assert engine.state == "RUNNING"


def test_invalid_measured_and_clock_fail_closed(tmp_path):
    engine = PilotExecutor(tmp_path, "a"*64)
    with pytest.raises(RuntimeError):
        engine.tick([float("nan")]*6, 0)
    with pytest.raises(RuntimeError):
        engine.tick([.5]*6, 0)
    engine.tick([0.]*6, 1, now=NOW, mono=0)
    engine.tick([0.]*6, 0, now=NOW, mono=0)
    assert engine.state == "ABORTED"


def meta(*, epoch=0, sequence=1, stamp=10, session="a"*32):
    return {**identity(session), "epoch": epoch, "sequence": sequence,
            "sim_time_ns": stamp, "source_unix_ms": int(NOW*1000)}


def test_ros_join_named_order_reset_identity_and_staleness(tmp_path):
    join = ObservationJoin(tmp_path)
    names = list(reversed(JOINTS))
    frame = f"{ASSET_ID}/{'a'*32}/0"
    join.state(meta(), NOW)
    join.joint(10, names, [0, 0, 0, 0, 0, .1], frame, NOW)
    saved = json.loads((tmp_path / "telemetry.json").read_text())
    assert saved["joint_positions_rad"][0] == .1
    assert join.accepted == 1
    join.state(meta(), NOW)
    join.joint(10, names, [0.]*6, frame, NOW)
    assert join.accepted == 1
    join.state(meta(epoch=1, stamp=1), NOW)
    join.joint(1, names, [0.]*6, f"{ASSET_ID}/{'a'*32}/1", NOW)
    assert join.accepted == 2
    join.state(meta(epoch=0, sequence=9, stamp=20), NOW)
    join.joint(20, names, [0.]*6, frame, NOW)
    assert join.accepted == 2
    join.state(meta(sequence=10, stamp=21), NOW+3)
    assert join.rejected >= 3


def test_ros_never_joins_across_epochs_or_sessions(tmp_path):
    join = ObservationJoin(tmp_path)
    join.state(meta(epoch=1), NOW)
    join.joint(10, list(JOINTS), [0.]*6, f"{ASSET_ID}/{'a'*32}/0", NOW)
    join.joint(10, list(JOINTS), [0.]*6, f"{ASSET_ID}/{'b'*32}/1", NOW)
    assert join.accepted == 0
