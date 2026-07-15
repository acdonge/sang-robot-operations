import asyncio
import inspect
import json
from pathlib import Path

import pytest

from recorder_core.gateway import (
    GatewayAPIError,
    InferenceRequest,
    RobotHandle,
    StartRequest,
    TrainRequest,
    UploadRequest,
    contract_sources,
    create_app,
    mjpeg_part,
    parse_args,
    state_conflict,
    status_payload,
    wait_for_commit,
)
from recorder_core.hf_upload import HfUploadManager, UploadPreflightError
from recorder_core.inference_manager import SAFETY_WARNING, InferenceConflictError
from recorder_core.manifest import load_manifest
from recorder_core.train_manager import TrainConflictError


MANIFEST_PATH = Path(__file__).parents[3] / "manifests" / "bi_so101_01.yaml"


class StubNode:
    def __init__(self):
        self.manifest = load_manifest(MANIFEST_PATH)
        self.state = "IDLE"
        self.session_id = None
        self.raw_path = None
        self.failure_reason = None
        self.frames = 0
        self.abort_calls = 0
        self.start_options = None

    def get_status(self):
        return {"fsm": self._fsm(), **self.get_health()}

    def get_health(self):
        return {
            "sources": {
                key: {
                    "age_ms": float("inf") if key == "observation.state" else 4.5,
                    "seq": 0 if key == "observation.state" else 12,
                    "fresh": key != "observation.state",
                    "topics": {"ignored": {}},
                }
                for key in self.manifest.recording.required_sources
            }
        }

    def _fsm(self):
        return {
            "state": self.state,
            "session_id": self.session_id,
            "frame_count": self.frames,
            "raw_path": self.raw_path,
            "failure_reason": self.failure_reason,
            "episode_index": 0,
            "episodes_total": 0,
            "current_instruction": "instruction",
            "phase": self.state,
            "reset_remaining_s": 0.0 if self.state == "RESETTING" else None,
        }

    def start_recording(self, task, **options):
        if self.state == "READY":
            self.state = "IDLE"
        if self.state != "IDLE":
            raise RuntimeError(f"record_start requires IDLE or READY, got {self.state}")
        if not task:
            raise ValueError("task is empty")
        self.session_id = "session-1"
        self.start_options = options
        self.state = "PREFLIGHT"
        return True

    def finish_recording(self):
        if self.state != "RECORDING":
            raise RuntimeError(f"finish requires RECORDING, got {self.state}")
        self.state = "READY"
        self.raw_path = "/tmp/dataset"
        return True

    def save_next_recording(self):
        if self.state != "RECORDING":
            raise RuntimeError(f"save_and_next requires RECORDING, got {self.state}")
        self.state = "RESETTING"
        return True

    def retry_recording(self):
        if self.state != "RECORDING":
            raise RuntimeError(f"retry requires RECORDING, got {self.state}")
        self.state = "RESETTING"
        return True

    def continue_recording(self):
        if self.state != "RESETTING":
            raise RuntimeError(f"continue requires RESETTING, got {self.state}")
        self.state = "WARMUP"
        return True

    def abort_recording(self):
        self.abort_calls += 1
        self.state = "IDLE" if self.state == "FAULT" else "FAULT"
        return True

    def get_camera_jpeg(self, camera_key):
        del camera_key
        return b"\xff\xd8jpeg\xff\xd9"


def make_app(
    tmp_path,
    node=None,
    upload_manager=None,
    train_manager=None,
    inference_manager=None,
):
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir(exist_ok=True)
    (ui_dir / "index.html").write_text("gateway ui", encoding="utf-8")
    return create_app(
        [node or StubNode()],
        ui_dir=ui_dir,
        store_root=tmp_path / "store",
        upload_manager=upload_manager,
        train_manager=train_manager,
        inference_manager=inference_manager,
    )


def call_route(app, name, *args):
    endpoint = next(route.endpoint for route in app.routes if route.name == name)
    result = endpoint(*args)
    return asyncio.run(result) if inspect.isawaitable(result) else result


