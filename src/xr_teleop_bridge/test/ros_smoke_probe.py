#!/usr/bin/env python3
"""Assert the XR bridge's ROS outputs against a live mock TCP stream."""

from __future__ import annotations

import argparse
import json
import threading
import time

from diagnostic_msgs.msg import DiagnosticArray
from geometry_msgs.msg import PoseStamped
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from tf2_msgs.msg import TFMessage
from xr_teleop_interfaces.msg import XRFrame

from xr_teleop_bridge.mock_sender import send_frames


class XRSmokeProbe(Node):
    def __init__(self) -> None:
        super().__init__("xr_teleop_smoke_probe")
        self.raw_sequences: list[int] = []
        self.raw_sources: set[str] = set()
        self.protocol_versions: set[int] = set()
        self.left_pose_count = 0
        self.tf_children: set[str] = set()
        self.diagnostic_messages: list[str] = []

        self.create_subscription(
            XRFrame, "/xr/raw/frame", self._on_raw, qos_profile_sensor_data
        )
        self.create_subscription(
            PoseStamped,
            "/xr/left/grip_pose",
            self._on_left_pose,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            TFMessage, "/tf", self._on_tf, qos_profile_sensor_data
        )
        self.create_subscription(
            DiagnosticArray, "/xr/diagnostics", self._on_diagnostics, 10
        )

    def _on_raw(self, message: XRFrame) -> None:
        self.raw_sequences.append(message.sequence)
        self.raw_sources.add(message.source)
        self.protocol_versions.add(message.protocol_version)

    def _on_left_pose(self, _message: PoseStamped) -> None:
        self.left_pose_count += 1

    def _on_tf(self, message: TFMessage) -> None:
        self.tf_children.update(
            transform.child_frame_id for transform in message.transforms
        )

    def _on_diagnostics(self, message: DiagnosticArray) -> None:
        for status in message.status:
            if status.name == "xr_teleop_bridge/tcp_input":
                self.diagnostic_messages.append(status.message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=12.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    sender_errors: list[str] = []

    def run_sender() -> None:
        time.sleep(0.5)
        try:
            send_frames(
                args.host,
                args.port,
                hz=60.0,
                frames=120,
                source="ros_smoke",
            )
        except Exception as exc:  # Report thread failures through the main process.
            sender_errors.append(str(exc))

    rclpy.init()
    probe = XRSmokeProbe()
    sender = threading.Thread(target=run_sender, name="xr-smoke-sender")
    sender.start()

    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            rclpy.spin_once(probe, timeout_sec=0.05)
            saw_fresh = "XR input fresh" in probe.diagnostic_messages
            saw_stale = "XR input stale" in probe.diagnostic_messages
            if not sender.is_alive() and saw_fresh and saw_stale:
                break
        sender.join(timeout=1.0)

        publisher_entries = probe.get_publisher_names_and_types_by_node(
            "xr_teleop_bridge", "/"
        )
        publisher_names = {name for name, _types in publisher_entries}
        expected_publishers = {
            "/tf",
            "/xr/diagnostics",
            "/xr/head/pose",
            "/xr/left/grip_pose",
            "/xr/raw/frame",
            "/xr/right/grip_pose",
        }
        dangerous_publishers = sorted(
            name
            for name in publisher_names
            if name.startswith("/bi_so")
            or "joint_trajectory" in name
            or name.endswith("/cmd")
            or "/command" in name
        )

        errors = []
        if sender.is_alive():
            errors.append("mock sender did not finish")
        if sender_errors:
            errors.append(f"mock sender failed: {sender_errors[-1]}")
        if not probe.raw_sequences:
            errors.append("no /xr/raw/frame message was received")
        if probe.raw_sources != {"ros_smoke"}:
            errors.append(f"unexpected raw sources: {sorted(probe.raw_sources)}")
        if probe.protocol_versions != {XRFrame.PROTOCOL_VERSION}:
            errors.append(
                f"unexpected protocol versions: {sorted(probe.protocol_versions)}"
            )
        if probe.left_pose_count == 0:
            errors.append("no /xr/left/grip_pose message was received")
        expected_children = {"xr_head", "xr_left_grip", "xr_right_grip"}
        if not expected_children.issubset(probe.tf_children):
            errors.append(
                "missing TF children: "
                + str(sorted(expected_children - probe.tf_children))
            )
        if "XR input fresh" not in probe.diagnostic_messages:
            errors.append("diagnostics never reported fresh XR input")
        if "XR input stale" not in probe.diagnostic_messages:
            errors.append("diagnostics never reported stale XR input")
        if not expected_publishers.issubset(publisher_names):
            errors.append(
                "missing bridge publishers: "
                + str(sorted(expected_publishers - publisher_names))
            )
        if dangerous_publishers:
            errors.append(
                f"bridge exposed robot command publishers: {dangerous_publishers}"
            )
        if errors:
            raise RuntimeError("; ".join(errors))

        summary = {
            "diagnostics": list(dict.fromkeys(probe.diagnostic_messages)),
            "last_sequence": probe.raw_sequences[-1],
            "left_pose_messages": probe.left_pose_count,
            "raw_messages": len(probe.raw_sequences),
            "robot_command_publishers": dangerous_publishers,
            "tf_children": sorted(probe.tf_children),
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
    finally:
        probe.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
