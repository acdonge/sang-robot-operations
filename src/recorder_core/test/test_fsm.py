from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

import pytest

from recorder_core.buffers import LatestValueBuffer
from recorder_core.fsm import ALLOWED_TRANSITIONS, RecorderFSM, RecorderState
from recorder_core.manifest import load_manifest


MANIFEST_PATH = Path(__file__).parents[3] / "manifests" / "bi_so101_01.yaml"


class StubWriter:
    def __init__(self, staging_path: Path):
        self.staging_path = staging_path
        self.staging_path.mkdir(parents=True)
        self.frame_count = 0
        self.calls = []
        self.created_thread = threading.get_ident()
        self.call_threads = []
        self.validation_thread = None

    def add_frame(self, frame, *, task):
        self.calls.append(("add_frame", frame, task))
        self.call_threads.append(("add_frame", threading.get_ident()))
        self.frame_count += 1

    def save_episode(self):
        self.calls.append(("save_episode",))
        self.call_threads.append(("save_episode", threading.get_ident()))

    def discard_episode(self):
        self.calls.append(("discard_episode",))
        self.call_threads.append(("discard_episode", threading.get_ident()))

    def finalize(self):
        self.calls.append(("finalize",))
        self.call_threads.append(("finalize", threading.get_ident()))


@pytest.fixture
def harness(tmp_path):
    base = load_manifest(MANIFEST_PATH)
    manifest = replace(
        base,
        recording=replace(
            base.recording,
            staging_root=str(tmp_path / "staging"),
            dataset_root=str(tmp_path / "datasets" / "raw"),
        ),
    )
    gate = {"failure": (None, None)}
    writers = []
    transitions = []

    def writer_factory(session_id, dataset_id):
        writer = StubWriter(Path(manifest.recording.staging_root) / f"{session_id}.partial")
        writers.append(writer)
        return writer

    def validate(staging_path):
        assert writers[-1].calls[-1] == ("finalize",)
        writers[-1].validation_thread = threading.get_ident()
        (staging_path / "validation.json").write_text(
            '{"status":"PASS"}\n', encoding="utf-8"
        )

    fsm = RecorderFSM(
        manifest,
        manifest_path=MANIFEST_PATH,
        writer_factory=writer_factory,
        sample_supplier=lambda: {"frame": "value"},
        gate_checker=lambda sample: gate["failure"],
        validation_runner=validate,
        warmup_s=1.0,
        min_free_gb=0.5,
        disk_free_gb=lambda path: 10.0,
        transition_callback=transitions.append,
        lerobot_sha="test-sha",
    )
    yield fsm, gate, writers, transitions, tmp_path
    fsm.close()


