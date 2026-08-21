"""Publish the canonical OMY ROS target over a latest-frame ZMQ link."""

from __future__ import annotations

import math

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from xr_teleop_interfaces.msg import XROmyTarget


PROTOCOL = "xr_omy_target_v1"


def target_document(message: XROmyTarget) -> dict:
    if message.protocol_version != XROmyTarget.PROTOCOL_VERSION:
        raise ValueError(
            f"unsupported target protocol version {message.protocol_version}"
        )
    if not message.target_link:
        raise ValueError("target_link must not be empty")
    gripper = float(message.gripper)
    if not math.isfinite(gripper) or not 0.0 <= gripper <= 1.0:
        raise ValueError("gripper must be finite and between 0 and 1")

    pose = message.target.pose
    position = (
        float(pose.position.x),
        float(pose.position.y),
        float(pose.position.z),
    )
    orientation = (
        float(pose.orientation.x),
        float(pose.orientation.y),
        float(pose.orientation.z),
        float(pose.orientation.w),
    )
    if not all(math.isfinite(value) for value in position + orientation):
        raise ValueError("target pose must contain only finite values")
    orientation_norm = math.sqrt(sum(value * value for value in orientation))
    target = None
    if orientation_norm > 1e-9:
        if not message.target.header.frame_id:
            raise ValueError("target frame_id must not be empty")
        if abs(orientation_norm - 1.0) > 1e-3:
            raise ValueError("target orientation must be a unit quaternion")
        target = {
            "frame_id": message.target.header.frame_id,
            "position": list(position),
            "orientation_xyzw": list(orientation),
        }
    if message.enabled and target is None:
        raise ValueError("an enabled target must contain a valid orientation")

    return {
        "protocol": PROTOCOL,
        "sequence": int(message.sequence),
        "ros_stamp_ns": (
            int(message.header.stamp.sec) * 1_000_000_000
            + int(message.header.stamp.nanosec)
        ),
        "target_link": message.target_link,
        "target": target,
        "gripper": gripper,
        "enabled": bool(message.enabled),
        "inhibit_reason": message.inhibit_reason,
        "clutch_epoch": int(message.clutch_epoch),
    }


class XROmyIsaacGateway(Node):
    def __init__(self) -> None:
        super().__init__("xr_omy_isaac_gateway")
        self.declare_parameter("source_topic", "/xr/omy/target")
        self.declare_parameter("bind_endpoint", "tcp://0.0.0.0:5559")

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
                "pyzmq is required for the OMY XR-to-Isaac gateway"
            ) from exc

        self._zmq = zmq
        self._context = zmq.Context()
        self._publisher = self._context.socket(zmq.PUB)
        self._publisher.setsockopt(zmq.LINGER, 0)
        self._publisher.setsockopt(zmq.SNDHWM, 1)
        self._publisher.setsockopt(zmq.CONFLATE, 1)
        self._publisher.bind(endpoint)
        self.create_subscription(
            XROmyTarget,
            source_topic,
            self._on_target,
            qos_profile_sensor_data,
        )
        self.get_logger().info(
            f"OMY Isaac target gateway bound to {endpoint}; no robot output"
        )

    def _on_target(self, message: XROmyTarget) -> None:
        try:
            self._publisher.send_json(
                target_document(message), flags=self._zmq.NOBLOCK
            )
        except self._zmq.Again:
            pass
        except ValueError as exc:
            self.get_logger().warning(f"Ignoring invalid OMY target: {exc}")

    def destroy_node(self) -> None:
        self._publisher.close(linger=0)
        self._context.term()
        super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = None
    try:
        node = XROmyIsaacGateway()
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
