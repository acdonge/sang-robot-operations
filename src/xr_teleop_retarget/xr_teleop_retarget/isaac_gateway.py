"""Publish the canonical ROS bimanual target to an Isaac-safe latest-frame link."""

from __future__ import annotations

import math

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from xr_teleop_interfaces.msg import XRBimanualTarget


PROTOCOL = "xr_bimanual_target_v1"


def target_document(message: XRBimanualTarget) -> dict:
    if message.protocol_version != XRBimanualTarget.PROTOCOL_VERSION:
        raise ValueError(
            f"unsupported target protocol version {message.protocol_version}"
        )
    return {
        "protocol": PROTOCOL,
        "sequence": int(message.sequence),
        "ros_stamp_ns": (
            int(message.header.stamp.sec) * 1_000_000_000
            + int(message.header.stamp.nanosec)
        ),
        "bimanual_enabled": bool(message.bimanual_enabled),
        "left": _hand_document(
            message.left_target,
            message.left_gripper,
            message.left_enabled,
            message.left_inhibit_reason,
        ),
        "right": _hand_document(
            message.right_target,
            message.right_gripper,
            message.right_enabled,
            message.right_inhibit_reason,
        ),
    }


def _hand_document(target, gripper: float, enabled: bool, reason: str) -> dict:
    pose = target.pose
    orientation = (
        float(pose.orientation.x),
        float(pose.orientation.y),
        float(pose.orientation.z),
        float(pose.orientation.w),
    )
    orientation_norm = math.sqrt(sum(value * value for value in orientation))
    target_value = None
    if orientation_norm > 1e-9:
        target_value = {
            "position": [
                float(pose.position.x),
                float(pose.position.y),
                float(pose.position.z),
            ],
            "orientation_xyzw": list(orientation),
        }
    return {
        "frame_id": target.header.frame_id,
        "target": target_value,
        "gripper": float(gripper),
        "enabled": bool(enabled),
        "inhibit_reason": reason,
    }


class XRIsaacGateway(Node):
    def __init__(self) -> None:
        super().__init__("xr_isaac_gateway")
        self.declare_parameter(
            "source_topic", "/xr/retarget/bimanual_target"
        )
        self.declare_parameter("bind_endpoint", "tcp://0.0.0.0:5558")

        source_topic = str(self.get_parameter("source_topic").value)
        endpoint = str(self.get_parameter("bind_endpoint").value)
        if not source_topic:
            raise ValueError("source_topic must not be empty")
        if not endpoint.startswith("tcp://"):
            raise ValueError("bind_endpoint must use tcp://")

        try:
            import zmq
        except ImportError as exc:
            raise RuntimeError(
                "pyzmq is required for the XR-to-Isaac gateway"
            ) from exc

        self._zmq = zmq
        self._context = zmq.Context()
        self._publisher = self._context.socket(zmq.PUB)
        self._publisher.setsockopt(zmq.LINGER, 0)
        self._publisher.setsockopt(zmq.SNDHWM, 1)
        self._publisher.bind(endpoint)
        self._sent = 0
        self._dropped = 0
        self.create_subscription(
            XRBimanualTarget,
            source_topic,
            self._on_target,
            qos_profile_sensor_data,
        )
        self.get_logger().info(
            f"Isaac bimanual target gateway bound to {endpoint}; no robot output"
        )

    def _on_target(self, message: XRBimanualTarget) -> None:
        try:
            document = target_document(message)
            self._publisher.send_json(document, flags=self._zmq.NOBLOCK)
            self._sent += 1
        except self._zmq.Again:
            self._dropped += 1
        except ValueError as exc:
            self.get_logger().warning(f"Ignoring invalid bimanual target: {exc}")

    def destroy_node(self) -> None:
        self._publisher.close(linger=0)
        self._context.term()
        super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = None
    try:
        node = XRIsaacGateway()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
