"""Threaded OpenCV camera publisher for manifest-compatible ROS 2 topics."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import math
import os
from pathlib import Path
import struct
import sys
import threading
import time
from typing import Any, Callable

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.utilities import remove_ros_args
from sensor_msgs.msg import CompressedImage

from so101_readonly_adapter.config import CameraConfig, load_hardware_config


@dataclass(frozen=True)
class CameraSnapshot:
    sequence: int
    jpeg: bytes | None
    error: str | None
    captured_at_ns: int | None = None
    invalid_mjpg_frames: int = 0


JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"
MJPG_INVALID_FRAME_LIMIT = 10
MJPG_INVALID_LOG_INTERVAL_S = 60.0
V4L2_CID_EXPOSURE_AUTO_PRIORITY = 0x009A0903
VIDIOC_S_CTRL = 0xC008561C


class MjpgPayloadError(RuntimeError):
    """Raised when raw MJPG bytes do not contain a complete JPEG frame."""


def set_v4l2_dynamic_framerate(device: str, *, enabled: bool) -> None:
    """Set the UVC control that permits auto exposure to reduce frame rate."""
    flags = os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(device, flags)
    try:
        control = struct.pack(
            "Ii",
            V4L2_CID_EXPOSURE_AUTO_PRIORITY,
            int(enabled),
        )
        fcntl.ioctl(descriptor, VIDIOC_S_CTRL, control)
    finally:
        os.close(descriptor)


def fourcc_text(value: float) -> str:
    encoded = int(value)
    return "".join(
        chr((encoded >> (8 * index)) & 0xFF) for index in range(4)
    ).rstrip("\x00")


def extract_mjpg_payload(frame: Any) -> bytes:
    payload = frame.tobytes()
    if not payload.startswith(JPEG_START):
        raise MjpgPayloadError("MJPG frame does not start with a JPEG marker")
    end = payload.rfind(JPEG_END)
    if end < len(JPEG_START):
        raise MjpgPayloadError("MJPG frame does not contain a JPEG end marker")
    return payload[: end + len(JPEG_END)]


def select_cameras(
    cameras: tuple[CameraConfig, ...],
    keys: list[str],
) -> tuple[CameraConfig, ...]:
    if not keys:
        return cameras
    requested = set(keys)
    available = {camera.key for camera in cameras}
    unknown = requested - available
    if unknown:
        raise ValueError("unknown camera keys: " + ", ".join(sorted(unknown)))
    return tuple(camera for camera in cameras if camera.key in requested)


class CameraWorker:
    def __init__(
        self,
        config: CameraConfig,
        *,
        reconnect_s: float,
        cv2_module: Any | None = None,
        dynamic_framerate_setter: Callable[..., None] | None = None,
    ) -> None:
        if cv2_module is None:
            import cv2 as cv2_module_import

            cv2_module = cv2_module_import
        self.config = config
        self.reconnect_s = reconnect_s
        self._cv2 = cv2_module
        self._set_dynamic_framerate = (
            dynamic_framerate_setter or set_v4l2_dynamic_framerate
        )
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sequence = 0
        self._jpeg: bytes | None = None
        self._error: str | None = None
        self._captured_at_ns: int | None = None
        self._invalid_mjpg_frames = 0
        self._thread = threading.Thread(
            target=self._run,
            name=f"camera-{config.key}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(2.0, self.reconnect_s + 1.0))

    def snapshot(self) -> CameraSnapshot:
        with self._lock:
            return CameraSnapshot(
                self._sequence,
                self._jpeg,
                self._error,
                self._captured_at_ns,
                self._invalid_mjpg_frames,
            )

    def _set_error(self, exc: Exception) -> None:
        with self._lock:
            self._error = f"{type(exc).__name__}: {exc}"

    def _open_capture(self):
        capture = self._cv2.VideoCapture(self.config.device)
        if not capture.isOpened():
            capture.release()
            raise ConnectionError(f"failed to open {self.config.device}")
        capture.set(
            self._cv2.CAP_PROP_FOURCC,
            self._cv2.VideoWriter_fourcc(*self.config.fourcc),
        )
        capture.set(self._cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        capture.set(self._cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        capture.set(self._cv2.CAP_PROP_FPS, self.config.fps)
        if self.config.fourcc == "MJPG" and self.config.mjpg_passthrough:
            if not capture.set(self._cv2.CAP_PROP_CONVERT_RGB, 0):
                capture.release()
                raise RuntimeError(
                    f"raw MJPG mode is unavailable for {self.config.device}"
                )
        else:
            capture.set(self._cv2.CAP_PROP_CONVERT_RGB, 1)
        if self.config.fixed_fps:
            try:
                self._set_dynamic_framerate(
                    self.config.device,
                    enabled=False,
                )
            except Exception:
                capture.release()
                raise
        actual_fourcc = fourcc_text(capture.get(self._cv2.CAP_PROP_FOURCC))
        actual_width = int(capture.get(self._cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(capture.get(self._cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = capture.get(self._cv2.CAP_PROP_FPS)
        expected = (
            self.config.fourcc,
            self.config.width,
            self.config.height,
        )
        actual = (actual_fourcc, actual_width, actual_height)
        if actual != expected or not math.isclose(
            actual_fps,
            self.config.fps,
            rel_tol=0.01,
        ):
            capture.release()
            raise RuntimeError(
                f"camera negotiated {actual_fourcc} "
                f"{actual_width}x{actual_height} @{actual_fps:g}fps; "
                f"expected {self.config.fourcc} "
                f"{self.config.width}x{self.config.height} "
                f"@{self.config.fps:g}fps"
            )
        return capture

    def _jpeg_bytes(self, frame: Any) -> bytes:
        if self.config.fourcc == "MJPG" and self.config.mjpg_passthrough:
            return extract_mjpg_payload(frame)
        ok, encoded = self._cv2.imencode(
            ".jpg",
            frame,
            [self._cv2.IMWRITE_JPEG_QUALITY, self.config.jpeg_quality],
        )
        if not ok:
            raise RuntimeError(f"JPEG encoding failed for {self.config.device}")
        return encoded.tobytes()

    def _run(self) -> None:
        while not self._stop.is_set():
            capture = None
            try:
                capture = self._open_capture()
                consecutive_invalid_mjpg_frames = 0
                while not self._stop.is_set():
                    ok, frame = capture.read()
                    captured_at_ns = time.time_ns()
                    if not ok or frame is None:
                        raise ConnectionError(
                            f"frame read failed from {self.config.device}"
                        )
                    try:
                        jpeg = self._jpeg_bytes(frame)
                    except MjpgPayloadError:
                        consecutive_invalid_mjpg_frames += 1
                        with self._lock:
                            self._invalid_mjpg_frames += 1
                        if consecutive_invalid_mjpg_frames <= MJPG_INVALID_FRAME_LIMIT:
                            continue
                        raise
                    consecutive_invalid_mjpg_frames = 0
                    with self._lock:
                        self._sequence += 1
                        self._jpeg = jpeg
                        self._error = None
                        self._captured_at_ns = captured_at_ns
            except Exception as exc:
                self._set_error(exc)
                self._stop.wait(self.reconnect_s)
            finally:
                if capture is not None:
                    capture.release()


WorkerFactory = Callable[[CameraConfig, float], CameraWorker]


class CameraReaderNode(Node):
    def __init__(
        self,
        cameras: tuple[CameraConfig, ...],
        *,
        reconnect_s: float,
        worker_factory: WorkerFactory | None = None,
    ) -> None:
        super().__init__("opencv_camera_reader")
        self._camera_publishers = {}
        self._workers = {}
        self._last_sequences = {}
        self._last_errors = {}
        self._last_invalid_mjpg_frames = {}
        self._last_logged_invalid_mjpg_frames = {}
        self._last_invalid_mjpg_log_at = {}
        factory = worker_factory or (
            lambda camera, reconnect: CameraWorker(
                camera,
                reconnect_s=reconnect,
            )
        )
        for camera in cameras:
            self._camera_publishers[camera.key] = self.create_publisher(
                CompressedImage,
                camera.topic,
                qos_profile_sensor_data,
            )
            worker = factory(camera, reconnect_s)
            self._workers[camera.key] = worker
            self._last_sequences[camera.key] = 0
            self._last_errors[camera.key] = None
            self._last_invalid_mjpg_frames[camera.key] = 0
            self._last_logged_invalid_mjpg_frames[camera.key] = 0
            self._last_invalid_mjpg_log_at[camera.key] = 0.0
            worker.start()
        publish_hz = max(camera.fps for camera in cameras) * 2.0
        self._timer = self.create_timer(1.0 / publish_hz, self._publish_latest)
        self.get_logger().info(
            f"JPEG output configured for {len(cameras)} camera(s)"
        )

    def _publish_latest(self) -> None:
        for key, worker in self._workers.items():
            snapshot = worker.snapshot()
            previous_error = self._last_errors[key]
            if snapshot.error != previous_error:
                if snapshot.error is None:
                    self.get_logger().info(f"{key}: camera stream recovered")
                else:
                    self.get_logger().error(f"{key}: {snapshot.error}")
                self._last_errors[key] = snapshot.error
            if (
                snapshot.jpeg is None
                or snapshot.captured_at_ns is None
                or snapshot.sequence == self._last_sequences[key]
            ):
                continue
            skipped = (
                snapshot.invalid_mjpg_frames
                - self._last_invalid_mjpg_frames[key]
            )
            if skipped > 0:
                now = time.monotonic()
                last_log_at = self._last_invalid_mjpg_log_at[key]
                if (
                    last_log_at == 0.0
                    or now - last_log_at >= MJPG_INVALID_LOG_INTERVAL_S
                ):
                    unreported = (
                        snapshot.invalid_mjpg_frames
                        - self._last_logged_invalid_mjpg_frames[key]
                    )
                    self.get_logger().warning(
                        f"{key}: skipped {unreported} invalid MJPG frame(s); "
                        f"{snapshot.invalid_mjpg_frames} total"
                    )
                    self._last_logged_invalid_mjpg_frames[key] = (
                        snapshot.invalid_mjpg_frames
                    )
                    self._last_invalid_mjpg_log_at[key] = now
            self._last_invalid_mjpg_frames[key] = snapshot.invalid_mjpg_frames
            message = CompressedImage()
            message.header.stamp.sec = snapshot.captured_at_ns // 1_000_000_000
            message.header.stamp.nanosec = snapshot.captured_at_ns % 1_000_000_000
            message.header.frame_id = key
            message.format = "jpeg"
            message.data = snapshot.jpeg
            self._camera_publishers[key].publish(message)
            if self._last_sequences[key] == 0:
                capture_age_ms = (time.time_ns() - snapshot.captured_at_ns) / 1e6
                self.get_logger().info(
                    f"{key}: first JPEG published "
                    f"({len(snapshot.jpeg)} bytes, capture age {capture_age_ms:.1f}ms)"
                )
            self._last_sequences[key] = snapshot.sequence

    def destroy_node(self) -> None:
        self._timer.cancel()
        for worker in self._workers.values():
            worker.stop()
        super().destroy_node()


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Threaded OpenCV ROS 2 camera adapter")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--camera", action="append", default=[])
    parser.add_argument("--reconnect-s", type=float, default=2.0)
    options = parser.parse_args(args)
    if options.reconnect_s <= 0.0:
        parser.error("reconnect-s must be positive")
    return options


def main(args: list[str] | None = None) -> None:
    raw_args = sys.argv if args is None else [sys.argv[0], *args]
    clean_args = remove_ros_args(args=raw_args)[1:]
    options = parse_args(clean_args)
    hardware = load_hardware_config(options.config)
    try:
        cameras = select_cameras(hardware.cameras, options.camera)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    rclpy.init(args=raw_args)
    node = CameraReaderNode(cameras, reconnect_s=options.reconnect_s)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
