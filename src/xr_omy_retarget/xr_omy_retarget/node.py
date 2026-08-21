"""ROS 2 node for safe Galaxy XR to OMY Cartesian target previews."""

from __future__ import annotations

import math

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from geometry_msgs.msg import PoseStamped, TransformStamped
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from std_msgs.msg import Bool, Float32
from std_srvs.srv import Trigger
from tf2_ros import TransformBroadcaster
from xr_teleop_interfaces.msg import XRControllerState, XRFrame, XROmyTarget

from xr_omy_retarget.retarget import (
    ControllerSample,
    RetargetConfig,
    RetargetResult,
    Retargeter,
)


class XROmyRetarget(Node):
    def __init__(self) -> None:
        super().__init__("xr_omy_retarget")
        self._declare_parameters()
        self._controller_hand = str(
            self.get_parameter("controller_hand").value
        ).lower()
        if self._controller_hand not in {"left", "right"}:
            raise ValueError("controller_hand must be 'left' or 'right'")
        self._base_frame = str(self.get_parameter("base_frame").value)
        self._target_link = str(self.get_parameter("target_link").value)
        self._target_child_frame = str(
            self.get_parameter("target_child_frame").value
        )
        for name, value in (
            ("base_frame", self._base_frame),
            ("target_link", self._target_link),
            ("target_child_frame", self._target_child_frame),
        ):
            if not value:
                raise ValueError(f"{name} must not be empty")

        self._config = self._load_config()
        self._retargeter = Retargeter(self._config)
        self._latest_result = self._retargeter.evaluate(None, math.inf)
        self._latest_sample: ControllerSample | None = None
        self._latest_head_tracking_valid = False
        self._latest_receive_time_s = -math.inf
        self._last_reason: str | None = None

        source_topic = str(self.get_parameter("source_topic").value)
        publish_hz = float(self.get_parameter("publish_hz").value)
        diagnostics_hz = float(self.get_parameter("diagnostics_hz").value)
        if not source_topic:
            raise ValueError("source_topic must not be empty")
        for name, value in (
            ("publish_hz", publish_hz),
            ("diagnostics_hz", diagnostics_hz),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be positive and finite")

        self._target_publisher = self.create_publisher(
            XROmyTarget, "/xr/omy/target", qos_profile_sensor_data
        )
        self._preview_publisher = self.create_publisher(
            PoseStamped, "/xr/omy/preview_pose", qos_profile_sensor_data
        )
        self._gripper_publisher = self.create_publisher(
            Float32, "/xr/omy/preview_gripper", qos_profile_sensor_data
        )
        self._enabled_publisher = self.create_publisher(
            Bool, "/xr/omy/enabled", 10
        )
        self._diagnostics_publisher = self.create_publisher(
            DiagnosticArray, "/xr/omy/diagnostics", 10
        )
        self._tf_broadcaster = TransformBroadcaster(self)
        self.create_subscription(
            XRFrame, source_topic, self._on_frame, qos_profile_sensor_data
        )
        self.create_service(Trigger, "/xr/omy/calibrate", self._on_calibrate)
        self.create_service(
            Trigger, "/xr/omy/reset_calibration", self._on_reset
        )
        self.create_timer(1.0 / publish_hz, self._publish_target)
        self.create_timer(1.0 / diagnostics_hz, self._publish_diagnostics)
        self.get_logger().info(
            "OMY XR Cartesian preview ready; joint and robot command output is disabled"
        )

    def _declare_parameters(self) -> None:
        self.declare_parameter("source_topic", "/xr/raw/frame")
        self.declare_parameter("controller_hand", "right")
        self.declare_parameter("base_frame", "link0")
        self.declare_parameter("target_link", "end_effector_link")
        self.declare_parameter("target_child_frame", "omy_xr_target")
        self.declare_parameter("publish_hz", 60.0)
        self.declare_parameter("diagnostics_hz", 5.0)
        self.declare_parameter("input_timeout_ms", 250.0)
        self.declare_parameter("deadman_threshold", 0.5)
        self.declare_parameter("translation_scale", 1.0)
        self.declare_parameter("track_orientation", False)
        self.declare_parameter(
            "neutral_position", [0.19080657, -0.11299910, 0.39004890]
        )
        self.declare_parameter(
            "neutral_orientation", [0.0, 0.0, 0.70710678, 0.70710678]
        )
        self.declare_parameter("workspace_min", [0.05, -0.45, 0.05])
        self.declare_parameter("workspace_max", [0.58, 0.40, 0.72])
        self.declare_parameter("max_rotation_delta_rad", 2.09439510239)

    def _load_config(self) -> RetargetConfig:
        timeout_ms = float(self.get_parameter("input_timeout_ms").value)
        if not math.isfinite(timeout_ms) or timeout_ms <= 0.0:
            raise ValueError("input_timeout_ms must be positive and finite")
        return RetargetConfig(
            neutral_position=tuple(self.get_parameter("neutral_position").value),
            neutral_orientation=tuple(
                self.get_parameter("neutral_orientation").value
            ),
            translation_scale=float(
                self.get_parameter("translation_scale").value
            ),
            track_orientation=bool(
                self.get_parameter("track_orientation").value
            ),
            workspace_min=tuple(self.get_parameter("workspace_min").value),
            workspace_max=tuple(self.get_parameter("workspace_max").value),
            max_rotation_delta_rad=float(
                self.get_parameter("max_rotation_delta_rad").value
            ),
            deadman_threshold=float(
                self.get_parameter("deadman_threshold").value
            ),
            stale_timeout_s=timeout_ms / 1000.0,
        )

    def _on_frame(self, message: XRFrame) -> None:
        if message.protocol_version != XRFrame.PROTOCOL_VERSION:
            self.get_logger().warning(
                f"Ignoring XR protocol version {message.protocol_version}"
            )
            return
        controller = (
            message.left if self._controller_hand == "left" else message.right
        )
        try:
            self._latest_sample = self._controller_sample(
                controller, message.sequence
            )
        except ValueError as exc:
            self.get_logger().warning(f"Ignoring invalid controller frame: {exc}")
            return
        self._latest_head_tracking_valid = bool(
            message.head_pose_valid and message.head_pose_tracked
        )
        self._latest_receive_time_s = self._now_s()

    @staticmethod
    def _controller_sample(
        controller: XRControllerState, sequence: int
    ) -> ControllerSample:
        pose = controller.grip_pose
        return ControllerSample(
            position=(pose.position.x, pose.position.y, pose.position.z),
            orientation=(
                pose.orientation.x,
                pose.orientation.y,
                pose.orientation.z,
                pose.orientation.w,
            ),
            connected=controller.connected,
            pose_valid=controller.grip_pose_valid,
            pose_tracked=controller.grip_pose_tracked,
            squeeze=controller.squeeze,
            trigger=controller.trigger,
            sequence=sequence,
        )

    def _on_calibrate(
        self, _request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        if self._latest_sample is None:
            response.success = False
            response.message = "No XR controller frame has been received"
            return response
        age_s = self._input_age_s()
        if (
            not math.isfinite(age_s)
            or age_s < 0.0
            or age_s > self._config.stale_timeout_s
        ):
            response.success = False
            response.message = f"XR input is stale ({age_s * 1000.0:.1f} ms)"
            return response
        if not self._latest_sample.tracking_valid:
            response.success = False
            response.message = "Controller pose must be connected, valid, and tracked"
            return response
        try:
            self._retargeter.calibrate(self._latest_sample)
        except ValueError as exc:
            response.success = False
            response.message = f"Calibration failed: {exc}"
            return response
        response.success = True
        response.message = (
            f"Calibrated fixed XR axes and {self._controller_hand} controller at "
            f"sequence {self._latest_sample.sequence}"
        )
        self.get_logger().info(response.message)
        return response

    def _on_reset(
        self, _request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        self._retargeter.clear_calibration()
        self._latest_result = self._retargeter.evaluate(
            self._latest_sample, self._input_age_s()
        )
        response.success = True
        response.message = "OMY XR calibration cleared"
        self.get_logger().info(response.message)
        return response

    def _publish_target(self) -> None:
        result = self._retargeter.evaluate(
            self._latest_sample, self._input_age_s()
        )
        self._latest_result = result
        stamp = self.get_clock().now().to_msg()
        message = self._target_message(result, stamp)
        self._target_publisher.publish(message)
        self._enabled_publisher.publish(Bool(data=result.output_enabled))
        if not result.output_enabled:
            return
        self._preview_publisher.publish(message.target)
        self._gripper_publisher.publish(Float32(data=result.gripper))
        self._tf_broadcaster.sendTransform(
            self._transform(message.target, self._target_child_frame)
        )

    def _target_message(self, result: RetargetResult, stamp) -> XROmyTarget:
        message = XROmyTarget()
        message.header.stamp = stamp
        message.protocol_version = XROmyTarget.PROTOCOL_VERSION
        message.sequence = result.sequence
        message.target_link = self._target_link
        message.target.header.stamp = stamp
        message.target.header.frame_id = self._base_frame
        if result.target_position is not None:
            (
                message.target.pose.position.x,
                message.target.pose.position.y,
                message.target.pose.position.z,
            ) = result.target_position
        if result.target_orientation is not None:
            (
                message.target.pose.orientation.x,
                message.target.pose.orientation.y,
                message.target.pose.orientation.z,
                message.target.pose.orientation.w,
            ) = result.target_orientation
        message.gripper = result.gripper
        message.enabled = result.output_enabled
        message.inhibit_reason = result.inhibit_reason
        message.clutch_epoch = result.clutch_reanchors
        return message

    def _publish_diagnostics(self) -> None:
        result = self._latest_result
        status = DiagnosticStatus()
        status.name = "xr_omy_retarget/preview_gate"
        status.hardware_id = "galaxy_xr_to_robotis_omy_preview"
        status.level = self._diagnostic_level(result.inhibit_reason)
        status.message = result.inhibit_reason
        status.values = [
            self._key("controller_hand", self._controller_hand),
            self._key("track_orientation", self._config.track_orientation),
            self._key("base_frame", self._base_frame),
            self._key("target_link", self._target_link),
            self._key("calibrated", result.calibrated),
            self._key("latest_head_tracking_valid", self._latest_head_tracking_valid),
            self._key("controller_tracking_valid", result.tracking_valid),
            self._key("deadman_pressed", result.deadman_pressed),
            self._key("within_workspace", result.within_workspace),
            self._key("rotation_within_limit", result.rotation_within_limit),
            self._key("output_enabled", result.output_enabled),
            self._key("input_age_ms", self._format_age_ms()),
            self._key("sequence", result.sequence),
            self._key("gripper", f"{result.gripper:.3f}"),
            self._key("clutch_epoch", result.clutch_reanchors),
            self._key("robot_command_output", "disabled"),
        ]
        message = DiagnosticArray()
        message.header.stamp = self.get_clock().now().to_msg()
        message.status = [status]
        self._diagnostics_publisher.publish(message)
        if result.inhibit_reason != self._last_reason:
            self.get_logger().info(
                f"OMY XR preview gate: {result.inhibit_reason}"
            )
            self._last_reason = result.inhibit_reason

    def _input_age_s(self) -> float:
        return self._now_s() - self._latest_receive_time_s

    def _now_s(self) -> float:
        return self.get_clock().now().nanoseconds / 1_000_000_000.0

    def _format_age_ms(self) -> str:
        age_ms = self._input_age_s() * 1000.0
        return f"{age_ms:.1f}" if math.isfinite(age_ms) else "inf"

    @staticmethod
    def _transform(
        pose: PoseStamped, child_frame: str
    ) -> TransformStamped:
        transform = TransformStamped()
        transform.header = pose.header
        transform.child_frame_id = child_frame
        transform.transform.translation.x = pose.pose.position.x
        transform.transform.translation.y = pose.pose.position.y
        transform.transform.translation.z = pose.pose.position.z
        transform.transform.rotation = pose.pose.orientation
        return transform

    @staticmethod
    def _diagnostic_level(reason: str) -> int:
        if reason == "enabled":
            return DiagnosticStatus.OK
        if reason in {"not_calibrated", "deadman_released"}:
            return DiagnosticStatus.WARN
        return DiagnosticStatus.ERROR

    @staticmethod
    def _key(key: str, value) -> KeyValue:
        text = str(value).lower() if isinstance(value, bool) else str(value)
        return KeyValue(key=key, value=text)


def main(args=None) -> None:
    rclpy.init(args=args)
    node = None
    try:
        node = XROmyRetarget()
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
