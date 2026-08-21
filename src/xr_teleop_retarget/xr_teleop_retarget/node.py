"""ROS 2 node for safe Galaxy XR to SO-ARM target previews."""

from __future__ import annotations

import math

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from geometry_msgs.msg import Pose, PoseStamped, TransformStamped
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from std_msgs.msg import Bool, Float32
from std_srvs.srv import Trigger
from tf2_ros import TransformBroadcaster
from visualization_msgs.msg import Marker, MarkerArray
from xr_teleop_interfaces.msg import XRBimanualTarget, XRControllerState, XRFrame

from xr_teleop_retarget.retarget import (
    ControllerSample,
    RetargetConfig,
    RetargetResult,
    Retargeter,
)


class XRRetargetPreview(Node):
    def __init__(self) -> None:
        super().__init__("xr_retarget_preview")
        self._declare_parameters()
        self._base_frame = str(self.get_parameter("base_frame").value)
        self._left_base_frame = str(
            self.get_parameter("left_base_frame").value
        )
        self._right_base_frame = str(
            self.get_parameter("right_base_frame").value
        )
        self._target_child_frame = str(
            self.get_parameter("target_child_frame").value
        )
        self._left_target_child_frame = str(
            self.get_parameter("left_target_child_frame").value
        )
        self._config = self._load_config()
        self._retargeters = {
            "left": Retargeter(self._config),
            "right": Retargeter(self._config),
        }
        self._latest_samples: dict[str, ControllerSample | None] = {
            "left": None,
            "right": None,
        }
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

        self._preview_publisher = self.create_publisher(
            PoseStamped, "/xr/retarget/preview_pose", qos_profile_sensor_data
        )
        self._gripper_publisher = self.create_publisher(
            Float32, "/xr/retarget/preview_gripper", qos_profile_sensor_data
        )
        self._enabled_publisher = self.create_publisher(
            Bool, "/xr/retarget/enabled", 10
        )
        self._bimanual_publisher = self.create_publisher(
            XRBimanualTarget,
            "/xr/retarget/bimanual_target",
            qos_profile_sensor_data,
        )
        self._marker_publisher = self.create_publisher(
            MarkerArray, "/xr/retarget/markers", 10
        )
        self._diagnostics_publisher = self.create_publisher(
            DiagnosticArray, "/xr/retarget/diagnostics", 10
        )
        self._tf_broadcaster = TransformBroadcaster(self)
        self.create_subscription(
            XRFrame, source_topic, self._on_frame, qos_profile_sensor_data
        )
        self.create_service(
            Trigger, "/xr/retarget/calibrate", self._on_calibrate
        )
        self.create_service(
            Trigger, "/xr/retarget/reset_calibration", self._on_reset
        )
        self.create_timer(1.0 / publish_hz, self._publish_preview)
        self.create_timer(1.0 / diagnostics_hz, self._publish_diagnostics)
        self.get_logger().info(
            "Bimanual XR retarget preview ready; robot command output is disabled"
        )

    def _declare_parameters(self) -> None:
        self.declare_parameter("source_topic", "/xr/raw/frame")
        self.declare_parameter("base_frame", "soarm_base")
        self.declare_parameter("left_base_frame", "left_soarm_base")
        self.declare_parameter("right_base_frame", "right_soarm_base")
        self.declare_parameter("target_child_frame", "soarm_preview_target")
        self.declare_parameter(
            "left_target_child_frame", "left_soarm_preview_target"
        )
        self.declare_parameter("publish_hz", 60.0)
        self.declare_parameter("diagnostics_hz", 5.0)
        self.declare_parameter("input_timeout_ms", 250.0)
        self.declare_parameter("deadman_threshold", 0.5)
        self.declare_parameter("translation_scale", 0.5)
        self.declare_parameter(
            "neutral_position", [0.29323402, -0.00017677, 0.23437033]
        )
        self.declare_parameter(
            "neutral_orientation",
            [0.01721363, -0.70689953, -0.01720645, 0.70689510],
        )
        self.declare_parameter("workspace_min", [0.10, -0.20, 0.05])
        self.declare_parameter("workspace_max", [0.40, 0.20, 0.35])
        self.declare_parameter("max_rotation_delta_rad", math.pi / 2.0)

    def _load_config(self) -> RetargetConfig:
        timeout_ms = float(self.get_parameter("input_timeout_ms").value)
        if not math.isfinite(timeout_ms) or timeout_ms <= 0.0:
            raise ValueError("input_timeout_ms must be positive and finite")
        return RetargetConfig(
            neutral_position=tuple(
                self.get_parameter("neutral_position").value
            ),
            neutral_orientation=tuple(
                self.get_parameter("neutral_orientation").value
            ),
            translation_scale=float(
                self.get_parameter("translation_scale").value
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
        try:
            self._latest_samples = {
                "left": self._controller_sample(message.left, message.sequence),
                "right": self._controller_sample(message.right, message.sequence),
            }
        except ValueError as exc:
            self.get_logger().warning(f"Ignoring invalid controller frame: {exc}")
            return
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
        samples = self._latest_samples
        age_s = self._input_age_s()
        if any(sample is None for sample in samples.values()):
            response.success = False
            response.message = "No complete bimanual XR frame has been received"
            return response
        if age_s > self._config.stale_timeout_s:
            response.success = False
            response.message = f"XR input is stale ({age_s * 1000.0:.1f} ms)"
            return response
        invalid_hands = [
            hand
            for hand, sample in samples.items()
            if sample is None or not sample.tracking_valid
        ]
        if invalid_hands:
            response.success = False
            response.message = (
                "Controller pose must be connected, valid, and tracked: "
                + ", ".join(invalid_hands)
            )
            return response
        for hand, retargeter in self._retargeters.items():
            retargeter.calibrate(samples[hand])
        response.success = True
        response.message = f"Calibrated both controllers at sequence {samples['right'].sequence}"
        self.get_logger().info(response.message)
        return response

    def _on_reset(
        self, _request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        for retargeter in self._retargeters.values():
            retargeter.clear_calibration()
        response.success = True
        response.message = "Retarget calibration cleared"
        self.get_logger().info(response.message)
        return response

    def _publish_preview(self) -> None:
        results = self._results()
        left_result = results["left"]
        right_result = results["right"]
        bimanual_enabled = (
            left_result.output_enabled and right_result.output_enabled
        )
        stamp = self.get_clock().now().to_msg()
        self._enabled_publisher.publish(Bool(data=bimanual_enabled))
        self._bimanual_publisher.publish(
            self._bimanual_target(results, stamp, bimanual_enabled)
        )
        self._marker_publisher.publish(self._markers(right_result, stamp))

        if not bimanual_enabled:
            return
        if any(
            result.target_position is None or result.target_orientation is None
            for result in results.values()
        ):
            return

        preview = self._pose_stamped(
            right_result, stamp, self._base_frame
        )
        left_preview = self._pose_stamped(
            left_result, stamp, self._left_base_frame
        )
        self._preview_publisher.publish(preview)
        self._gripper_publisher.publish(Float32(data=right_result.gripper))
        self._tf_broadcaster.sendTransform(
            [
                self._transform(left_preview, self._left_target_child_frame),
                self._transform(preview, self._target_child_frame),
            ]
        )

    def _publish_diagnostics(self) -> None:
        results = self._results()
        left_result = results["left"]
        right_result = results["right"]
        output_enabled = (
            left_result.output_enabled and right_result.output_enabled
        )
        reason = self._bimanual_reason(results)
        status = DiagnosticStatus()
        status.name = "xr_teleop_retarget/preview_gate"
        status.hardware_id = "galaxy_xr_to_bi_soarm_preview"
        status.level = max(
            self._diagnostic_level(left_result),
            self._diagnostic_level(right_result),
        )
        status.message = reason
        status.values = [
            self._key("left_calibrated", left_result.calibrated),
            self._key("right_calibrated", right_result.calibrated),
            self._key("left_tracking_valid", left_result.tracking_valid),
            self._key("right_tracking_valid", right_result.tracking_valid),
            self._key("left_deadman_pressed", left_result.deadman_pressed),
            self._key("right_deadman_pressed", right_result.deadman_pressed),
            self._key("left_within_workspace", left_result.within_workspace),
            self._key("right_within_workspace", right_result.within_workspace),
            self._key("left_inhibit_reason", left_result.inhibit_reason),
            self._key("right_inhibit_reason", right_result.inhibit_reason),
            self._key("output_enabled", output_enabled),
            self._key("input_age_ms", self._format_age_ms()),
            self._key("sequence", right_result.sequence),
            self._key("left_gripper", f"{left_result.gripper:.3f}"),
            self._key("right_gripper", f"{right_result.gripper:.3f}"),
            self._key("robot_command_output", "disabled"),
        ]
        message = DiagnosticArray()
        message.header.stamp = self.get_clock().now().to_msg()
        message.status = [status]
        self._diagnostics_publisher.publish(message)
        if reason != self._last_reason:
            self.get_logger().info(f"Bimanual preview gate: {reason}")
            self._last_reason = reason

    def _results(self) -> dict[str, RetargetResult]:
        age_s = self._input_age_s()
        return {
            hand: retargeter.evaluate(self._latest_samples[hand], age_s)
            for hand, retargeter in self._retargeters.items()
        }

    @staticmethod
    def _bimanual_reason(results: dict[str, RetargetResult]) -> str:
        inhibited = [
            f"{hand}:{result.inhibit_reason}"
            for hand, result in results.items()
            if not result.output_enabled
        ]
        return "enabled" if not inhibited else ",".join(inhibited)

    def _input_age_s(self) -> float:
        return self._now_s() - self._latest_receive_time_s

    def _now_s(self) -> float:
        return self.get_clock().now().nanoseconds / 1_000_000_000.0

    def _pose_stamped(
        self, result: RetargetResult, stamp, frame_id: str
    ) -> PoseStamped:
        message = PoseStamped()
        message.header.stamp = stamp
        message.header.frame_id = frame_id
        if (
            result.target_position is not None
            and result.target_orientation is not None
        ):
            self._set_pose(
                message.pose, result.target_position, result.target_orientation
            )
        return message

    def _bimanual_target(
        self,
        results: dict[str, RetargetResult],
        stamp,
        bimanual_enabled: bool,
    ) -> XRBimanualTarget:
        message = XRBimanualTarget()
        message.header.stamp = stamp
        message.header.frame_id = "bimanual_local_targets"
        message.protocol_version = XRBimanualTarget.PROTOCOL_VERSION
        message.sequence = results["right"].sequence
        message.left_target = self._pose_stamped(
            results["left"], stamp, self._left_base_frame
        )
        message.right_target = self._pose_stamped(
            results["right"], stamp, self._right_base_frame
        )
        message.left_gripper = results["left"].gripper
        message.right_gripper = results["right"].gripper
        message.left_enabled = results["left"].output_enabled
        message.right_enabled = results["right"].output_enabled
        message.bimanual_enabled = bimanual_enabled
        message.left_inhibit_reason = results["left"].inhibit_reason
        message.right_inhibit_reason = results["right"].inhibit_reason
        return message

    def _transform(
        self, preview: PoseStamped, child_frame: str
    ) -> TransformStamped:
        message = TransformStamped()
        message.header = preview.header
        message.child_frame_id = child_frame
        message.transform.translation.x = preview.pose.position.x
        message.transform.translation.y = preview.pose.position.y
        message.transform.translation.z = preview.pose.position.z
        message.transform.rotation = preview.pose.orientation
        return message

    def _markers(self, result: RetargetResult, stamp) -> MarkerArray:
        markers = MarkerArray()
        markers.markers.append(self._workspace_marker(stamp))
        markers.markers.append(self._neutral_marker(stamp))
        markers.markers.append(self._target_marker(result, stamp))
        markers.markers.append(self._status_marker(result, stamp))
        return markers

    def _workspace_marker(self, stamp) -> Marker:
        marker = self._marker(stamp, "workspace", 0, Marker.CUBE)
        center = tuple(
            (lower + upper) / 2.0
            for lower, upper in zip(
                self._config.workspace_min, self._config.workspace_max
            )
        )
        size = tuple(
            upper - lower
            for lower, upper in zip(
                self._config.workspace_min, self._config.workspace_max
            )
        )
        self._set_pose(marker.pose, center, (0.0, 0.0, 0.0, 1.0))
        marker.scale.x, marker.scale.y, marker.scale.z = size
        marker.color.r = 0.10
        marker.color.g = 0.65
        marker.color.b = 0.80
        marker.color.a = 0.16
        return marker

    def _neutral_marker(self, stamp) -> Marker:
        marker = self._marker(stamp, "neutral", 1, Marker.SPHERE)
        self._set_pose(
            marker.pose,
            self._config.neutral_position,
            self._config.neutral_orientation,
        )
        marker.scale.x = marker.scale.y = marker.scale.z = 0.025
        marker.color.r = 0.95
        marker.color.g = 0.75
        marker.color.b = 0.10
        marker.color.a = 0.9
        return marker

    def _target_marker(self, result: RetargetResult, stamp) -> Marker:
        marker = self._marker(stamp, "target", 2, Marker.ARROW)
        if result.target_position is None or result.target_orientation is None:
            marker.action = Marker.DELETE
            return marker
        self._set_pose(
            marker.pose, result.target_position, result.target_orientation
        )
        marker.scale.x = 0.08
        marker.scale.y = 0.015
        marker.scale.z = 0.015
        marker.color.r = 0.10 if result.output_enabled else 0.95
        marker.color.g = 0.90 if result.output_enabled else 0.20
        marker.color.b = 0.20
        marker.color.a = 0.95
        return marker

    def _status_marker(self, result: RetargetResult, stamp) -> Marker:
        marker = self._marker(stamp, "status", 3, Marker.TEXT_VIEW_FACING)
        marker.pose.position.x = self._config.neutral_position[0]
        marker.pose.position.y = self._config.neutral_position[1]
        marker.pose.position.z = self._config.workspace_max[2] + 0.04
        marker.pose.orientation.w = 1.0
        marker.scale.z = 0.025
        marker.text = (
            "PREVIEW ENABLED"
            if result.output_enabled
            else f"INHIBITED: {result.inhibit_reason}"
        )
        marker.color.r = 0.10 if result.output_enabled else 0.95
        marker.color.g = 0.90 if result.output_enabled else 0.35
        marker.color.b = 0.20
        marker.color.a = 1.0
        return marker

    def _marker(self, stamp, namespace: str, marker_id: int, marker_type: int) -> Marker:
        marker = Marker()
        marker.header.stamp = stamp
        marker.header.frame_id = self._base_frame
        marker.ns = namespace
        marker.id = marker_id
        marker.type = marker_type
        marker.action = Marker.ADD
        return marker

    @staticmethod
    def _set_pose(pose: Pose, position, orientation) -> None:
        pose.position.x, pose.position.y, pose.position.z = position
        (
            pose.orientation.x,
            pose.orientation.y,
            pose.orientation.z,
            pose.orientation.w,
        ) = orientation

    @staticmethod
    def _diagnostic_level(result: RetargetResult) -> int:
        if result.output_enabled:
            return DiagnosticStatus.OK
        if result.inhibit_reason in {
            "input_stale",
            "outside_workspace",
            "rotation_limit",
        }:
            return DiagnosticStatus.ERROR
        return DiagnosticStatus.WARN

    @staticmethod
    def _key(key: str, value) -> KeyValue:
        return KeyValue(key=key, value=str(value))

    def _format_age_ms(self) -> str:
        age_s = self._input_age_s()
        return "inf" if math.isinf(age_s) else f"{age_s * 1000.0:.3f}"


def main(args=None) -> None:
    rclpy.init(args=args)
    node = None
    try:
        node = XRRetargetPreview()
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
