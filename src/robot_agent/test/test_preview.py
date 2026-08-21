from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from robot_agent.preview import (
    CameraPreviewServer,
    HealthSnapshotStore,
    PreviewFrameStore,
)


JPEG = b"\xff\xd8test-frame\xff\xd9"


def test_frame_store_keeps_latest_and_waits_for_new_sequence():
    store = PreviewFrameStore()
    first = store.update("robot-1", "wrist", JPEG, received_monotonic=1.0)
    second = store.update("robot-1", "wrist", JPEG + b"next", received_monotonic=2.0)

    assert first.sequence == 1
    assert second.sequence == 2
    assert store.get("robot-1", "wrist") == second
    assert store.wait_for_newer("robot-1", "wrist", 1, timeout_s=0.01) == second
    assert store.wait_for_newer("robot-1", "wrist", 2, timeout_s=0.01) is None


def test_frame_store_rejects_non_jpeg():
    with pytest.raises(ValueError, match="not JPEG"):
        PreviewFrameStore().update("robot-1", "wrist", b"not-an-image")


def test_health_snapshot_store_rejects_sequence_rollback():
    store = HealthSnapshotStore()
    snapshot = {
        "robot_id": "robot-1",
        "boot_id": "boot-a",
        "sequence": 2,
    }
    store.update(snapshot)

    with pytest.raises(ValueError, match="did not advance"):
        store.update({**snapshot, "sequence": 1})


def test_preview_server_serves_health_snapshot_and_mjpeg():
    server = CameraPreviewServer("127.0.0.1", 0)
    server.frame_store.update("robot-1", "wrist cam", JPEG)
    health = {"robot_id": "robot-1", "boot_id": "boot-a", "sequence": 1}
    server.health_store.update(health)
    server.start()
    base = f"http://127.0.0.1:{server.port}"
    try:
        request = Request(
            base + "/healthz",
            headers={"Origin": "http://127.0.0.1:18091"},
        )
        with urlopen(request, timeout=1) as response:
            assert response.status == 200
            assert response.headers["Access-Control-Allow-Origin"] == (
                "http://127.0.0.1:18091"
            )
            assert response.read() == b'{"status":"ok"}'
        denied_request = Request(
            base + "/healthz",
            headers={"Origin": "https://untrusted.example"},
        )
        with urlopen(denied_request, timeout=1) as response:
            assert response.headers["Access-Control-Allow-Origin"] is None
        with urlopen(
            base + "/robots/robot-1/cameras/wrist%20cam.jpg", timeout=1
        ) as response:
            assert response.headers["Content-Type"] == "image/jpeg"
            assert response.read() == JPEG
        with urlopen(server.health_snapshot_url("robot-1"), timeout=1) as response:
            assert response.headers["Content-Type"] == "application/json"
            assert response.read() == (
                b'{"boot_id":"boot-a","robot_id":"robot-1","sequence":1}'
            )
        stream_request = Request(
            server.stream_url("robot-1", "wrist cam"),
            headers={"Origin": "http://127.0.0.1:8090"},
        )
        with urlopen(stream_request, timeout=1) as response:
            assert response.headers["Content-Type"].startswith(
                "multipart/x-mixed-replace"
            )
            assert response.headers["Access-Control-Allow-Origin"] == (
                "http://127.0.0.1:8090"
            )
            chunk = response.read1(256)
            if JPEG not in chunk:
                chunk += response.read1(256)
            assert b"--frame" in chunk
            assert JPEG in chunk
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/robots/robot-1/cameras/missing.jpg", timeout=1)
        assert error.value.code == 404
    finally:
        server.stop()