def tick_until_state(fsm, expected, *, now_monotonic, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while fsm.state is not expected and time.monotonic() < deadline:
        fsm.tick(now_monotonic=now_monotonic)
        time.sleep(0.001)
    assert fsm.state is expected


def start_recording(fsm):
    assert fsm.start(
        "pick_box",
        session_id="session-1",
        now_monotonic=10.0,
        now_utc=datetime(2026, 7, 13, 12, 34, 56, tzinfo=timezone.utc),
    )
    assert fsm.state is RecorderState.PREFLIGHT
    tick_until_state(fsm, RecorderState.WARMUP, now_monotonic=10.0)
    assert fsm.state is RecorderState.WARMUP
    fsm.tick(now_monotonic=10.9)
    assert fsm.state is RecorderState.WARMUP
    fsm.tick(now_monotonic=11.0)
    assert fsm.state is RecorderState.RECORDING


def test_normal_transition_table_finalize_validate_and_promote(harness):
    fsm, _, writers, transitions, tmp_path = harness
    executor_thread = threading.get_ident()
    start_recording(fsm)

    fsm.tick(now_monotonic=11.1)
    assert fsm.frame_count == 1
    assert fsm.stop(now_monotonic=12.0)
    assert fsm.state is RecorderState.COMMITTING
    tick_until_state(fsm, RecorderState.READY, now_monotonic=12.0)

    assert fsm.state is RecorderState.READY
    assert [event.current for event in transitions] == [
        RecorderState.PREFLIGHT,
        RecorderState.WARMUP,
        RecorderState.RECORDING,
        RecorderState.COMMITTING,
        RecorderState.READY,
    ]
    assert [call[0] for call in writers[0].calls] == [
        "add_frame",
        "save_episode",
        "finalize",
    ]
    call_threads = dict(writers[0].call_threads)
    assert writers[0].created_thread != executor_thread
    assert call_threads["add_frame"] == executor_thread
    assert call_threads["save_episode"] != executor_thread
    assert call_threads["finalize"] != executor_thread
    assert writers[0].validation_thread != executor_thread
    expected_raw = (
        tmp_path
        / "datasets"
        / "raw"
        / "bi_so101_v1"
        / "pick_box"
        / "real"
        / "bi_so101_v1__pick_box__real__20260713_123456"
    )
    assert fsm.raw_path == expected_raw
    assert json.loads((expected_raw / "validation.json").read_text())["status"] == "PASS"
    session = json.loads(
        (tmp_path / "sessions" / "session-1" / "session.json").read_text()
    )
    assert session["result"] == "PASS"
    assert session["frame_count"] == 1
    assert session["lerobot_sha"] == "test-sha"
    assert (tmp_path / "sessions" / "session-1" / "manifest.yaml").is_file()


def test_transition_table_matches_v2_normal_abort_and_reset_edges():
    assert ALLOWED_TRANSITIONS == {
        RecorderState.IDLE: frozenset({RecorderState.PREFLIGHT}),
        RecorderState.PREFLIGHT: frozenset(
            {RecorderState.WARMUP, RecorderState.ABORTING}
        ),
        RecorderState.WARMUP: frozenset(
            {RecorderState.RECORDING, RecorderState.ABORTING}
        ),
        RecorderState.RECORDING: frozenset(
            {
                RecorderState.RESETTING,
                RecorderState.COMMITTING,
                RecorderState.ABORTING,
            }
        ),
        RecorderState.RESETTING: frozenset(
            {RecorderState.WARMUP, RecorderState.COMMITTING, RecorderState.ABORTING}
        ),
        RecorderState.COMMITTING: frozenset(
            {RecorderState.READY, RecorderState.ABORTING}
        ),
        RecorderState.READY: frozenset({RecorderState.IDLE}),
        RecorderState.ABORTING: frozenset({RecorderState.FAULT}),
        RecorderState.FAULT: frozenset({RecorderState.IDLE}),
    }


def test_recording_gate_failure_aborts_immediately_and_latches_fault(harness):
    fsm, gate, _, transitions, tmp_path = harness
    start_recording(fsm)
    fsm.tick(now_monotonic=11.1)
    gate["failure"] = ("top", "stale")

    fsm.tick(now_monotonic=11.2)
    assert fsm.state is RecorderState.ABORTING
    tick_until_state(fsm, RecorderState.FAULT, now_monotonic=11.2)

    assert fsm.state is RecorderState.FAULT
    assert transitions[-2].current is RecorderState.ABORTING
    assert transitions[-1].current is RecorderState.FAULT
    assert fsm.failure_source == "top"
    assert not list((tmp_path / "staging").glob("*.partial"))
    session = json.loads(
        (tmp_path / "sessions" / "session-1" / "session.json").read_text()
    )
    assert session["result"] == "FAIL"
    assert session["frame_count"] == 1
    with pytest.raises(RuntimeError, match="requires IDLE"):
        fsm.start("pick_box")
    fsm.reset()
    assert fsm.state is RecorderState.IDLE


def test_preflight_disk_failure_never_creates_writer(harness):
    fsm, _, writers, _, tmp_path = harness
    fsm.disk_free_gb = lambda path: 0.25

    assert fsm.start("pick_box", session_id="disk-fail", now_monotonic=1.0)
    assert fsm.state is RecorderState.PREFLIGHT
    tick_until_state(fsm, RecorderState.FAULT, now_monotonic=1.0)

    assert fsm.state is RecorderState.FAULT
    assert fsm.failure_source == "disk"
    assert not writers
    assert not list((tmp_path / "staging").glob("*.partial"))


def test_empty_instruction_list_falls_back_to_task_for_gateway_compatibility(harness):
    fsm, _, _, _, _ = harness

    assert fsm.start(
        "pick_box",
        task_instructions=[],
        session_id="default-instruction",
        now_monotonic=1.0,
    )

    assert fsm.task_instructions == ("pick_box",)
    assert fsm.current_instruction == "pick_box"
    tick_until_state(fsm, RecorderState.WARMUP, now_monotonic=1.0)


def test_warmup_requires_continuous_gate_success(harness):
    fsm, gate, _, transitions, _ = harness
    assert fsm.start("pick_box", session_id="warmup-fail", now_monotonic=1.0)
    tick_until_state(fsm, RecorderState.WARMUP, now_monotonic=1.0)
    gate["failure"] = ("action", "stale")

    fsm.tick(now_monotonic=1.5)
    assert fsm.state is RecorderState.ABORTING
    tick_until_state(fsm, RecorderState.FAULT, now_monotonic=1.5)

    assert fsm.state is RecorderState.FAULT
    assert [event.current for event in transitions] == [
        RecorderState.PREFLIGHT,
        RecorderState.WARMUP,
        RecorderState.ABORTING,
        RecorderState.FAULT,
    ]


def test_warmup_waits_for_post_start_snapshots_without_running_gate(harness):
    fsm, gate, _, transitions, _ = harness
    camera_buffer = LatestValueBuffer()
    start_snapshot = camera_buffer.update("before-start", received_monotonic=0.9)
    gate_checks = 0

    def warmup_ready():
        snapshot = camera_buffer.snapshot()
        return snapshot is not None and snapshot.seq > start_snapshot.seq

    def check_gate(sample):
        nonlocal gate_checks
        gate_checks += 1
        return gate["failure"]

    fsm.warmup_ready = warmup_ready
    fsm.gate_checker = check_gate

    assert fsm.start("pick_box", session_id="snapshot-race", now_monotonic=1.0)
    tick_until_state(fsm, RecorderState.WARMUP, now_monotonic=1.0)
    assert gate_checks == 1  # PREFLIGHT only

    fsm.tick(now_monotonic=10.0)
    assert fsm.state is RecorderState.WARMUP
    assert gate_checks == 1

    camera_buffer.update("after-start", received_monotonic=10.0)
    fsm.tick(now_monotonic=10.0)
    assert fsm.state is RecorderState.WARMUP
    assert gate_checks == 2
    fsm.tick(now_monotonic=11.0)
    assert fsm.state is RecorderState.RECORDING
    assert gate_checks == 3
    assert [event.current for event in transitions] == [
        RecorderState.PREFLIGHT,
        RecorderState.WARMUP,
        RecorderState.RECORDING,
    ]


def test_commit_validation_failure_is_not_promoted(harness):
    fsm, _, _, _, tmp_path = harness
    fsm.validation_runner = lambda path: (_ for _ in ()).throw(
        RuntimeError("validation failed")
    )
    start_recording(fsm)
    fsm.tick(now_monotonic=11.1)

    assert fsm.stop(now_monotonic=12.0)
    assert fsm.state is RecorderState.COMMITTING
    tick_until_state(fsm, RecorderState.FAULT, now_monotonic=12.0)

    assert fsm.state is RecorderState.FAULT
    assert fsm.failure_source == "commit"
    assert not list((tmp_path / "staging").glob("*.partial"))
    assert not list((tmp_path / "datasets").rglob("bi_so101_v1__*"))


def test_successful_session_log_failure_rolls_promotion_back(harness, monkeypatch):
    fsm, _, _, _, tmp_path = harness
    start_recording(fsm)
    fsm.tick(now_monotonic=11.1)
    original_write = fsm._write_session_log

    def fail_pass_log(result, **kwargs):
        if result == "PASS":
            raise OSError("session volume unavailable")
        original_write(result, **kwargs)

    monkeypatch.setattr(fsm, "_write_session_log", fail_pass_log)

    assert fsm.stop(now_monotonic=12.0)
    tick_until_state(fsm, RecorderState.FAULT, now_monotonic=12.0)

    assert fsm.state is RecorderState.FAULT
    assert fsm.raw_path is None
    assert not list((tmp_path / "staging").glob("*.partial"))
    assert not list((tmp_path / "datasets").rglob("bi_so101_v1__*"))


def test_slow_create_stays_in_preflight_and_restarts_fresh_warmup(harness):
    fsm, gate, _, transitions, _ = harness
    original_factory = fsm.writer_factory
    create_started = threading.Event()
    allow_create = threading.Event()
    gate_checks = 0

    def slow_factory(session_id, dataset_id):
        create_started.set()
        assert allow_create.wait(timeout=2.0)
        return original_factory(session_id, dataset_id)

    def check_gate(sample):
        nonlocal gate_checks
        gate_checks += 1
        return gate["failure"]

    fsm.writer_factory = slow_factory
    fsm.gate_checker = check_gate

    assert fsm.start("pick_box", session_id="slow-create", now_monotonic=1.0)
    assert create_started.wait(timeout=2.0)
    assert gate_checks == 1

    gate["failure"] = ("observation.state", "stale during create")
    fsm.tick(now_monotonic=100.0)
    fsm.tick(now_monotonic=101.0)
    assert fsm.state is RecorderState.PREFLIGHT
    assert gate_checks == 1
    assert [event.current for event in transitions] == [RecorderState.PREFLIGHT]

    gate["failure"] = (None, None)
    allow_create.set()
    tick_until_state(fsm, RecorderState.WARMUP, now_monotonic=101.0)

    fsm.tick(now_monotonic=101.9)
    assert fsm.state is RecorderState.WARMUP
    fsm.tick(now_monotonic=102.0)
    assert fsm.state is RecorderState.RECORDING
    assert gate_checks == 3


def test_add_frame_over_50ms_is_measured_and_counted(harness):
    fsm, _, writers, _, _ = harness
    timings = []
    fsm.frame_timing_callback = timings.append
    start_recording(fsm)
    writer = writers[0]
    original_add_frame = writer.add_frame

    def slow_add_frame(frame, *, task):
        time.sleep(0.055)
        original_add_frame(frame, task=task)

    writer.add_frame = slow_add_frame
    fsm.tick(now_monotonic=11.1)

    health = fsm.get_frame_write_health()
    assert health["last_tick_ms"] >= 50.0
    assert health["max_tick_ms"] == health["last_tick_ms"]
    assert health["ticks_over_50ms"] == 1
    assert timings == [health["last_tick_ms"]]


def test_slow_commit_stays_committing_until_worker_finishes(harness):
    fsm, _, writers, _, _ = harness
    start_recording(fsm)
    fsm.tick(now_monotonic=11.1)
    writer = writers[0]
    original_save_episode = writer.save_episode
    save_started = threading.Event()
    allow_save = threading.Event()

    def slow_save_episode():
        save_started.set()
        assert allow_save.wait(timeout=2.0)
        original_save_episode()

    writer.save_episode = slow_save_episode

    assert fsm.stop(now_monotonic=12.0)
    assert save_started.wait(timeout=2.0)
    fsm.tick(now_monotonic=100.0)
    assert fsm.state is RecorderState.COMMITTING

    allow_save.set()
    tick_until_state(fsm, RecorderState.READY, now_monotonic=100.0)


def test_multi_episode_retry_rotates_instruction_only_after_save(harness):
    fsm, _, writers, _, tmp_path = harness
    assert fsm.start(
        "pick_box",
        task_instructions=["pick the red box", "pick the blue box"],
        reset_time_s=5.0,
        session_id="multi-session",
        now_monotonic=10.0,
        now_utc=datetime(2026, 7, 13, 12, 34, 56, tzinfo=timezone.utc),
    )
    tick_until_state(fsm, RecorderState.WARMUP, now_monotonic=10.0)
    fsm.tick(now_monotonic=11.0)
    assert fsm.state is RecorderState.RECORDING

    fsm.tick(now_monotonic=11.1)
    fsm.tick(now_monotonic=11.2)
    assert fsm.retry(now_monotonic=11.3)
    assert fsm.current_instruction == "pick the red box"
    assert fsm.continue_after_reset()
    tick_until_state(fsm, RecorderState.WARMUP, now_monotonic=11.3)
    fsm.tick(now_monotonic=12.3)

    fsm.tick(now_monotonic=12.4)
    assert fsm.save_and_next(now_monotonic=12.5)
    assert fsm.continue_after_reset()
    tick_until_state(fsm, RecorderState.WARMUP, now_monotonic=12.5)
    assert fsm.episode_index == 1
    assert fsm.current_instruction == "pick the blue box"
    fsm.tick(now_monotonic=13.5)

    fsm.tick(now_monotonic=13.6)
    assert fsm.finish(now_monotonic=13.7)
    tick_until_state(fsm, RecorderState.READY, now_monotonic=13.7)

    calls = writers[0].calls
    assert [call[0] for call in calls] == [
        "add_frame",
        "add_frame",
        "discard_episode",
        "add_frame",
        "save_episode",
        "add_frame",
        "save_episode",
        "finalize",
    ]
    assert [call[2] for call in calls if call[0] == "add_frame"] == [
        "pick the red box",
        "pick the red box",
        "pick the red box",
        "pick the blue box",
    ]
    session = json.loads(
        (tmp_path / "sessions" / "multi-session" / "session.json").read_text()
    )
    assert session["result"] == "PASS"
    assert session["task_instructions"] == [
        "pick the red box",
        "pick the blue box",
    ]
    assert [(episode["index"], episode["result"]) for episode in session["episodes"]] == [
        (0, "RETRIED"),
        (0, "SAVED"),
        (1, "SAVED"),
    ]
    assert [episode["frames"] for episode in session["episodes"]] == [2, 1, 1]


def test_num_episodes_auto_finishes_after_save_and_next(harness):
    fsm, _, writers, transitions, _ = harness
    assert fsm.start(
        "pick_box",
        task_instructions=["first", "second"],
        num_episodes=2,
        reset_time_s=0.0,
        session_id="auto-finish",
        now_monotonic=20.0,
    )
    tick_until_state(fsm, RecorderState.WARMUP, now_monotonic=20.0)
    fsm.tick(now_monotonic=21.0)
    fsm.tick(now_monotonic=21.1)
    assert fsm.save_and_next(now_monotonic=21.2)
    tick_until_state(fsm, RecorderState.WARMUP, now_monotonic=21.2)

    fsm.tick(now_monotonic=22.2)
    fsm.tick(now_monotonic=22.3)
    assert fsm.save_and_next(now_monotonic=22.4)
    tick_until_state(fsm, RecorderState.READY, now_monotonic=22.4)

    assert fsm.episode_index == 2
    assert fsm.episodes_saved == 2
    assert [call[0] for call in writers[0].calls] == [
        "add_frame",
        "save_episode",
        "add_frame",
        "save_episode",
        "finalize",
    ]
    assert [event.current for event in transitions][-3:] == [
        RecorderState.RESETTING,
        RecorderState.COMMITTING,
        RecorderState.READY,
    ]


def test_episode_time_limit_automatically_saves_and_resets(harness):
    fsm, _, writers, _, _ = harness
    assert fsm.start(
        "pick_box",
        episode_time_s=0.5,
        reset_time_s=2.0,
        session_id="timed-episode",
        now_monotonic=30.0,
    )
    tick_until_state(fsm, RecorderState.WARMUP, now_monotonic=30.0)
    fsm.tick(now_monotonic=31.0)
    fsm.tick(now_monotonic=31.5)

    assert fsm.state is RecorderState.RESETTING
    deadline = time.monotonic() + 2.0
    while fsm._worker_future is not None and time.monotonic() < deadline:
        fsm.tick(now_monotonic=31.5)
        time.sleep(0.001)
    assert fsm._worker_future is None
    fsm.tick(now_monotonic=33.5)
    assert fsm.state is RecorderState.WARMUP
    assert [call[0] for call in writers[0].calls] == [
        "add_frame",
        "save_episode",
    ]
