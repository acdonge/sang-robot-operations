#!/usr/bin/env python3
"""Exercise OMY calibration, clutching, safety gates, TF, and ZMQ."""

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
from xr_teleop_interfaces.msg import XRFrame, XROmyTarget
import zmq


NEUTRAL_POSITION = (0.19080657, -0.11299910, 0.39004890)


class OmySmokeProbe(Node):
    def __init__(self) -> None:
        super().__init__("xr_omy_smoke_probe")
        self.frame_publisher = self.create_publisher(
            XRFrame, "/xr/raw/frame", qos_profile_sensor_data
        )
        self.calibrate_client = self.create_client(
            Trigger, "/xr/omy/calibrate"
        )
        self.targets: list[XROmyTarget] = []
        self.preview_poses: list[PoseStamped] = []
        self.gripper_values: list[float] = []
        self.enabled_values: list[bool] = []
        self.diagnostic_messages: list[str] = []
        self.tf_children: set[str] = set()
        self.gateway_payloads: list[dict] = []
        self.create_subscription(
            XROmyTarget,
            "/xr/omy/target",
            self.targets.append,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            PoseStamped,
            "/xr/omy/preview_pose",
            self.preview_poses.append,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            Float32,
            "/xr/omy/preview_gripper",
            lambda message: self.gripper_values.append(message.data),
            qos_profile_sensor_data,
        )
        self.create_subscription(
            Bool,
            "/xr/omy/enabled",
            lambda message: self.enabled_values.append(message.data),
            10,
        )
        self.create_subscription(
            DiagnosticArray, "/xr/omy/diagnostics", self._on_diagnostics, 10
        )
        self.create_subscription(
            TFMessage, "/tf", self._on_tf, qos_profile_sensor_data
        )
        self.zmq_context = zmq.Context()
        self.zmq_subscriber = self.zmq_context.socket(zmq.SUB)
        self.zmq_subscriber.setsockopt(zmq.SUBSCRIBE, b"")
        self.zmq_subscriber.setsockopt(zmq.CONFLATE, 1)
        self.zmq_subscriber.connect(
            os.environ.get(
                "XR_OMY_ISAAC_ENDPOINT", "tcp://127.0.0.1:15559"
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
            if status.name == "xr_omy_retarget/preview_gate":
                self.diagnostic_messages.append(status.message)

    def _on_tf(self, message: TFMessage) -> None:
        self.tf_children.update(
            transform.child_frame_id for transform in message.transforms
        )


def frame(
    sequence: int,
    *,
    position=(0.0, 1.0, -0.4),
    squeeze=0.0,
    trigger=0.0,
    tracked=True,
) -> XRFrame:
    message = XRFrame()
    message.header.frame_id = "xr_local"
    message.protocol_version = XRFrame.PROTOCOL_VERSION
    message.sequence = sequence
    message.source = "omy_smoke"
    message.head_pose_valid = True
    message.head_pose_tracked = True
    message.head_pose.orientation.w = 1.0
    message.right.connected = True
    message.right.grip_pose_valid = True
    message.right.grip_pose_tracked = tracked
    (
        message.right.grip_pose.position.x,
        message.right.grip_pose.position.y,
        message.right.grip_pose.position.z,
    ) = position
    message.right.grip_pose.orientation.w = 1.0
    message.right.squeeze = squeeze
    message.right.trigger = trigger
    return message


def spin_and_publish(
    node: OmySmokeProbe, message: XRFrame, duration_s: float
) -> None:
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        node.frame_publisher.publish(message)
        rclpy.spin_once(node, timeout_sec=0.02)
        node.poll_gateway()
        time.sleep(0.01)


def spin_only(node: OmySmokeProbe, duration_s: float) -> None:
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.03)
        node.poll_gateway()


def enabled_target(node: OmySmokeProbe, sequence: int) -> XROmyTarget | None:
    matches = [
        target
        for target in node.targets
        if target.sequence == sequence and target.enabled
    ]
    return matches[-1] if matches else None


def position_tuple(message: XROmyTarget) -> tuple[float, float, float]:
    position = message.target.pose.position
    return (position.x, position.y, position.z)


def close(left, right, tolerance=1e-4) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(left, right))


