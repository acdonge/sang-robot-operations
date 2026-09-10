from pathlib import Path
import time
from types import SimpleNamespace

import pytest
import rclpy

from so101_readonly_adapter.arm_node import SO101ArmReaderNode, select_arms
from so101_readonly_adapter.camera_node import (
    CameraSnapshot,
    CameraReaderNode,
    CameraWorker,
    extract_mjpg_payload,
    select_cameras,
)
from so101_readonly_adapter.config import CameraConfig, load_hardware_config


def hardware_config():
    path = Path(__file__).parents[3] / "config" / "bi_so101_hardware.yaml"
    return load_hardware_config(path)


def test_arm_and_camera_selection_preserve_config_order():
    config = hardware_config()

    arms = select_arms(config.arms, ["leader_right", "follower_left"])
    cameras = select_cameras(config.cameras, ["right_wrist", "top"])

    assert [arm.key for arm in arms] == ["follower_left", "leader_right"]
    assert [camera.key for camera in cameras] == ["top", "right_wrist"]
    with pytest.raises(ValueError, match="unknown arm"):
        select_arms(config.arms, ["missing"])


def test_extract_mjpg_payload_strips_capture_padding_and_rejects_invalid_data():
    frame = SimpleNamespace(tobytes=lambda: b"\xff\xd8jpeg\xff\xd9\x00\x00")

    assert extract_mjpg_payload(frame) == b"\xff\xd8jpeg\xff\xd9"
    with pytest.raises(RuntimeError, match="start"):
        extract_mjpg_payload(SimpleNamespace(tobytes=lambda: b"not-jpeg"))
    with pytest.raises(RuntimeError, match="end"):
        extract_mjpg_payload(SimpleNamespace(tobytes=lambda: b"\xff\xd8broken"))


def test_camera_worker_passes_through_mjpg_and_releases_device():
    events = []

    mjpg_frame = SimpleNamespace(
        tobytes=lambda: b"\xff\xd8jpeg-data\xff\xd9\x00\x00"
    )

    class Capture:
        def __init__(self, device):
            events.append(("open", device))

        def isOpened(self):
            return True

        def set(self, key, value):
            events.append(("set", key, value))
            return True

        def get(self, key):
            return {
                1: 1196444237.0,
                2: 640.0,
                3: 480.0,
                4: 30.0,
            }[key]

        def read(self):
            time.sleep(0.001)
            return True, mjpg_frame

        def release(self):
            events.append(("release",))

    cv2 = SimpleNamespace(
        CAP_V4L2=200,
        CAP_PROP_FOURCC=1,
        CAP_PROP_FRAME_WIDTH=2,
        CAP_PROP_FRAME_HEIGHT=3,
        CAP_PROP_FPS=4,
        CAP_PROP_CONVERT_RGB=5,
        VideoCapture=Capture,
        VideoWriter_fourcc=lambda *_value: 1196444237,
    )
    config = CameraConfig(
        key="test",
        device="/dev/video-test",
        topic="/camera/test/compressed",
        width=640,
        height=480,
        fps=30.0,
        fourcc="MJPG",
        fixed_fps=True,
    )
    worker = CameraWorker(
        config,
        reconnect_s=0.01,
        cv2_module=cv2,
        dynamic_framerate_setter=lambda device, *, enabled: events.append(
            ("dynamic_framerate", device, enabled)
        ),
    )

    worker.start()
    deadline = time.monotonic() + 1.0
    snapshot = worker.snapshot()
    while snapshot.sequence == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
        snapshot = worker.snapshot()
    worker.stop()

    assert snapshot.jpeg == b"\xff\xd8jpeg-data\xff\xd9"
    assert snapshot.error is None
    assert snapshot.captured_at_ns is not None
    assert ("open", "/dev/video-test") in events
    assert ("dynamic_framerate", "/dev/video-test", False) in events
    assert ("release",) in events


