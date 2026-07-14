import asyncio
import json
from pathlib import Path

import pytest

from recorder_core.gateway import (
    GatewayAPIError,
    RobotHandle,
    StartRequest,
    contract_sources,
    create_app,
    mjpeg_part,
    parse_args,
    state_conflict,
    status_payload,
    wait_for_commit,
)
from recorder_core.manifest import load_manifest


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
        }

    def start_recording(self, task):
        if self.state == "READY":
            self.state = "IDLE"
        if self.state != "IDLE":
            raise RuntimeError(f"record_start requires IDLE or READY, got {self.state}")
        if not task:
            raise ValueError("task is empty")
        self.session_id = "session-1"
        self.state = "PREFLIGHT"
        return True

    def stop_recording(self):
        if self.state != "RECORDING":
            raise RuntimeError(f"record_stop requires RECORDING, got {self.state}")
        self.state = "READY"
        self.raw_path = "/tmp/dataset"
        return True

    def abort_recording(self):
        self.abort_calls += 1
        self.state = "IDLE" if self.state == "FAULT" else "FAULT"
        return True

    def get_camera_jpeg(self, camera_key):
        del camera_key
        return b"\xff\xd8jpeg\xff\xd9"


def make_app(tmp_path, node=None):
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    (ui_dir / "index.html").write_text("gateway ui", encoding="utf-8")
    return create_app([node or StubNode()], ui_dir=ui_dir)


def call_route(app, name, *args):
    endpoint = next(route.endpoint for route in app.routes if route.name == name)
    return asyncio.run(endpoint(*args))


def test_state_conflict_matches_contract():
    assert state_conflict("start", "IDLE") is None
    assert state_conflict("start", "READY") is None
    assert "got RECORDING" in state_conflict("start", "RECORDING")
    assert state_conflict("stop", "RECORDING") is None
    assert "got IDLE" in state_conflict("stop", "IDLE")


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


def test_cli_accepts_multiple_manifests_and_duplicate_robot_ids_are_rejected():
    options = parse_args(["first.yaml", "second.yaml", "--ui-dir", "/tmp/ui"])
    assert options.manifest_path == [Path("first.yaml"), Path("second.yaml")]
    assert options.ui_dir == Path("/tmp/ui")

    node = StubNode()
    with pytest.raises(ValueError, match="duplicate robot_id"):
        create_app([node, node], ui_dir="/tmp/ui")