def main() -> None:
    rclpy.init()
    probe = OmySmokeProbe()
    errors: list[str] = []
    try:
        if not probe.calibrate_client.wait_for_service(timeout_sec=4.0):
            raise RuntimeError("calibration service did not become available")

        anchor = frame(1)
        spin_and_publish(probe, anchor, 0.6)
        future = probe.calibrate_client.call_async(Trigger.Request())
        deadline = time.monotonic() + 3.0
        while not future.done() and time.monotonic() < deadline:
            probe.frame_publisher.publish(anchor)
            rclpy.spin_once(probe, timeout_sec=0.05)
        if not future.done() or not future.result().success:
            errors.append("calibration service failed")

        first_grasp = frame(
            2, position=(0.2, 1.2, -0.5), squeeze=1.0, trigger=0.25
        )
        spin_and_publish(probe, first_grasp, 0.6)
        grasp_target = enabled_target(probe, 2)
        if grasp_target is None:
            errors.append("first clutch grasp did not enable a target")
        elif not close(position_tuple(grasp_target), NEUTRAL_POSITION):
            errors.append("first clutch grasp jumped away from neutral")
        if grasp_target is not None and grasp_target.clutch_epoch != 1:
            errors.append("first squeeze did not report a clutch reanchor")

        moved = frame(
            3, position=(0.2, 1.2, -0.6), squeeze=1.0, trigger=0.75
        )
        spin_and_publish(probe, moved, 0.8)
        moved_target = enabled_target(probe, 3)
        expected_moved = (
            NEUTRAL_POSITION[0] + 0.05,
            NEUTRAL_POSITION[1],
            NEUTRAL_POSITION[2],
        )
        if moved_target is None:
            errors.append("controller motion did not produce an enabled target")
        elif not close(position_tuple(moved_target), expected_moved):
            errors.append(
                f"forward mapping was {position_tuple(moved_target)}, "
                f"expected {expected_moved}"
            )
        if moved_target is not None:
            if moved_target.target.header.frame_id != "link0":
                errors.append("target base frame is not link0")
            if moved_target.target_link != "end_effector_link":
                errors.append("target TCP is not end_effector_link")
        if not probe.gripper_values or abs(probe.gripper_values[-1] - 0.75) > 1e-4:
            errors.append("trigger did not reach normalized gripper output")
        if "omy_xr_target" not in probe.tf_children:
            errors.append("OMY target TF was not published")

        enabled_gateway = [
            payload
            for payload in probe.gateway_payloads
            if payload.get("sequence") == 3 and payload.get("enabled")
        ]
        if not enabled_gateway:
            errors.append("Isaac gateway did not emit the enabled OMY target")
        else:
            payload = enabled_gateway[-1]
            if payload.get("protocol") != "xr_omy_target_v1":
                errors.append("Isaac gateway protocol is unexpected")
            if payload.get("target_link") != "end_effector_link":
                errors.append("Isaac gateway lost the OMY TCP link")
            if payload.get("target", {}).get("frame_id") != "link0":
                errors.append("Isaac gateway lost the OMY base frame")

        released = frame(
            4, position=(-0.3, 1.4, -0.1), squeeze=0.0, trigger=0.75
        )
        spin_and_publish(probe, released, 0.5)
        if not probe.targets or probe.targets[-1].enabled:
            errors.append("releasing squeeze did not inhibit output")
        elif probe.targets[-1].inhibit_reason != "deadman_released":
            errors.append("released squeeze reported the wrong inhibit reason")

        regrasped = frame(
            5, position=(-0.3, 1.4, -0.1), squeeze=1.0, trigger=0.75
        )
        spin_and_publish(probe, regrasped, 0.6)
        regrasp_target = enabled_target(probe, 5)
        if regrasp_target is None:
            errors.append("regrasp did not re-enable output")
        elif not close(position_tuple(regrasp_target), expected_moved):
            errors.append("release/reposition/regrasp caused a target jump")
        if regrasp_target is not None and regrasp_target.clutch_epoch != 2:
            errors.append("regrasp did not report a clutch reanchor")

        spin_only(probe, 0.45)
        if not probe.targets or probe.targets[-1].enabled:
            errors.append("stale input did not inhibit output")
        elif probe.targets[-1].inhibit_reason != "input_stale":
            errors.append("stale input reported the wrong inhibit reason")
        if not probe.gateway_payloads or probe.gateway_payloads[-1].get(
            "enabled", True
        ):
            errors.append("Isaac gateway did not propagate stale inhibition")

        invalid_tracking = frame(6, squeeze=1.0, tracked=False)
        spin_and_publish(probe, invalid_tracking, 0.4)
        if not probe.targets or probe.targets[-1].inhibit_reason != "tracking_invalid":
            errors.append("invalid tracking was not inhibited")

        if True not in probe.enabled_values:
            errors.append("enabled status was never published")
        if "enabled" not in probe.diagnostic_messages:
            errors.append("diagnostics never reported enabled")
        if not probe.preview_poses:
            errors.append("no enabled preview pose was published")

        publishers = probe.get_publisher_names_and_types_by_node(
            "xr_omy_retarget", "/"
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
                    "status": "PASS",
                    "enabled_targets": sum(
                        1 for target in probe.targets if target.enabled
                    ),
                    "gateway_frames": len(probe.gateway_payloads),
                    "clutch_continuity": True,
                    "stale_gate": True,
                    "robot_command_publishers": [],
                },
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