def test_camera_worker_skips_bounded_mjpg_warmup_frames():
    frames = [
        SimpleNamespace(tobytes=lambda: b"\x00" * 32),
        SimpleNamespace(tobytes=lambda: b"\x00" * 32),
        SimpleNamespace(tobytes=lambda: b"\xff\xd8ready\xff\xd9"),
    ]

    class Capture:
        def __init__(self, _device):
            self.index = 0

        def isOpened(self):
            return True

        def set(self, _key, _value):
            return True

        def get(self, key):
            return {1: 1196444237.0, 2: 640.0, 3: 480.0, 4: 30.0}[key]

        def read(self):
            frame = frames[min(self.index, len(frames) - 1)]
            self.index += 1
            time.sleep(0.001)
            return True, frame

        def release(self):
            pass

    cv2 = SimpleNamespace(
        CAP_PROP_FOURCC=1,
        CAP_PROP_FRAME_WIDTH=2,
        CAP_PROP_FRAME_HEIGHT=3,
        CAP_PROP_FPS=4,
        CAP_PROP_CONVERT_RGB=5,
        VideoCapture=Capture,
        VideoWriter_fourcc=lambda *_value: 1196444237,
    )
    worker = CameraWorker(
        CameraConfig(
            key="otos",
            device="/dev/otos",
            topic="/camera/otos/compressed",
            width=640,
            height=480,
            fps=30.0,
            fourcc="MJPG",
            mjpg_passthrough=True,
        ),
        reconnect_s=0.01,
        cv2_module=cv2,
    )

    worker.start()
    deadline = time.monotonic() + 1.0
    snapshot = worker.snapshot()
    while snapshot.sequence == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
        snapshot = worker.snapshot()
    worker.stop()

    assert snapshot.sequence > 0
    assert snapshot.jpeg == b"\xff\xd8ready\xff\xd9"
    assert snapshot.error is None
    assert snapshot.invalid_mjpg_frames == 2


@pytest.mark.parametrize(
    ("fourcc", "mjpg_passthrough", "fourcc_value"),
    (
        ("YUYV", True, 1448695129),
        ("UYVY", True, 1498831189),
        ("MJPG", False, 1196444237),
    ),
)
def test_camera_worker_encodes_non_passthrough_input_as_jpeg(
    fourcc,
    mjpg_passthrough,
    fourcc_value,
):
    encoded = SimpleNamespace(tobytes=lambda: b"\xff\xd8encoded\xff\xd9")

    class Capture:
        def __init__(self, _device):
            pass

        def isOpened(self):
            return True

        def set(self, _key, _value):
            return True

        def get(self, key):
            return {
                1: float(fourcc_value),
                2: 640.0,
                3: 480.0,
                4: 30.0,
            }[key]

        def read(self):
            time.sleep(0.001)
            return True, object()

        def release(self):
            pass

    cv2 = SimpleNamespace(
        CAP_PROP_FOURCC=1,
        CAP_PROP_FRAME_WIDTH=2,
        CAP_PROP_FRAME_HEIGHT=3,
        CAP_PROP_FPS=4,
        CAP_PROP_CONVERT_RGB=5,
        IMWRITE_JPEG_QUALITY=6,
        VideoCapture=Capture,
        VideoWriter_fourcc=lambda *_value: fourcc_value,
        imencode=lambda extension, frame, parameters: (
            extension == ".jpg" and frame is not None and parameters == [6, 85],
            encoded,
        ),
    )
    config = CameraConfig(
        key="d455_color",
        device="/dev/d455",
        topic="/camera/d455/compressed",
        width=640,
        height=480,
        fps=30.0,
        fourcc=fourcc,
        jpeg_quality=85,
        mjpg_passthrough=mjpg_passthrough,
    )
    worker = CameraWorker(config, reconnect_s=0.01, cv2_module=cv2)

    worker.start()
    deadline = time.monotonic() + 1.0
    snapshot = worker.snapshot()
    while snapshot.sequence == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
        snapshot = worker.snapshot()
    worker.stop()

    assert snapshot.jpeg == b"\xff\xd8encoded\xff\xd9"
    assert snapshot.error is None


def test_ros_nodes_do_not_replace_rclpy_internal_collections():
    config = hardware_config()

    class Bus:
        is_open = False

        def open(self):
            self.is_open = True

        def close(self):
            self.is_open = False

        def read_positions(self):
            return {motor_id: 2000 for motor_id in range(1, 7)}

    class Worker:
        def start(self):
            pass

        def stop(self):
            pass

        def snapshot(self):
            return CameraSnapshot(0, None, None)

    rclpy.init()
    arm_node = SO101ArmReaderNode(
        (config.arms[0],),
        reconnect_s=1.0,
        bus_factory=lambda _config, _calibration: Bus(),
    )
    camera_node = CameraReaderNode(
        (config.cameras[0],),
        reconnect_s=1.0,
        worker_factory=lambda _config, _reconnect: Worker(),
    )
    try:
        assert isinstance(arm_node._timers, list)
        assert isinstance(camera_node._publishers, list)
        assert len(arm_node._read_timers) == 1
        assert len(camera_node._camera_publishers) == 1
    finally:
        camera_node.destroy_node()
        arm_node.destroy_node()
        rclpy.shutdown()
