#!/usr/bin/env python3
"""Publish deterministic SO-101 observation topics for integration smoke tests."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CompressedImage, JointState
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

from so101_readonly_adapter.config import load_hardware_config


class FakeSourcesNode(Node):
    def __init__(
        self,
        config_path: Path,
        *,
        publish_hz: float,
        drop_key: str | None,
        drop_file: Path | None,
        drop_start_s: float,
        drop_duration_s: float,
        duration_s: float,
    ) -> None:
        super().__init__("fake_so101_sources")
        hardware = load_hardware_config(config_path)
        self._hardware = hardware
        self._drop_key = drop_key
        self._drop_file = drop_file
        self._drop_start_s = drop_start_s
        self._drop_duration_s = drop_duration_s
        self._duration_s = duration_s
        self._started = time.monotonic()
        self._arm_publishers = {
            arm.key: self.create_publisher(
                JointState if arm.role == "follower" else JointTrajectory,
                arm.topic,
                qos_profile_sensor_data,
            )
            for arm in hardware.arms
        }
        self._camera_publishers = {
            camera.key: self.create_publisher(
                CompressedImage,
                camera.topic,
                qos_profile_sensor_data,
            )
            for camera in hardware.cameras
        }
        self._timer = self.create_timer(1.0 / publish_hz, self._tick)

    def _dropped(self, key: str, elapsed: float) -> bool:
        if key != self._drop_key:
            return False
        return (
            self._drop_file is not None
            and self._drop_file.exists()
        ) or (
            self._drop_start_s <= elapsed
            < self._drop_start_s + self._drop_duration_s
        )

    def _tick(self) -> None:
        elapsed = time.monotonic() - self._started
        if self._duration_s and elapsed >= self._duration_s:
            rclpy.shutdown()
            return
        stamp = self.get_clock().now().to_msg()
        positions = [0.0, 0.0, 0.0, 0.0, 0.0, 50.0]
        for arm in self._hardware.arms:
            if self._dropped(arm.key, elapsed):
                continue
            names = [
                f"{arm.side}_{name}"
                for name in (
                    "shoulder_pan",
                    "shoulder_lift",
                    "elbow_flex",
                    "wrist_flex",
                    "wrist_roll",
                    "gripper",
                )
            ]
            if arm.role == "follower":
                message = JointState()
                message.header.stamp = stamp
                message.name = names
                message.position = positions
            else:
                message = JointTrajectory()
                message.header.stamp = stamp
                message.joint_names = names
                point = JointTrajectoryPoint()
                point.positions = positions
                message.points = [point]
            self._arm_publishers[arm.key].publish(message)
        for camera in self._hardware.cameras:
            if self._dropped(camera.key, elapsed):
                continue
            message = CompressedImage()
            message.header.stamp = stamp
            message.header.frame_id = camera.key
            message.format = "jpeg"
            message.data = b"\xff\xd8\xff\xd9"
            self._camera_publishers[camera.key].publish(message)


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish fake SO-101 source topics")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--publish-hz", type=float, default=10.0)
    parser.add_argument("--drop-key")
    parser.add_argument("--drop-file", type=Path)
    parser.add_argument("--drop-start-s", type=float, default=2.0)
    parser.add_argument("--drop-duration-s", type=float, default=3.0)
    parser.add_argument("--duration-s", type=float, default=0.0)
    options = parser.parse_args(args)
    if options.publish_hz <= 0.0:
        parser.error("publish-hz must be positive")
    if min(options.drop_start_s, options.drop_duration_s, options.duration_s) < 0.0:
        parser.error("schedule values must not be negative")
    return options


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    rclpy.init(args=sys.argv if args is None else [sys.argv[0], *args])
    node = FakeSourcesNode(
        options.config,
        publish_hz=options.publish_hz,
        drop_key=options.drop_key,
        drop_file=options.drop_file,
        drop_start_s=options.drop_start_s,
        drop_duration_s=options.drop_duration_s,
        duration_s=options.duration_s,
    )
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