def make_indexed_dataset(tmp_path, *, status="PASS"):
    dataset_id = "contract__pick_box__real__20260714_120000"
    dataset = (
        tmp_path
        / "store"
        / "datasets"
        / "raw"
        / "contract"
        / "pick_box"
        / "real"
        / dataset_id
    )
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta" / "info.json").write_text(
        json.dumps({"total_episodes": 2, "total_frames": 20, "fps": 10}),
        encoding="utf-8",
    )
    (dataset / "validation.json").write_text(
        json.dumps({"status": status}), encoding="utf-8"
    )
    session = tmp_path / "store" / "sessions" / "session-1" / "session.json"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "dataset_id": dataset_id,
                "updated_at": "2026-07-14T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return dataset_id, dataset, session


def test_state_conflict_matches_contract():
    assert state_conflict("start", "IDLE") is None
    assert state_conflict("start", "READY") is None
    assert "got RECORDING" in state_conflict("start", "RECORDING")
    assert state_conflict("stop", "RECORDING") is None
    assert "got IDLE" in state_conflict("stop", "IDLE")
    assert state_conflict("save_next", "RECORDING") is None
    assert state_conflict("retry", "RECORDING") is None
    assert state_conflict("continue", "RESETTING") is None
    assert "got WARMUP" in state_conflict("finish", "WARMUP")


def test_contract_sources_normalizes_never_received_age():
    sources = contract_sources(StubNode().get_health())
    assert sources["observation.state"] == {
        "age_ms": None,
        "seq": 0,
        "fresh": False,
    }
    assert "topics" not in sources["action"]


def test_robot_health_start_stop_and_conflict_routes(tmp_path):
    node = StubNode()
    app = make_app(tmp_path, node)

    robots = call_route(app, "list_robots")
    assert robots == [
        {
            "robot_id": "bi_so101_01",
            "model": "bi_so101",
            "cameras": [
                "top",
                "left_side",
                "right_side",
                "left_wrist",
                "right_wrist",
            ],
            "fps": 30,
        }
    ]

    health = call_route(app, "robot_health", "bi_so101_01")
    assert health["fsm_state"] == "IDLE"
    assert health["sources"]["observation.state"]["age_ms"] is None

    start = call_route(
        app, "record_start", "bi_so101_01", StartRequest(task="fold_towel")
    )
    assert start == {"session_id": "session-1"}

    with pytest.raises(GatewayAPIError) as conflict:
        call_route(
            app, "record_start", "bi_so101_01", StartRequest(task="fold_towel")
        )
    assert conflict.value.status_code == 409

    node.state = "RECORDING"
    node.frames = 120
    stop = call_route(app, "record_stop", "bi_so101_01")
    assert stop == {
        "session_id": "session-1",
        "dataset_path": "/tmp/dataset",
    }

    with pytest.raises(GatewayAPIError) as conflict:
        call_route(app, "record_stop", "bi_so101_01")
    assert conflict.value.status_code == 409


def test_ready_start_fault_abort_and_static_mount(tmp_path):
    node = StubNode()
    node.state = "READY"
    app = make_app(tmp_path, node)

    start = call_route(
        app, "record_start", "bi_so101_01", StartRequest(task="pick_box")
    )
    assert start == {"session_id": "session-1"}

    node.state = "FAULT"
    abort = call_route(app, "record_abort", "bi_so101_01")
    assert abort == {"aborted": True}
    assert node.state == "IDLE"

    static_mount = app.routes[-1]
    assert static_mount.name == "ui"
    assert static_mount.app.html is True


def test_status_and_mjpeg_parts_keep_contract_and_original_jpeg_bytes():
    node = StubNode()
    payload = status_payload(RobotHandle(node))
    assert payload["fsm_state"] == "IDLE"
    assert payload["phase"] == "IDLE"
    assert payload["episode_index"] == 0
    assert payload["episodes_total"] == 0
    assert payload["current_instruction"] == "instruction"
    assert set(payload["system"]) == {"cpu_pct", "ram_free_gb", "disk_free_gb"}
    assert payload["message"] == ""

    jpeg = b"\xff\xd8original-compressed-bytes\xff\xd9"
    part = mjpeg_part(jpeg)
    assert part.startswith(b"--frame\r\nContent-Type: image/jpeg\r\n")
    assert f"Content-Length: {len(jpeg)}".encode() in part
    assert jpeg in part


