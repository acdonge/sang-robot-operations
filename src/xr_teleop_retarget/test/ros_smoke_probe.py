#!/usr/bin/env python3
"""Exercise calibration, preview output, safety gating, TF, and markers."""

from __future__ import annotations

import json
import os
import time

from diagnostic_msgs.msg import DiagnosticArray
from geometry_msgs.msg import PoseStamped
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from std_msgs.msg import Bool, Float32
from std_srvs.srv import Trigger
from tf2_msgs.msg import TFMessage
from visualization_msgs.msg import MarkerArray
from xr_teleop_interfaces.msg import XRBimanualTarget, XRFrame
import zmq


class RetargetSmokeProbe(Node):
    def __init__(self) -> None:
        super().__init__("xr_retarget_smoke_probe")
        self.frame_publisher = self.create_publisher(
            XRFrame, "/xr/raw/frame", qos_profile_sensor_data
        )
        self.calibrate_client = self.create_client(
            Trigger, "/xr/retarget/calibrate"
        )
        self.preview_poses: list[PoseStamped] = []
        self.gripper_values: list[float] = []
        self.enabled_values: list[bool] = []
        self.bimanual_targets: list[XRBimanualTarget] = []
        self.gateway_payloads: list[dict] = []
        self.diagnostic_messages: list[str] = []
        self.tf_children: set[str] = set()
        self.marker_count = 0
        self.create_subscription(
            PoseStamped,
            "/xr/retarget/preview_pose",
            self.preview_poses.append,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            Float32,
            "/xr/retarget/preview_gripper",
            lambda message: self.gripper_values.append(message.data),
            qos_profile_sensor_data,
        )
        self.create_subscription(
            Bool,
            "/xr/retarget/enabled",
            lambda message: self.enabled_values.append(message.data),
            10,
        )
        self.create_subscription(
            XRBimanualTarget,
            "/xr/retarget/bimanual_target",
            self.bimanual_targets.append,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            DiagnosticArray,
            "/xr/retarget/diagnostics",
            self._on_diagnostics,
            10,
        )
        self.create_subscription(
            TFMessage, "/tf", self._on_tf, qos_profile_sensor_data
        )
        self.create_subscription(
            MarkerArray,
            "/xr/retarget/markers",
            lambda message: setattr(self, "marker_count", len(message.markers)),
            10,
        )
        self.zmq_context = zmq.Context()
        self.zmq_subscriber = self.zmq_context.socket(zmq.SUB)
        self.zmq_subscriber.setsockopt(zmq.SUBSCRIBE, b"")
        self.zmq_subscriber.setsockopt(zmq.CONFLATE, 1)
        self.zmq_subscriber.connect(
            os.environ.get(
                "XR_ISAAC_ENDPOINT", "tcp://127.0.0.1:5558"
            )
        )

    def poll_gateway(self) -> None:
        while self.zmq_subscriber.poll(timeout=0):
            self.gateway_payloads.append(
                self.zmq_subscriber.recv_json(flags=zmq.NOBLOCK)
            )

    def close_gateway(self) -> None:
        self.zmq_subscriber.close(linger=0)
        self.zmq_context.term()

    def _on_diagnostics(self, message: DiagnosticArray) -> None:
        for status in message.status:
            if status.name == "xr_teleop_retarget/preview_gate":
                self.diagnostic_messages.append(status.message)

    def _on_tf(self, message: TFMessage) -> None:
        self.tf_children.update(
            transform.child_frame_id for transform in message.transforms
        )


def frame(
    sequence: int,
    *,
    right_z: float,
    right_squeeze: float,
    right_trigger: float,
    left_position=(0.1, 1.0, -0.4),
    left_squeeze: float | None = None,
    left_trigger: float = 0.25,
) -> XRFrame:
    message = XRFrame()
    message.header.frame_id = "xr_local"
    message.protocol_version = XRFrame.PROTOCOL_VERSION
    message.sequence = sequence
    message.source = "retarget_smoke"
    message.left.connected = True
    message.left.grip_pose_valid = True
    message.left.grip_pose_tracked = True
    (
        message.left.grip_pose.position.x,
        message.left.grip_pose.position.y,
        message.left.grip_pose.position.z,
    ) = left_position
    message.left.grip_pose.orientation.w = 1.0
    message.left.squeeze = (
        right_squeeze if left_squeeze is None else left_squeeze
    )
    message.left.trigger = left_trigger
    message.right.connected = True
    message.right.grip_pose_valid = True
    message.right.grip_pose_tracked = True
    message.right.grip_pose.position.x = 0.0
    message.right.grip_pose.position.y = 1.0
    message.right.grip_pose.position.z = right_z
    message.right.grip_pose.orientation.w = 1.0
    message.right.squeeze = right_squeeze
    message.right.trigger = right_trigger
    return message


def spin_and_publish(
    node: RetargetSmokeProbe, message: XRFrame, duration_s: float
) -> None:
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        node.frame_publisher.publish(message)
        rclpy.spin_once(node, timeout_sec=0.02)
        node.poll_gateway()
        time.sleep(0.01)


