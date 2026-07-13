"""Manifest-driven synthetic ROS publisher for recorder verification."""

from dataclasses import dataclass
import time

import cv2
import numpy as np
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CompressedImage, JointState
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

from recorder_core.manifest import ManifestValidationError, load_manifest


@dataclass(frozen=True)
class FeaturePublisher:
    source_key: str
    publisher: object
    names: tuple[str, ...]
    phase_offset: int


def _partition_names(names: tuple[str, ...], count: int):
    if count > len(names):
        raise ManifestValidationError(
            "feature interface cannot have more topics than feature names"
        )
    quotient, remainder = divmod(len(names), count)
    chunks = []
    start = 0
    for index in range(count):
        size = quotient + (1 if index < remainder else 0)
        chunks.append((names[start : start + size], start))
        start += size
    return chunks


class FakeRobot(Node):
    def __init__(self) -> None:
        super().__init__("fake_robot")
        self.declare_parameter("manifest_path", "")
        self.declare_parameter("drop_source", "")
        self.declare_parameter("drop_after_s", 5.0)

        manifest_path = self.get_parameter("manifest_path").value
        if not manifest_path:
            raise ManifestValidationError("ROS parameter 'manifest_path' must not be empty")
        self.manifest = load_manifest(manifest_path)
        self.drop_source = str(self.get_parameter("drop_source").value)
        self.drop_after_s = float(self.get_parameter("drop_after_s").value)
        if self.drop_after_s < 0.0:
            raise ManifestValidationError("drop_after_s must be non-negative")
        if self.drop_source and self.drop_source not in self.manifest.interfaces.source_keys:
            raise ManifestValidationError(
                f"drop_source must be a manifest source key, got {self.drop_source!r}"
            )

        self._camera_publishers = {
            camera.key: (
                camera,
                self.create_publisher(
                    CompressedImage, camera.topic, qos_profile_sensor_data
                ),
            )
            for camera in self.manifest.interfaces.cameras
        }
        self._state_publishers = []
        for interface in self.manifest.interfaces.state:
            for topic, (names, offset) in zip(
                interface.topics,
                _partition_names(interface.feature_names, len(interface.topics)),
                strict=True,
            ):
                self._state_publishers.append(
                    FeaturePublisher(
                        interface.key,
                        self.create_publisher(
                            JointState, topic, qos_profile_sensor_data
                        ),
                        names,
                        offset,
                    )
                )

        self._action_publishers = []
        for interface in self.manifest.interfaces.action:
            for topic, (names, offset) in zip(
                interface.topics,
                _partition_names(interface.feature_names, len(interface.topics)),
                strict=True,
            ):
                self._action_publishers.append(
                    FeaturePublisher(
                        interface.key,
                        self.create_publisher(
                            JointTrajectory, topic, qos_profile_sensor_data
                        ),
                        names,
                        offset,
                    )
                )

        self._started_monotonic = time.monotonic()
        self._drop_logged = False
        self._frame_index = 0
        self._timer = self.create_timer(
            1.0 / self.manifest.recording.fps, self._publish_tick
        )
        self.get_logger().info(
            "Synthetic publishing started: cameras=%d fps=%d drop_source=%s drop_after_s=%.1f"
            % (
                len(self._camera_publishers),
                self.manifest.recording.fps,
                self.drop_source or "none",
                self.drop_after_s,
            )
        )

    def _is_dropped(self, source_key: str, elapsed: float) -> bool:
        dropped = bool(
            self.drop_source == source_key and elapsed >= self.drop_after_s
        )
        if dropped and not self._drop_logged:
            self.get_logger().warning(f"Dropping source: {self.drop_source}")
            self._drop_logged = True
        return dropped

    @staticmethod
    def _positions(elapsed: float, count: int, offset: int):
        return [
            float(100.0 * np.sin(elapsed * 1.2 + (offset + index) * 0.35))
            for index in range(count)
        ]

    def _camera_image(self, camera, camera_index: int, elapsed: float):
        image = np.empty((camera.height, camera.width, 3), dtype=np.uint8)
        image[:] = (
            (40 + camera_index * 37) % 256,
            (90 + camera_index * 53) % 256,
            (150 + camera_index * 29) % 256,
        )
        x = int((elapsed * 120.0 + camera_index * 47) % camera.width)
        y = int(camera.height * (0.3 + 0.2 * np.sin(elapsed + camera_index)))
        cv2.circle(image, (x, y), 35, (255, 255, 255), -1)
        cv2.line(image, (x, 0), (x, camera.height - 1), (0, 0, 0), 3)
        cv2.putText(
            image,
            f"{camera.key} frame={self._frame_index}",
            (20, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        return image

    def _publish_tick(self) -> None:
        elapsed = time.monotonic() - self._started_monotonic
        stamp = self.get_clock().now().to_msg()

        for camera_index, (source_key, item) in enumerate(
            self._camera_publishers.items()
        ):
            if self._is_dropped(source_key, elapsed):
                continue
            camera, publisher = item
            image = self._camera_image(camera, camera_index, elapsed)
            encoded, jpeg = cv2.imencode(
                ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80]
            )
            if not encoded:
                self.get_logger().error(f"JPEG encoding failed for {source_key}")
                continue
            message = CompressedImage()
            message.header.stamp = stamp
            message.format = "jpeg"
            message.data = jpeg.tobytes()
            publisher.publish(message)

        for item in self._state_publishers:
            if self._is_dropped(item.source_key, elapsed):
                continue
            message = JointState()
            message.header.stamp = stamp
            message.name = list(item.names)
            message.position = self._positions(
                elapsed, len(item.names), item.phase_offset
            )
            item.publisher.publish(message)

        for item in self._action_publishers:
            if self._is_dropped(item.source_key, elapsed):
                continue
            message = JointTrajectory()
            message.header.stamp = stamp
            message.joint_names = list(item.names)
            point = JointTrajectoryPoint()
            point.positions = self._positions(
                elapsed, len(item.names), item.phase_offset
            )
            message.points = [point]
            item.publisher.publish(message)

        self._frame_index += 1


def main(args=None) -> None:
    rclpy.init(args=args)
    node = None
    try:
        node = FakeRobot()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except ManifestValidationError as exc:
        if node is not None:
            node.get_logger().fatal(str(exc))
        else:
            rclpy.logging.get_logger("fake_robot").fatal(str(exc))
        raise
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
