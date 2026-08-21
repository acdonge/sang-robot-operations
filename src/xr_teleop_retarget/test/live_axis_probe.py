#!/usr/bin/env python3
"""Measure a live deadman-enabled XR retarget movement sequence."""

from __future__ import annotations

import json
import time

from diagnostic_msgs.msg import DiagnosticArray
from geometry_msgs.msg import PoseStamped
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from std_msgs.msg import Bool, Float32
from visualization_msgs.msg import Marker, MarkerArray
from xr_teleop_retarget.retarget import RetargetConfig


NEUTRAL = RetargetConfig().neutral_position
MIN_POSITIVE_DELTA_M = 0.015
WAIT_TIMEOUT_S = 300.0
MIN_CAPTURE_S = 5.0
RELEASE_SETTLE_S = 0.75


class LiveAxisProbe(Node):
    def __init__(self) -> None:
        super().__init__("xr_retarget_live_axis_probe")
        self.candidate_positions: list[tuple[float, float, float]] = []
        self.preview_positions: list[tuple[float, float, float]] = []
        self.enabled = False
        self.first_candidate_time: float | None = None
        self.released_since: float | None = None
        self.diagnostic_messages: list[str] = []
        self.max_gripper = 0.0

        self.create_subscription(
            PoseStamped,
            "/xr/retarget/preview_pose",
            self._on_preview_pose,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            Bool,
            "/xr/retarget/enabled",
            self._on_enabled,
            10,
        )
        self.create_subscription(
            Float32,
            "/xr/retarget/preview_gripper",
            self._on_gripper,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            DiagnosticArray,
            "/xr/retarget/diagnostics",
            self._on_diagnostics,
            10,
        )
        self.create_subscription(
            MarkerArray,
            "/xr/retarget/markers",
            self._on_markers,
            10,
        )

    def _on_preview_pose(self, message: PoseStamped) -> None:
        position = message.pose.position
        self.preview_positions.append((position.x, position.y, position.z))

    def _on_enabled(self, message: Bool) -> None:
        self.enabled = message.data
        if self.enabled:
            self.released_since = None

    def _on_gripper(self, message: Float32) -> None:
        self.max_gripper = max(self.max_gripper, float(message.data))

    def _on_diagnostics(self, message: DiagnosticArray) -> None:
        for status in message.status:
            if status.name == "xr_teleop_retarget/preview_gate":
                self.diagnostic_messages.append(status.message)

    def _on_markers(self, message: MarkerArray) -> None:
        markers = {marker.ns: marker for marker in message.markers}
        target = markers.get("target")
        status = markers.get("status")
        if target is None or status is None:
            return

        if status.text == "INHIBITED: deadman_released":
            if (
                self.first_candidate_time is not None
                and self.released_since is None
            ):
                self.released_since = time.monotonic()
            return

        deadman_passed = status.text in {
            "PREVIEW ENABLED",
            "INHIBITED: outside_workspace",
            "INHIBITED: rotation_limit",
        }
        if not deadman_passed or target.action == Marker.DELETE:
            return

        self.released_since = None
        if self.first_candidate_time is None:
            self.first_candidate_time = time.monotonic()
        position = target.pose.position
        self.candidate_positions.append((position.x, position.y, position.z))


def main() -> None:
    rclpy.init()
    probe = LiveAxisProbe()
    deadline = time.monotonic() + WAIT_TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            rclpy.spin_once(probe, timeout_sec=0.05)
            now = time.monotonic()
            if probe.first_candidate_time is None or probe.released_since is None:
                continue
            if now - probe.first_candidate_time < MIN_CAPTURE_S:
                continue
            if now - probe.released_since >= RELEASE_SETTLE_S:
                break

        if not probe.candidate_positions:
            raise RuntimeError(
                "no deadman-enabled target candidates received before timeout"
            )

        mins = tuple(
            min(point[index] for point in probe.candidate_positions)
            for index in range(3)
        )
        maxs = tuple(
            max(point[index] for point in probe.candidate_positions)
            for index in range(3)
        )
        positive_deltas = tuple(
            maxs[index] - NEUTRAL[index] for index in range(3)
        )
        errors = [
            f"{axis} positive delta was only {delta:.3f} m"
            for axis, delta in zip(
                ("x/forward", "y/left", "z/up"), positive_deltas
            )
            if delta < MIN_POSITIVE_DELTA_M
        ]

        publishers = probe.get_publisher_names_and_types_by_node(
            "xr_retarget_preview", "/"
        )
        dangerous = sorted(
            name
            for name, _types in publishers
            if "joint_trajectory" in name
            or name.endswith("/cmd")
            or "/command" in name
            or "/motor" in name
            or "/servo" in name
        )
        if dangerous:
            errors.append(f"robot command publishers exposed: {dangerous}")
        if not probe.preview_positions:
            errors.append("safety gate never enabled preview output")

        print(
            json.dumps(
                {
                    "diagnostics": list(dict.fromkeys(probe.diagnostic_messages)),
                    "max_gripper": round(probe.max_gripper, 4),
                    "max_m": dict(
                        zip(("x", "y", "z"), (round(v, 4) for v in maxs))
                    ),
                    "min_m": dict(
                        zip(("x", "y", "z"), (round(v, 4) for v in mins))
                    ),
                    "positive_delta_m": dict(
                        zip(
                            ("forward", "left", "up"),
                            (round(v, 4) for v in positive_deltas),
                        )
                    ),
                    "candidate_samples": len(probe.candidate_positions),
                    "preview_samples": len(probe.preview_positions),
                    "robot_command_publishers": dangerous,
                },
                indent=2,
                sort_keys=True,
            )
        )
        if errors:
            raise RuntimeError("; ".join(errors))
        print("PASS: live forward/left/up retarget movement")
    finally:
        probe.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
