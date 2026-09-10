"""Review regressions; temporary bridge files only, never launch Isaac or ROS."""

import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from isaac_twin import executor as executor_module
from isaac_twin.contract import ASSET_ID, JOINTS, MAX_MOTION_S, atomic_json
from isaac_twin.executor import PilotExecutor, digest, utc_string
from isaac_twin.receiver import ObservationJoin
from isaac_twin.test_executor import NOW, command, control, meta, permit, result


@pytest.fixture
def engine(tmp_path):
    return PilotExecutor(tmp_path, "a" * 64)


@pytest.fixture
def running(engine):
    permit(engine)
    command(engine, not_valid_after=utc_string(NOW + 10))
    engine.tick([0.] * 6, 0, now=NOW, mono=0)
    assert engine.active is not None
    return engine


def test_review_1_abort_before_acceptance_prevents_motion(engine):
    permit(engine)
    command(engine, not_valid_after=utc_string(NOW + 10))
    control(engine, "ABORT")
    for elapsed in (0., .1, .2):
        target = engine.tick([0.] * 6, elapsed, now=NOW + elapsed, mono=elapsed)
        assert engine.active is None
        assert target == pytest.approx([0.] * 6)
    assert result(engine)["state"] in ("REJECTED", "ABORTED", "FAILED")


def test_review_2_old_permits_cannot_rearm_monotonic_expiry(engine):
    path = engine.root / "worker-permit.json"
    permit(engine, now=NOW - .5)
    older = json.loads(path.read_text())
    assert engine.permit_valid("run-a", NOW, 0)
    permit(engine)
    newer = json.loads(path.read_text())
    assert engine.permit_valid("run-a", NOW, .5)
    for mono, payload in ((1.6, older), (2.6, newer), (3.6, older), (4.6, newer)):
        atomic_json(path, payload)
        assert not engine.permit_valid("run-a", NOW, mono)


def test_review_3_restart_holds_nonzero_measurement_without_permit(running):
    restarted = PilotExecutor(running.root, "a" * 64)
    measured = [.04, 0., 0., 0., 0., 0.]
    assert restarted.session_id != running.session_id
    assert result(restarted)["state"] in ("ABORTED", "FAILED")
    target = restarted.tick(measured, 0, now=NOW, mono=0)
    assert restarted.active is None
    assert target == pytest.approx(measured)


def test_review_4_command_expiry_uses_monotonic_elapsed_time(engine):
    permit(engine)
    command(engine, not_valid_after=utc_string(NOW + .5))
    engine.tick([0.] * 6, 0, now=NOW, mono=0)
    assert engine.active is not None
    measured = [.02, 0., 0., 0., 0., 0.]
    target = engine.tick(measured, 1.5, now=NOW, mono=1.5)
    assert engine.active is None
    assert target == pytest.approx(measured)
    assert result(engine)["state"] in ("ABORTED", "FAILED")


def test_review_4_old_command_is_rejected_despite_fresh_permit(engine):
    permit(engine)
    command(engine, issued_at=utc_string(NOW - 3),
            not_valid_after=utc_string(NOW + 10))
    target = engine.tick([0.] * 6, 0, now=NOW, mono=0)
    assert engine.active is None
    assert target == pytest.approx([0.] * 6)
    assert result(engine)["state"] in ("REJECTED", "ABORTED", "FAILED")


@pytest.mark.parametrize("expiry_delay", [900, 365 * 86400])
def test_review_4_long_envelope_cannot_extend_motion_budget(engine, expiry_delay):
    permit(engine)
    command(engine, not_valid_after=utc_string(NOW + expiry_delay))
    engine.tick([0.] * 6, 0, now=NOW, mono=0)
    if engine.active is None:
        assert expiry_delay > 900, "The real signed-goal envelope must remain admissible"
        assert result(engine)["state"] in ("REJECTED", "ABORTED", "FAILED")
        return
    elapsed = MAX_MOTION_S + .1
    permit(engine, now=NOW + elapsed)
    measured = [.02, 0., 0., 0., 0., 0.]
    target = engine.tick(measured, 1, now=NOW + elapsed, mono=elapsed)
    assert engine.active is None
    assert target == pytest.approx(measured)
    assert result(engine)["state"] in ("ABORTED", "FAILED")


def test_review_8_shutdown_is_durable_and_idempotent(running):
    measured = [.02, 0., 0., 0., 0., 0.]
    running.tick(measured, .1, now=NOW + .1, mono=.1)
    running.shutdown(now=NOW + .2)
    terminal = result(running)
    assert terminal["state"] in ("ABORTED", "FAILED")
    assert running.active is None
    assert running.target == pytest.approx(measured)
    assert running.heartbeat(NOW + .2)["active_command_id"] is None
    running.shutdown(now=NOW + .3)
    assert result(running) == terminal
    restarted = PilotExecutor(running.root, "a" * 64)
    assert result(restarted) == terminal
    assert restarted.active is None