def test_unknown_robot_and_camera_return_error_object(tmp_path):
    app = make_app(tmp_path)
    with pytest.raises(GatewayAPIError) as unknown_robot:
        call_route(app, "robot_health", "missing")
    assert unknown_robot.value.status_code == 404

    with pytest.raises(GatewayAPIError) as unknown_camera:
        call_route(app, "camera_stream", "bi_so101_01", "missing")
    assert unknown_camera.value.status_code == 404

    handler = app.exception_handlers[GatewayAPIError]
    response = asyncio.run(handler(None, unknown_camera.value))
    assert json.loads(response.body) == {"error": "unknown camera key: missing"}


def test_dataset_list_and_upload_routes_use_index_and_injected_api(tmp_path):
    dataset_id, dataset, session_path = make_indexed_dataset(tmp_path)

    class FakeApi:
        def __init__(self):
            self.calls = []

        def create_repo(self, **kwargs):
            self.calls.append(("create", kwargs))

        def upload_folder(self, **kwargs):
            self.calls.append(("upload", kwargs))

    api = FakeApi()
    uploader = HfUploadManager(
        api_factory=lambda: api,
        now=lambda: "2026-07-14T13:00:00+00:00",
    )
    app = make_app(tmp_path, upload_manager=uploader)

    datasets = call_route(app, "list_datasets")
    assert len(datasets) == 1
    assert set(datasets[0]) == {
        "dataset_id",
        "contract",
        "task",
        "domain",
        "path",
        "episodes",
        "frames",
        "fps",
        "size_mb",
        "created",
        "validation_status",
        "session_id",
    }
    assert datasets[0]["dataset_id"] == dataset_id
    assert datasets[0]["path"] == str(dataset)

    started = call_route(
        app,
        "upload_dataset",
        dataset_id,
        UploadRequest(repo_id="owner/repo", token="secret", private=True),
    )
    assert uploader.wait(started["job_id"], 1.0)
    assert call_route(app, "upload_status", started["job_id"])["state"] == "done"
    assert [name for name, _ in api.calls] == ["create", "upload"]
    session = json.loads(session_path.read_text(encoding="utf-8"))
    assert session["hf_repo_id"] == "owner/repo"

    with pytest.raises(GatewayAPIError) as missing:
        call_route(app, "upload_status", "missing")
    assert missing.value.status_code == 404


def test_dataset_upload_preflight_and_active_conflicts_are_409(tmp_path):
    dataset_id, _, _ = make_indexed_dataset(tmp_path, status="FAIL")
    app = make_app(tmp_path, upload_manager=HfUploadManager(api_factory=lambda: None))
    request = UploadRequest(repo_id="owner/repo", token="secret", private=False)

    with pytest.raises(GatewayAPIError) as invalid:
        call_route(app, "upload_dataset", dataset_id, request)
    assert invalid.value.status_code == 409
    assert "not PASS" in invalid.value.error

    class BusyUploader:
        def start_upload(self, **kwargs):
            del kwargs
            raise UploadPreflightError("upload already in progress for dataset")

        def get_status(self, job_id):
            del job_id
            raise KeyError

        def events_after(self, sequence):
            return sequence, []

    validation_path = (
        tmp_path
        / "store"
        / "datasets"
        / "raw"
        / "contract"
        / "pick_box"
        / "real"
        / dataset_id
        / "validation.json"
    )
    validation_path.write_text(
        json.dumps({"status": "PASS"}), encoding="utf-8"
    )
    busy_app = make_app(tmp_path, upload_manager=BusyUploader())
    with pytest.raises(GatewayAPIError) as active:
        call_route(busy_app, "upload_dataset", dataset_id, request)
    assert active.value.status_code == 409
    assert "already in progress" in active.value.error