def main() -> None:
    rclpy.init()
    probe = RetargetSmokeProbe()
    errors: list[str] = []
    try:
        if not probe.calibrate_client.wait_for_service(timeout_sec=4.0):
            raise RuntimeError("calibration service did not become available")

        anchor = frame(
            1,
            right_z=-0.4,
            right_squeeze=0.0,
            right_trigger=0.0,
        )
        spin_and_publish(probe, anchor, 0.6)
        future = probe.calibrate_client.call_async(Trigger.Request())
        deadline = time.monotonic() + 3.0
        while not future.done() and time.monotonic() < deadline:
            probe.frame_publisher.publish(anchor)
            rclpy.spin_once(probe, timeout_sec=0.05)
        if not future.done() or not future.result().success:
            errors.append("calibration service failed")

        moved = frame(
            2,
            right_z=-0.45,
            right_squeeze=1.0,
            right_trigger=0.75,
            left_position=(0.05, 1.05, -0.4),
        )
        spin_and_publish(probe, moved, 1.0)

        if not probe.preview_poses:
            errors.append("no enabled preview pose was received")
        else:
            pose = probe.preview_poses[-1]
            if pose.header.frame_id != "soarm_base":
                errors.append(f"unexpected preview frame: {pose.header.frame_id}")
            if abs(pose.pose.position.x - 0.31823402) > 1e-4:
                errors.append(
                    f"forward mapping was {pose.pose.position.x}, expected 0.31823402"
                )
            if abs(pose.pose.position.y + 0.00017677) > 1e-4:
                errors.append("forward motion unexpectedly changed robot Y")
            if abs(pose.pose.position.z - 0.23437033) > 1e-4:
                errors.append("forward motion unexpectedly changed robot Z")
        if not probe.gripper_values or abs(probe.gripper_values[-1] - 0.75) > 1e-4:
            errors.append("trigger did not reach preview gripper output")
        if True not in probe.enabled_values:
            errors.append("preview gate never enabled")
        enabled_targets = [
            target
            for target in probe.bimanual_targets
            if target.bimanual_enabled
        ]
        if not enabled_targets:
            errors.append("no enabled bimanual ROS target was received")
        else:
            target = enabled_targets[-1]
            left_position = target.left_target.pose.position
            right_position = target.right_target.pose.position
            if target.left_target.header.frame_id != "left_soarm_base":
                errors.append("left target frame is not arm-local")
            if target.right_target.header.frame_id != "right_soarm_base":
                errors.append("right target frame is not arm-local")
            if (
                abs(left_position.x - 0.29323402) > 1e-4
                or abs(left_position.y - 0.02482323) > 1e-4
                or abs(left_position.z - 0.25937033) > 1e-4
            ):
                errors.append("left controller did not map independently")
            if (
                abs(right_position.x - 0.31823402) > 1e-4
                or abs(right_position.y + 0.00017677) > 1e-4
                or abs(right_position.z - 0.23437033) > 1e-4
            ):
                errors.append("right controller did not map independently")
            if abs(target.left_gripper - 0.25) > 1e-4:
                errors.append("left trigger did not reach bimanual target")
            if abs(target.right_gripper - 0.75) > 1e-4:
                errors.append("right trigger did not reach bimanual target")
        enabled_gateway = [
            payload
            for payload in probe.gateway_payloads
            if payload.get("bimanual_enabled")
        ]
        if not enabled_gateway:
            errors.append("no enabled Isaac gateway payload was received")
        else:
            payload = enabled_gateway[-1]
            if payload.get("protocol") != "xr_bimanual_target_v1":
                errors.append("unexpected Isaac gateway protocol")
            if payload["left"]["frame_id"] != "left_soarm_base":
                errors.append("gateway lost the left target frame")
            if payload["right"]["frame_id"] != "right_soarm_base":
                errors.append("gateway lost the right target frame")
        if "enabled" not in probe.diagnostic_messages:
            errors.append("diagnostics never reported enabled")
        expected_tf = {"left_soarm_preview_target", "soarm_preview_target"}
        if not expected_tf.issubset(probe.tf_children):
            errors.append("both preview target TFs were not published")
        if probe.marker_count != 4:
            errors.append(f"expected 4 visualization markers, got {probe.marker_count}")

        released = frame(
            3,
            right_z=-0.45,
            right_squeeze=0.0,
            right_trigger=0.75,
            left_position=(0.05, 1.05, -0.4),
            left_squeeze=1.0,
        )
        spin_and_publish(probe, released, 0.6)
        if not probe.enabled_values or probe.enabled_values[-1]:
            errors.append("releasing deadman did not inhibit preview")
        if not any(
            "right:deadman_released" in message
            for message in probe.diagnostic_messages
        ):
            errors.append("diagnostics did not report released deadman")
        if not probe.gateway_payloads or probe.gateway_payloads[-1].get(
            "bimanual_enabled", True
        ):
            errors.append("gateway did not inhibit after one deadman released")

        publishers = probe.get_publisher_names_and_types_by_node(
            "xr_retarget_preview", "/"
        )
        publisher_names = {name for name, _types in publishers}
        dangerous = sorted(
            name
            for name in publisher_names
            if "joint_trajectory" in name
            or name.endswith("/cmd")
            or "/command" in name
            or "/motor" in name
            or "/servo" in name
        )
        if dangerous:
            errors.append(f"robot command publishers exposed: {dangerous}")
        if errors:
            raise RuntimeError("; ".join(errors))

        print(
            json.dumps(
                {
                    "diagnostics": list(dict.fromkeys(probe.diagnostic_messages)),
                    "gateway_protocol": enabled_gateway[-1]["protocol"],
                    "gripper": probe.gripper_values[-1],
                    "left_target": enabled_gateway[-1]["left"]["target"][
                        "position"
                    ],
                    "markers": probe.marker_count,
                    "preview_x": probe.preview_poses[-1].pose.position.x,
                    "robot_command_publishers": dangerous,
                    "tf_children": sorted(probe.tf_children),
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        probe.close_gateway()
        probe.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