def test_review_8_runner_shutdown_finalizes_before_stopping_simulation(running):
    # Execute only cleanup, not runner imports, initialization, or its live loop.
    source = Path(__file__).with_name("runner.py").read_text()
    main = next(node for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name == "main")
    cleanup = next(node.finalbody for node in main.body
                   if isinstance(node, ast.Try) and node.finalbody)
    code = compile(ast.Module(body=cleanup, type_ignores=[]), "<runner cleanup>", "exec")
    stopped = []

    def stop():
        assert running.active is None
        assert result(running)["state"] in ("ABORTED", "FAILED")
        stopped.append(True)

    namespace = dict(engine=running, ROOT=running.root, atomic_json=atomic_json,
                     measured=[.02, 0., 0., 0., 0., 0.], report={}, node=None, app=None,
                     simulation=SimpleNamespace(stop=stop),
                     time=SimpleNamespace(time=lambda: NOW + .1, monotonic=lambda: .1))
    exec(code, namespace)
    assert stopped == [True]
    saved = json.loads(running.journal_path.read_text())
    assert saved["commands"]["command-a"]["result"] == result(running)


@pytest.mark.parametrize("action", ["ABORT", "RESET"])
def test_review_9_control_result_recovers_after_publish_failure(running, monkeypatch, action):
    if action == "RESET":
        control(running, "ABORT", "prepare-abort")
        running.tick([0.] * 6, .1, now=NOW + .1, mono=.1)
    control(running, action, "interrupted-control")
    output = running.root / "control-results/interrupted-control.json"

    def interrupted_write(path, data):
        if path == output:
            raise OSError("simulated result publication failure")
        atomic_json(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(executor_module, "atomic_json", interrupted_write)
        try:
            running.tick([0.] * 6, .2, now=NOW + .2, mono=.2)
        except OSError as exc:
            assert str(exc) == "simulated result publication failure"
    saved = json.loads(running.journal_path.read_text())
    expected = saved["controls"]["interrupted-control"]["result"]
    assert expected["state"] == "ACKNOWLEDGED"
    assert not output.exists()
    restarted = PilotExecutor(running.root, "a" * 64)
    restarted.tick([0.] * 6, 0, now=NOW + .3, mono=.3)
    assert json.loads(output.read_text()) == expected
    assert restarted.active is None


@pytest.mark.parametrize("directory", ["commands", "controls"])
def test_review_10_processed_files_do_not_starve_new_input(engine, directory):
    ident_key = "command_id" if directory == "commands" else "control_id"
    if directory == "commands":
        command(engine, command_id="old-0000", scenario_id="other")
    else:
        control(engine, "ABORT", "old-0000", scenario_id="other")
    engine.tick([0.] * 6, 0, now=NOW, mono=0)
    template = deepcopy(engine.journal[directory]["old-0000"])
    payload = json.loads((engine.root / directory / "old-0000.json").read_text())
    # Seed durable history directly to avoid 1,024 full-journal rewrites.
    for index in range(1, 1024):
        ident = f"old-{index:04d}"
        old_payload = {**payload, ident_key: ident}
        row = deepcopy(template)
        row["sha256"] = digest(old_payload)
        row["result"][ident_key] = ident
        if "payload" in row:
            row["payload"] = old_payload
        engine.journal[directory][ident] = row
        atomic_json(engine.root / directory / f"{ident}.json", old_payload)
    engine._persist()
    permit(engine)
    command(engine, command_id="zz-new", not_valid_after=utc_string(NOW + 10))
    if directory == "controls":
        engine.tick([0.] * 6, 0, now=NOW, mono=0)
        assert engine.active is not None
        control(engine, "ABORT", "zz-new")
    engine.tick([0.] * 6, .1, now=NOW + .1, mono=.1)
    assert "zz-new" in engine.journal[directory]
    if directory == "commands":
        assert engine.active is not None and engine.active.command_id == "zz-new"
    else:
        assert engine.active is None
        assert engine.journal["controls"]["zz-new"]["result"]["state"] == "ACKNOWLEDGED"


SESSION = "a" * 32
# These tests cover the accepted JPEG envelope, not image decoding.
JPEG = b"\xff\xd8review-frame\xff\xd9"


@pytest.fixture
def observer(tmp_path):
    return ObservationJoin(tmp_path, expected_session=SESSION)


def frame(session=SESSION, epoch=0):
    return f"{ASSET_ID}/{session}/{epoch}"


def test_review_6_pinned_session_cannot_be_replaced_by_first_or_late_pair(observer):
    foreign = "b" * 32
    for session, sequence in ((foreign, 1), (SESSION, 1), (foreign, 2), (SESSION, 2)):
        stamp = sequence * 10
        observer.state(meta(session=session, sequence=sequence, stamp=stamp), NOW, mono=0)
        observer.joint(stamp, list(JOINTS), [0.] * 6, frame(session), NOW, mono=0)
    assert observer.current_session == SESSION
    assert observer.accepted == 2
    saved = json.loads((observer.root / "telemetry.json").read_text())
    assert saved["session_id"] == SESSION and saved["sequence"] == 2


@pytest.mark.parametrize("state_delay, joint_delay, residence", [
    (.2, .3, .1), (0., 0., .3), (0., -.05, .3),
])
def test_review_7_join_rechecks_source_and_monotonic_age(observer, state_delay, joint_delay, residence):
    observer.state(meta(), NOW + state_delay, mono=0)
    observer.joint(10, list(JOINTS), [0.] * 6, frame(), NOW + joint_delay, mono=residence)
    assert observer.accepted == 0
    assert not (observer.root / "telemetry.json").exists()


@pytest.mark.parametrize("joint_first", [False, True])
def test_review_7_join_preserves_earliest_receipt_and_source_provenance(observer, joint_first):
    state_delay, joint_delay = (.1, .05) if joint_first else (.05, .1)
    actions = [lambda: observer.state(meta(), NOW + state_delay, mono=state_delay),
               lambda: observer.joint(10, list(JOINTS), [0.] * 6, frame(),
                                      NOW + joint_delay, mono=joint_delay)]
    for action in reversed(actions) if joint_first else actions:
        action()
    assert observer.accepted == 1
    saved = json.loads((observer.root / "telemetry.json").read_text())
    assert set(saved) == (set(meta()) - {"source_unix_ms"}) | {
        "received_at_unix_ms", "joint_names", "joint_positions_rad"}
    assert saved["received_at_unix_ms"] == int((NOW + .05) * 1000)
    provenance = json.loads((observer.root / "sample-provenance.json").read_text())
    assert provenance["source_unix_ms"] == int(NOW * 1000)
    assert provenance["metadata_received_at_unix_ms"] == int((NOW + state_delay) * 1000)
    assert provenance["joint_received_at_unix_ms"] == int((NOW + joint_delay) * 1000)
    for key in ("session_id", "epoch", "sequence", "sim_time_ns"):
        assert provenance[key] == saved[key]


def test_review_5_image_waits_for_matching_validated_metadata(observer):
    observer.image(10, frame(), JPEG, NOW, mono=0)
    observer.state({**meta(), "scene_sha256": "b" * 64}, NOW, mono=0)
    observer.state(meta(stamp=20, sequence=2), NOW, mono=0)
    assert observer.accepted_images == 0
    assert not (observer.root / "frame.jpg").exists()
    observer.state(meta(), NOW + .1, mono=.1)
    assert observer.accepted_images == 1
    assert (observer.root / "frame.jpg").read_bytes() == JPEG
    provenance = json.loads((observer.root / "image-provenance.json").read_text())
    assert provenance["sha256"] == hashlib.sha256(JPEG).hexdigest()
    for key, value in meta().items():
        assert provenance[key] == value


@pytest.mark.parametrize("session, epoch, sequence", [
    ("b" * 32, 1, 2), (SESSION, 0, 99), (SESSION, 1, 1),
])
def test_review_5_old_or_foreign_image_cannot_replace_current(observer, session, epoch, sequence):
    observer.state(meta(epoch=1), NOW, mono=0)
    observer.joint(10, list(JOINTS), [0.] * 6, frame(epoch=1), NOW, mono=0)
    observer.image(10, frame(epoch=1), JPEG, NOW, mono=0)
    observer.state(meta(session=session, epoch=epoch, sequence=sequence), NOW, mono=0)
    observer.image(10, frame(session, epoch), b"\xff\xd8old\xff\xd9", NOW, mono=0)
    assert observer.accepted_images == 1
    assert (observer.root / "frame.jpg").read_bytes() == JPEG
    provenance = json.loads((observer.root / "image-provenance.json").read_text())
    assert provenance["session_id"] == SESSION and provenance["epoch"] == 1
    assert provenance["sha256"] == hashlib.sha256(JPEG).hexdigest()


def test_review_5_same_epoch_image_timestamp_cannot_regress(observer):
    observer.state(meta(), NOW, mono=0)
    observer.image(10, frame(), JPEG, NOW, mono=0)
    observer.state(meta(sequence=2, stamp=9), NOW + .1, mono=.1)
    observer.image(9, frame(), b"\xff\xd8regressed\xff\xd9", NOW + .1, mono=.1)
    assert observer.accepted_images == 1
    assert (observer.root / "frame.jpg").read_bytes() == JPEG


@pytest.mark.parametrize("state_delay, image_delay, residence", [
    (.2, .3, .1), (0., 0., .3), (0., -.05, .3),
])
def test_review_5_image_join_rechecks_source_and_monotonic_age(observer, state_delay, image_delay, residence):
    observer.state(meta(), NOW + state_delay, mono=0)
    observer.image(10, frame(), JPEG, NOW + image_delay, mono=residence)
    assert observer.accepted_images == 0
    assert not (observer.root / "frame.jpg").exists()