def test_training_routes_use_index_enforce_conflicts_and_stop_contract(tmp_path):
    dataset_id, dataset, _ = make_indexed_dataset(tmp_path)

    class FakeTrainer:
        def __init__(self):
            self.calls = []
            self.runs = [{"run_id": "older", "state": "DONE"}]
            self.stop_result = True

        def list_runs(self):
            return self.runs

        def start_train(self, **kwargs):
            self.calls.append(kwargs)
            return "run-1"

        def stop(self, run_id):
            if run_id == "missing":
                raise KeyError(run_id)
            self.calls.append(("stop", run_id))
            return self.stop_result

        def events_after(self, sequence):
            return sequence, []

    trainer = FakeTrainer()
    app = make_app(tmp_path, train_manager=trainer)

    assert call_route(app, "list_runs") == trainer.runs
    started = call_route(
        app,
        "train_start",
        TrainRequest(dataset_id=dataset_id, policy_type="act", steps=20),
    )
    assert started == {"run_id": "run-1"}
    assert trainer.calls[0] == {
        "dataset_id": dataset_id,
        "dataset_path": str(dataset),
        "policy_type": "act",
        "steps": 20,
    }
    assert call_route(app, "train_stop", "run-1") == {"run_id": "run-1"}

    with pytest.raises(GatewayAPIError) as missing:
        call_route(app, "train_stop", "missing")
    assert missing.value.status_code == 404

    trainer.stop_result = False
    with pytest.raises(GatewayAPIError) as inactive:
        call_route(app, "train_stop", "run-1")
    assert inactive.value.status_code == 409

    with pytest.raises(GatewayAPIError) as unknown_dataset:
        call_route(
            app,
            "train_start",
            TrainRequest(dataset_id="missing", policy_type="act", steps=20),
        )
    assert unknown_dataset.value.status_code == 404


def test_training_active_conflict_is_409(tmp_path):
    dataset_id, _, _ = make_indexed_dataset(tmp_path)

    class BusyTrainer:
        def list_runs(self):
            return []

        def start_train(self, **kwargs):
            del kwargs
            raise TrainConflictError("another training run is already active")

        def stop(self, run_id):
            del run_id
            return False

        def events_after(self, sequence):
            return sequence, []

    app = make_app(tmp_path, train_manager=BusyTrainer())
    with pytest.raises(GatewayAPIError) as conflict:
        call_route(
            app,
            "train_start",
            TrainRequest(dataset_id=dataset_id, policy_type="act", steps=20),
        )
    assert conflict.value.status_code == 409
    assert "already active" in conflict.value.error


def test_inference_policy_start_stop_warning_and_recording_conflict(tmp_path):
    node = StubNode()

    class FakeInferencer:
        def __init__(self):
            self.calls = []
            self.conflict = False

        def list_policies(self):
            return [{"run_id": "run-1", "policy_type": "act"}]

        def start(self, run_id, started_node):
            if self.conflict:
                raise InferenceConflictError("another inference is already active")
            self.calls.append(("start", run_id, started_node))
            return {"state": "RUNNING"}

        def stop(self):
            self.calls.append(("stop",))
            return {
                "run_id": "run-1",
                "robot_id": "bi_so101_01",
                "state": "STOPPED",
            }

        def status(self):
            return {
                "type": "inference",
                "state": "STOPPED",
                "hz": 0.0,
                "last_action_age_ms": None,
            }

    inferencer = FakeInferencer()
    app = make_app(tmp_path, node=node, inference_manager=inferencer)

    assert call_route(app, "list_policies") == [
        {"run_id": "run-1", "policy_type": "act"}
    ]
    body = InferenceRequest(run_id="run-1", robot_id="bi_so101_01")
    started = call_route(app, "inference_start", body)
    assert started == {
        "run_id": "run-1",
        "robot_id": "bi_so101_01",
        "state": "RUNNING",
        "warning": SAFETY_WARNING,
    }
    assert inferencer.calls == [("start", "run-1", node)]
    assert call_route(app, "inference_stop") == {
        "run_id": "run-1",
        "robot_id": "bi_so101_01",
        "state": "STOPPED",
    }

    node.state = "RECORDING"
    with pytest.raises(GatewayAPIError) as recording:
        call_route(app, "inference_start", body)
    assert recording.value.status_code == 409
    assert "got RECORDING" in recording.value.error

    node.state = "IDLE"
    inferencer.conflict = True
    with pytest.raises(GatewayAPIError) as conflict:
        call_route(app, "inference_start", body)
    assert conflict.value.status_code == 409
    assert "already active" in conflict.value.error


