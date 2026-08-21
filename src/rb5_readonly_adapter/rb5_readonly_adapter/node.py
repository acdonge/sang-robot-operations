"""Publish RB5 measured joint state from the read-only vendor data channel."""

from __future__ import annotations

import argparse
import math
import sys
import time
from typing import Callable

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.utilities import remove_ros_args
from sensor_msgs.msg import JointState

from rb5_readonly_adapter.client import DEFAULT_DATA_PORT, RB5DataClient


JOINT_NAMES = ("base", "shoulder", "elbow", "wrist1", "wrist2", "wrist3")
ClientFactory = Callable[[], RB5DataClient]


class RB5StateReaderNode(Node):
    def __init__(
        self,
        *,
        robot_ip: str,
        data_port: int,
        topic: str,
        publish_hz: float,
        request_timeout_s: float,
        reconnect_s: float,
        client_factory: ClientFactory | None = None,
    ) -> None:
        super().__init__("rb5_readonly_state_reader")
        self._request_timeout_s = request_timeout_s
        self._reconnect_s = reconnect_s
        self._next_connect_at = 0.0
        self._last_error: str | None = None
        factory = client_factory or (
            lambda: RB5DataClient(robot_ip, port=data_port)
        )
        self._client = factory()
        self._state_publisher = self.create_publisher(
            JointState,
            topic,
            qos_profile_sensor_data,
        )
        self._read_timer = self.create_timer(1.0 / publish_hz, self._tick)
        self.get_logger().info(
            f"reading RB5 data channel {robot_ip}:{data_port} without command APIs"
        )

    def _failure(self, exc: Exception, now: float) -> None:
        message = f"{type(exc).__name__}: {exc}"
        if message != self._last_error:
            self.get_logger().error(message)
        self._last_error = message
        self._next_connect_at = now + self._reconnect_s
        self._client.close()

    def _tick(self) -> None:
        now = time.monotonic()
        if now < self._next_connect_at:
            return
        try:
            positions = self._client.read_joint_positions(self._request_timeout_s)
        except Exception as exc:
            self._failure(exc, now)
            return
        if self._last_error is not None:
            self.get_logger().info("RB5 data channel recovered")
        self._last_error = None

        message = JointState()
        message.header.stamp = self.get_clock().now().to_msg()
        message.header.frame_id = "rb5_readonly_adapter"
        message.name = list(JOINT_NAMES)
        message.position = list(positions)
        self._state_publisher.publish(message)

    def destroy_node(self) -> None:
        self._read_timer.cancel()
        self._client.close()
        super().destroy_node()


def positive_finite(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise argparse.ArgumentTypeError("value must be positive and finite")
    return parsed


def valid_port(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only RB5 ROS 2 adapter")
    parser.add_argument("--robot-ip", default="10.0.2.7")
    parser.add_argument("--data-port", type=valid_port, default=DEFAULT_DATA_PORT)
    parser.add_argument("--topic", default="/rbpodo/joint_states")
    parser.add_argument("--publish-hz", type=positive_finite, default=30.0)
    parser.add_argument("--request-timeout-s", type=positive_finite, default=0.25)
    parser.add_argument("--reconnect-s", type=positive_finite, default=2.0)
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> None:
    raw_args = sys.argv if args is None else [sys.argv[0], *args]
    clean_args = remove_ros_args(args=raw_args)[1:]
    options = parse_args(clean_args)

    rclpy.init(args=raw_args)
    node = RB5StateReaderNode(
        robot_ip=options.robot_ip,
        data_port=options.data_port,
        topic=options.topic,
        publish_hz=options.publish_hz,
        request_timeout_s=options.request_timeout_s,
        reconnect_s=options.reconnect_s,
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
