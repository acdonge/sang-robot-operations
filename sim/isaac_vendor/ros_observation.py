"""Read-only Isaac Sim camera and ROS 2 observation runtime."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any

import cv2
import numpy as np

from .profile import CameraContract, VendorProfile


@dataclass(frozen=True)
class CameraBinding:
    contract: CameraContract
    sensor: Any


@dataclass
class _PublishStats:
    count: int = 0
    first_monotonic: float | None = None
    last_monotonic: float | None = None
    last_payload_bytes: int = 0
    last_width: int | None = None
    last_height: int | None = None
    last_pixel_mean: float | None = None
    last_pixel_stddev: float | None = None

    def mark(
        self,
        *,
        payload_bytes: int,
        width: int | None = None,
        height: int | None = None,
        pixel_mean: float | None = None,
        pixel_stddev: float | None = None,
    ) -> None:
        now = time.monotonic()
        if self.first_monotonic is None:
            self.first_monotonic = now
        self.last_monotonic = now
        self.count += 1
        self.last_payload_bytes = payload_bytes
        self.last_width = width
        self.last_height = height
        if pixel_mean is not None:
            self.last_pixel_mean = pixel_mean
        if pixel_stddev is not None:
            self.last_pixel_stddev = pixel_stddev

    def report(self, minimum_rate_hz: float) -> dict[str, Any]:
        duration = (
            0.0
            if self.first_monotonic is None or self.last_monotonic is None
            else self.last_monotonic - self.first_monotonic
        )
        rate_hz = (self.count - 1) / duration if self.count > 1 and duration > 0 else None
        return {
            "count": self.count,
            "rate_hz": rate_hz,
            "minimum_rate_hz": minimum_rate_hz,
            "rate_ok": rate_hz is not None and rate_hz >= minimum_rate_hz,
            "last_payload_bytes": self.last_payload_bytes,
            "last_width": self.last_width,
            "last_height": self.last_height,
            "last_pixel_mean": self.last_pixel_mean,
            "last_pixel_stddev": self.last_pixel_stddev,
        }


def enable_observation_extensions(app: Any) -> None:
    """Enable only the Isaac extensions needed to publish RGB observations."""
    from isaacsim.core.utils.extensions import enable_extension

    enable_extension("isaacsim.sensors.camera")
    enable_extension("isaacsim.ros2.bridge")
    app.update()
    app.update()


def _usd_look_at_quaternion(
    eye: tuple[float, float, float] | np.ndarray,
    target: tuple[float, float, float] | np.ndarray,
) -> np.ndarray:
    from pxr import Gf

    eye_array = np.asarray(eye, dtype=float)
    target_array = np.asarray(target, dtype=float)
    if np.linalg.norm(target_array - eye_array) <= 1e-9:
        raise RuntimeError("camera eye and target must be different")
    matrix = Gf.Matrix4d().SetLookAt(
        Gf.Vec3d(*eye_array),
        Gf.Vec3d(*target_array),
        Gf.Vec3d(0.0, 0.0, 1.0),
    ).GetInverse()
    quaternion = matrix.ExtractRotation().GetQuat()
    imaginary = quaternion.GetImaginary()
    return np.asarray(
        [
            quaternion.GetReal(),
            imaginary[0],
            imaginary[1],
            imaginary[2],
        ],
        dtype=np.float32,
    )


def create_camera_bindings(profile: VendorProfile) -> dict[str, CameraBinding]:
    """Wrap vendor cameras or provision profile-declared runtime cameras."""
    from isaacsim.sensors.camera import Camera

    frequency = int(round(profile.observation.publish_rate_hz))
    bindings: dict[str, CameraBinding] = {}
    for contract in profile.cameras:
        sensor = Camera(
            prim_path=contract.prim_path,
            name=f"sang_{profile.profile_id}_{contract.key}",
            frequency=frequency,
            resolution=(contract.width, contract.height),
        )
        if contract.mode == "world_look_at":
            sensor.set_world_pose(
                position=np.asarray(contract.position, dtype=float),
                orientation=_usd_look_at_quaternion(
                    contract.position,
                    contract.target,
                ),
                camera_axes="usd",
            )
        elif contract.mode == "link_mount_look_at":
            sensor.set_local_pose(
                translation=np.asarray(contract.translation, dtype=float),
                orientation=np.asarray([1.0, 0.0, 0.0, 0.0]),
                camera_axes="usd",
            )
        if contract.focal_length is not None:
            sensor.set_focal_length(contract.focal_length)
        if contract.focus_distance is not None:
            sensor.set_focus_distance(contract.focus_distance)
        if contract.horizontal_aperture is not None:
            sensor.set_horizontal_aperture(contract.horizontal_aperture)
        if contract.clipping_range is not None:
            sensor.set_clipping_range(*contract.clipping_range)
        bindings[contract.key] = CameraBinding(contract, sensor)
    return bindings


def initialize_cameras(bindings: dict[str, CameraBinding]) -> None:
    for binding in bindings.values():
        binding.sensor.initialize()


def finalize_link_camera_poses(bindings: dict[str, CameraBinding]) -> None:
    """Apply workcell look-at offsets after the articulation reaches its ready pose."""
    for binding in bindings.values():
        contract = binding.contract
        if contract.mode != "link_mount_look_at":
            continue
        eye, _ = binding.sensor.get_world_pose(camera_axes="usd")
        eye = np.asarray(eye, dtype=float)
        target = np.asarray(contract.target, dtype=float)
        direction = target - eye
        distance = float(np.linalg.norm(direction))
        if not math.isfinite(distance) or distance <= 1e-9:
            raise RuntimeError(f"invalid look-at pose for camera {contract.key}")
        adjusted_eye = eye + direction / distance * contract.forward_offset_m
        binding.sensor.set_world_pose(
            position=adjusted_eye,
            orientation=_usd_look_at_quaternion(adjusted_eye, target),
            camera_axes="usd",
        )


class RosObservationPublisher:
    """Publish JointState and JPEG RGB frames without creating subscribers."""

    def __init__(
        self,
        profile: VendorProfile,
        articulation: Any,
        joint_indices: list[int],
        cameras: dict[str, CameraBinding],
    ) -> None:
        import rclpy
        from rclpy.context import Context
        from rclpy.node import Node
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import CompressedImage, JointState

        self._rclpy = rclpy
        self._context = Context()
        rclpy.init(args=None, context=self._context)
        self._node = Node(
            f"sang_isaac_{profile.profile_id}_observation",
            context=self._context,
            use_global_arguments=False,
            enable_rosout=False,
            start_parameter_services=False,
        )
        self._profile = profile
        self._articulation = articulation
        self._joint_indices = joint_indices
        self._cameras = cameras
        self._joint_message_class = JointState
        self._image_message_class = CompressedImage
        self._joint_publisher = self._node.create_publisher(
            JointState,
            profile.observation.joint_state_topic,
            qos_profile_sensor_data,
        )
        self._camera_publishers = {
            key: self._node.create_publisher(
                CompressedImage,
                binding.contract.topic,
                qos_profile_sensor_data,
            )
            for key, binding in cameras.items()
        }
        self._joint_stats = _PublishStats()
        self._camera_stats = {key: _PublishStats() for key in cameras}

    def publish(self) -> None:
        stamp = self._node.get_clock().now().to_msg()
        positions = np.asarray(self._articulation.get_joint_positions())[
            0, self._joint_indices
        ]
        velocities = np.asarray(self._articulation.get_joint_velocities())[
            0, self._joint_indices
        ]
        joint_message = self._joint_message_class()
        joint_message.header.stamp = stamp
        joint_message.header.frame_id = self._profile.observation.joint_state_frame_id
        joint_message.name = list(self._profile.controlled_joints)
        joint_message.position = [float(value) for value in positions]
        joint_message.velocity = [float(value) for value in velocities]
        self._joint_publisher.publish(joint_message)
        self._joint_stats.mark(payload_bytes=(len(positions) + len(velocities)) * 8)

        for key, binding in self._cameras.items():
            rgba = np.asarray(binding.sensor.get_rgba())
            expected_shape = (binding.contract.height, binding.contract.width)
            if rgba.ndim != 3 or rgba.shape[:2] != expected_shape or rgba.shape[2] < 3:
                raise RuntimeError(
                    f"camera {key} returned {rgba.shape}; expected "
                    f"({expected_shape[0]}, {expected_shape[1]}, 3+)"
                )
            rgb = np.ascontiguousarray(rgba[:, :, :3], dtype=np.uint8)
            success, encoded = cv2.imencode(
                ".jpg",
                cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
                [cv2.IMWRITE_JPEG_QUALITY, self._profile.observation.jpeg_quality],
            )
            if not success:
                raise RuntimeError(f"failed to encode camera {key} as JPEG")
            payload = encoded.tobytes()
            image_message = self._image_message_class()
            image_message.header.stamp = stamp
            image_message.header.frame_id = binding.contract.frame_id
            image_message.format = "jpeg"
            image_message.data = payload
            self._camera_publishers[key].publish(image_message)
            stats = self._camera_stats[key]
            sample_pixels = stats.count == 0
            stats.mark(
                payload_bytes=len(payload),
                width=binding.contract.width,
                height=binding.contract.height,
                pixel_mean=float(rgb.mean()) if sample_pixels else None,
                pixel_stddev=float(rgb.std()) if sample_pixels else None,
            )

    def report(self) -> dict[str, Any]:
        joint_report = self._joint_stats.report(
            self._profile.observation.joint_minimum_rate_hz
        )
        camera_reports = {
            key: self._camera_stats[key].report(binding.contract.minimum_rate_hz)
            for key, binding in self._cameras.items()
        }
        all_rates_ok = joint_report["rate_ok"] and all(
            item["rate_ok"] for item in camera_reports.values()
        )
        images_valid = all(
            item["last_payload_bytes"] > 0
            and item["last_pixel_stddev"] is not None
            and item["last_pixel_stddev"] > 1.0
            for item in camera_reports.values()
        )
        node_publisher_count = sum(1 for _ in self._node.publishers)
        observation_publisher_count = 1 + len(self._camera_publishers)
        subscription_count = sum(1 for _ in self._node.subscriptions)
        return {
            "node_name": self._node.get_name(),
            "joint_state": {
                "topic": self._profile.observation.joint_state_topic,
                **joint_report,
            },
            "cameras": {
                key: {
                    "topic": self._cameras[key].contract.topic,
                    **report,
                }
                for key, report in camera_reports.items()
            },
            "all_rates_ok": bool(all_rates_ok),
            "images_valid": images_valid,
            "publisher_count": observation_publisher_count,
            "ros_internal_publisher_count": (
                node_publisher_count - observation_publisher_count
            ),
            "subscription_count": subscription_count,
            "hardware_command_count": 0,
            "read_only": subscription_count == 0,
        }

    def close(self) -> None:
        self._node.destroy_node()
        if self._context.ok():
            self._rclpy.shutdown(context=self._context)