def test_websocket_broadcasts_upload_events(tmp_path):
    upload_event = {
        "type": "upload",
        "job_id": "job-1",
        "dataset_id": "dataset-1",
        "state": "uploading",
        "detail": "uploading dataset folder",
    }

    class EventUploader:
        def events_after(self, sequence):
            return (1, [upload_event]) if sequence == 0 else (1, [])

    train_event = {
        "type": "train",
        "run_id": "run-1",
        "state": "RUNNING",
        "step": 1,
        "loss": 0.5,
    }

    class EventTrainer:
        def events_after(self, sequence):
            return (1, [train_event]) if sequence == 0 else (1, [])

    inference_event = {
        "type": "inference",
        "state": "RUNNING",
        "hz": 29.8,
        "last_action_age_ms": 4.0,
    }

    class EventInferencer:
        def status(self):
            return inference_event

    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def accept(self):
            pass

        async def receive(self):
            return {"type": "websocket.disconnect"}

        async def send_json(self, payload):
            self.sent.append(payload)

    app = make_app(
        tmp_path,
        upload_manager=EventUploader(),
        train_manager=EventTrainer(),
        inference_manager=EventInferencer(),
    )
    websocket = FakeWebSocket()

    call_route(app, "websocket_status", websocket)

    assert upload_event in websocket.sent
    assert train_event in websocket.sent
    assert inference_event in websocket.sent


def test_commit_wait_reports_fault_and_sixty_second_timeout():
    node = StubNode()
    node.state = "FAULT"
    node.failure_reason = "validation failed"
    with pytest.raises(GatewayAPIError) as fault:
        asyncio.run(wait_for_commit(node))
    assert fault.value.status_code == 500
    assert fault.value.error == "validation failed"

    node.state = "COMMITTING"
    with pytest.raises(GatewayAPIError) as timeout:
        asyncio.run(wait_for_commit(node, timeout_s=0.0))
    assert timeout.value.status_code == 504


def test_extended_start_and_episode_control_routes(tmp_path):
    node = StubNode()
    app = make_app(tmp_path, node)
    body = StartRequest(
        task="sort_blocks",
        task_instructions=["red first", "blue first"],
        episode_time_s=20.0,
        reset_time_s=5.0,
        num_episodes=2,
        rosbag=True,
    )

    assert call_route(app, "record_start", "bi_so101_01", body) == {
        "session_id": "session-1"
    }
    assert node.start_options == {
        "task_instructions": ["red first", "blue first"],
        "episode_time_s": 20.0,
        "reset_time_s": 5.0,
        "num_episodes": 2,
        "rosbag": True,
    }

    node.state = "RECORDING"
    assert call_route(app, "record_retry", "bi_so101_01") == {"accepted": True}
    assert call_route(app, "record_continue", "bi_so101_01") == {"accepted": True}
    node.state = "RECORDING"
    assert call_route(app, "record_save_next", "bi_so101_01") == {
        "accepted": True
    }
    with pytest.raises(GatewayAPIError) as conflict:
        call_route(app, "record_save_next", "bi_so101_01")
    assert conflict.value.status_code == 409

    node.state = "RECORDING"
    assert call_route(app, "record_finish", "bi_so101_01") == {
        "session_id": "session-1",
        "dataset_path": "/tmp/dataset",
    }


def test_cli_accepts_multiple_manifests_and_duplicate_robot_ids_are_rejected():
    options = parse_args(["first.yaml", "second.yaml", "--ui-dir", "/tmp/ui"])
    assert options.manifest_path == [Path("first.yaml"), Path("second.yaml")]
    assert options.ui_dir == Path("/tmp/ui")

    node = StubNode()
    with pytest.raises(ValueError, match="duplicate robot_id"):
        create_app([node, node], ui_dir="/tmp/ui")
