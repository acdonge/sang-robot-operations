"""ROS 2 publisher for read-only SO-101 follower and leader observations."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from functools import partial
from pathlib import Path
import sys
import time
from typing import Callable

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.utilities import remove_ros_args
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

from so101_readonly_adapter.calibration import ArmCalibration, load_calibration
from so101_readonly_adapter.config import ArmConfig, load_hardware_config
from so101_readonly_adapter.feetech_bus import ReadOnlyFeetechBus


BusFactory = Callable[[ArmConfig, ArmCalibration], ReadOnlyFeetechBus]


@dataclass
class ArmRuntime:
    config: ArmConfig
    calibration: ArmCalibration
    bus: ReadOnlyFeetechBus
    publisher: object
    next_connect_at: float = 0.0
    last_error: str | None = None


def select_arms(arms: tuple[ArmConfig, ...], keys: list[str]) -> tuple[ArmConfig, ...]:
    if not keys:
        return arms
    requested = set(keys)
    available = {arm.key for arm in arms}
    unknown = requested - available
    if unknown:
        raise ValueError("unknown arm keys: " + ", ".join(sorted(unknown)))
    return tuple(arm for arm in arms if arm.key in requested)


class SO101ArmReaderNode(Node):
    def __init__(
        self,
        arms: tuple[ArmConfig, ...],
        *,
        reconnect_s: float,
        bus_factory: BusFactory | None = None,
    ) -> None:
        super().__init__("so101_readonly_arm_reader")
        self._reconnect_s = reconnect_s
        self._read_timers = []
        self._runtimes = []
        factory = bus_factory or self._make_bus

        for arm in arms:
            calibration = load_calibration(arm.calibration_path)
            message_type = JointState if arm.role == "follower" else JointTrajectory
            publisher = self.create_publisher(
                message_type,
                arm.topic,
                qos_profile_sensor_data,
            )
            runtime = ArmRuntime(
                config=arm,
                calibration=calibration,
                bus=factory(arm, calibration),
                publisher=publisher,
            )
            self._runtimes.append(runtime)
            self._read_timers.append(
                self.create_timer(
                    1.0 / arm.publish_hz,
                    partial(self._tick, runtime),
                )
            )
        self.get_logger().info(
            f"read-only position monitoring configured for {len(arms)} SO-101 arm(s)"
        )

    @staticmethod
    def _make_bus(
        config: ArmConfig,
        calibration: ArmCalibration,
    ) -> ReadOnlyFeetechBus:
        return ReadOnlyFeetechBus(
            config.port,
            calibration,
            baudrate=config.baudrate,
            expected_model=config.expected_model,
        )

    def _failure(self, runtime: ArmRuntime, exc: Exception, now: float) -> None:
        message = f"{type(exc).__name__}: {exc}"
        if message != runtime.last_error:
            self.get_logger().error(f"{runtime.config.key}: {message}")
        runtime.last_error = message
        runtime.next_connect_at = now + self._reconnect_s
        runtime.bus.close()

    def _connect(self, runtime: ArmRuntime, now: float) -> bool:
        if runtime.bus.is_open:
            return True
        if now < runtime.next_connect_at:
            return False
        try:
            runtime.bus.open()
        except Exception as exc:
            self._failure(runtime, exc, now)
            return False
        runtime.last_error = None
        self.get_logger().info(
            f"{runtime.config.key}: reading {runtime.config.port} without register writes"
        )
        return True

    def _tick(self, runtime: ArmRuntime) -> None:
        now = time.monotonic()
        if not self._connect(runtime, now):
            return
        try:
            raw_positions = runtime.bus.read_positions()
            positions = runtime.calibration.normalize(raw_positions)
        except Exception as exc:
            self._failure(runtime, exc, now)
            return
        stamp = self.get_clock().now().to_msg()
        names = [
            f"{runtime.config.side}_{name}"
            for name in runtime.calibration.motor_names
        ]
        if runtime.config.role == "follower":
            message = JointState()
            message.header.stamp = stamp
            message.header.frame_id = "so101_readonly_adapter"
            message.name = names
            message.position = list(positions)
        else:
            message = JointTrajectory()
            message.header.stamp = stamp
            message.header.frame_id = "so101_readonly_adapter"
            message.joint_names = names
            point = JointTrajectoryPoint()
            point.positions = list(positions)
            message.points = [point]
        runtime.publisher.publish(message)

    def destroy_node(self) -> None:
        for timer in self._read_timers:
            timer.cancel()
        for runtime in self._runtimes:
            runtime.bus.close()
        super().destroy_node()


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only SO-101 ROS 2 arm adapter")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--arm", action="append", default=[])
    parser.add_argument("--reconnect-s", type=float, default=2.0)
    options = parser.parse_args(args)
    if options.reconnect_s <= 0.0:
        parser.error("reconnect-s must be positive")
    return options


def main(args: list[str] | None = None) -> None:
    raw_args = sys.argv if args is None else [sys.argv[0], *args]
    clean_args = remove_ros_args(args=raw_args)[1:]
    options = parse_args(clean_args)
    hardware = load_hardware_config(options.config)
    try:
        arms = select_arms(hardware.arms, options.arm)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    rclpy.init(args=raw_args)
    node = SO101ArmReaderNode(arms, reconnect_s=options.reconnect_s)
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
