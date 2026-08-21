"""ROS 2 node that exposes validated XR frames, poses, TF, and diagnostics."""

from __future__ import annotations

import math

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from geometry_msgs.msg import Pose, PoseStamped, TransformStamped, Vector3
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from tf2_ros import TransformBroadcaster
from xr_teleop_interfaces.msg import XRControllerState, XRFrame

from xr_teleop_bridge.buffer import LatestFrameBuffer
from xr_teleop_bridge.protocol import ControllerData, PoseData, XRFrameData
from xr_teleop_bridge.server import ServerStats, TcpFrameServer


SENSOR_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.BEST_EFFORT,
    durability=DurabilityPolicy.VOLATILE,
)


class XRTeleopBridge(Node):
    def __init__(self) -> None:
        super().__init__("xr_teleop_bridge")
        self._declare_parameters()

        bind_host = str(self.get_parameter("bind_host").value)
        port = int(self.get_parameter("port").value)
        publish_hz = float(self.get_parameter("publish_hz").value)
        diagnostics_hz = float(self.get_parameter("diagnostics_hz").value)
        self._stale_timeout_ms = float(
            self.get_parameter("stale_timeout_ms").value
        )
        self._validate_parameters(
            bind_host, port, publish_hz, diagnostics_hz, self._stale_timeout_ms
        )

        self._left_child_frame = str(
            self.get_parameter("left_child_frame").value
        )
        self._right_child_frame = str(
            self.get_parameter("right_child_frame").value
        )
        self._head_child_frame = str(
            self.get_parameter("head_child_frame").value
        )

        self._frame_buffer = LatestFrameBuffer()
        self._last_diagnostic_message: str | None = None
        self._raw_publisher = self.create_publisher(
            XRFrame, "/xr/raw/frame", SENSOR_QOS
        )
        self._head_pose_publisher = self.create_publisher(
            PoseStamped, "/xr/head/pose", SENSOR_QOS
        )
        self._left_pose_publisher = self.create_publisher(
            PoseStamped, "/xr/left/grip_pose", SENSOR_QOS
        )
        self._right_pose_publisher = self.create_publisher(
            PoseStamped, "/xr/right/grip_pose", SENSOR_QOS
        )
        self._diagnostics_publisher = self.create_publisher(
            DiagnosticArray, "/xr/diagnostics", 10
        )
        self._tf_broadcaster = TransformBroadcaster(self)

        self._server = TcpFrameServer(
            bind_host,
            port,
            self._frame_buffer.update,
        )
        self._server.start()

        self._publish_timer = self.create_timer(
            1.0 / publish_hz, self._publish_latest
        )
        self._diagnostics_timer = self.create_timer(
            1.0 / diagnostics_hz, self._publish_diagnostics
        )
        self.get_logger().info(
            "XR TCP gateway listening on %s:%d; robot command output is disabled"
            % (bind_host, self._server.bound_port)
        )

    def _declare_parameters(self) -> None:
        self.declare_parameter("bind_host", "0.0.0.0")
        self.declare_parameter("port", 5556)
        self.declare_parameter("publish_hz", 120.0)
        self.declare_parameter("diagnostics_hz", 2.0)
        self.declare_parameter("stale_timeout_ms", 250.0)
        self.declare_parameter("head_child_frame", "xr_head")
        self.declare_parameter("left_child_frame", "xr_left_grip")
        self.declare_parameter("right_child_frame", "xr_right_grip")

    @staticmethod
    def _validate_parameters(
        bind_host: str,
        port: int,
        publish_hz: float,
        diagnostics_hz: float,
        stale_timeout_ms: float,
    ) -> None:
        if not bind_host:
            raise ValueError("bind_host must not be empty")
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        for name, value in (
            ("publish_hz", publish_hz),
            ("diagnostics_hz", diagnostics_hz),
            ("stale_timeout_ms", stale_timeout_ms),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be positive and finite")

    def _publish_latest(self) -> None:
        snapshot = self._frame_buffer.take_latest()
        if snapshot is None:
            return

        frame = snapshot.frame
        stamp = self.get_clock().now().to_msg()
        raw_message = self._frame_message(frame, stamp)
        self._raw_publisher.publish(raw_message)

        if frame.head.valid:
            self._head_pose_publisher.publish(
                self._pose_stamped(frame.head, frame.frame_id, stamp)
            )
        if frame.left.grip.valid:
            self._left_pose_publisher.publish(
                self._pose_stamped(frame.left.grip, frame.frame_id, stamp)
            )
        if frame.right.grip.valid:
            self._right_pose_publisher.publish(
                self._pose_stamped(frame.right.grip, frame.frame_id, stamp)
            )

        transforms = []
        if frame.head.tracked:
            transforms.append(
                self._transform(
                    frame.head, frame.frame_id, self._head_child_frame, stamp
                )
            )
        if frame.left.grip.tracked:
            transforms.append(
                self._transform(
                    frame.left.grip,
                    frame.frame_id,
                    self._left_child_frame,
                    stamp,
                )
            )
        if frame.right.grip.tracked:
            transforms.append(
                self._transform(
                    frame.right.grip,
                    frame.frame_id,
                    self._right_child_frame,
                    stamp,
                )
            )
        if transforms:
            self._tf_broadcaster.sendTransform(transforms)

    def _frame_message(self, frame: XRFrameData, stamp) -> XRFrame:
        message = XRFrame()
        message.header.stamp = stamp
        message.header.frame_id = frame.frame_id
        message.protocol_version = XRFrame.PROTOCOL_VERSION
        message.sequence = frame.sequence
        message.device_time_ns = frame.device_time_ns
        message.source = frame.source
        message.head_pose_valid = frame.head.valid
        message.head_pose_tracked = frame.head.tracked
        message.head_pose = self._pose_message(frame.head)
        message.left = self._controller_message(
            frame.left, XRControllerState.HAND_LEFT
        )
        message.right = self._controller_message(
            frame.right, XRControllerState.HAND_RIGHT
        )
        return message

    def _controller_message(
        self, controller: ControllerData, hand: int
    ) -> XRControllerState:
        message = XRControllerState()
        message.hand = hand
        message.connected = controller.connected
        message.grip_pose_valid = controller.grip.valid
        message.grip_pose_tracked = controller.grip.tracked
        message.grip_pose = self._pose_message(controller.grip)
        message.aim_pose_valid = controller.aim.valid
        message.aim_pose_tracked = controller.aim.tracked
        message.aim_pose = self._pose_message(controller.aim)
        message.linear_velocity = Vector3(
            x=controller.linear_velocity[0],
            y=controller.linear_velocity[1],
            z=controller.linear_velocity[2],
        )
        message.angular_velocity = Vector3(
            x=controller.angular_velocity[0],
            y=controller.angular_velocity[1],
            z=controller.angular_velocity[2],
        )
        message.trigger = controller.trigger
        message.squeeze = controller.squeeze
        message.thumbstick_x = controller.thumbstick[0]
        message.thumbstick_y = controller.thumbstick[1]
        message.buttons = controller.buttons
        return message

    @staticmethod
    def _pose_message(pose: PoseData) -> Pose:
        message = Pose()
        message.position.x = pose.position[0]
        message.position.y = pose.position[1]
        message.position.z = pose.position[2]
        message.orientation.x = pose.orientation[0]
        message.orientation.y = pose.orientation[1]
        message.orientation.z = pose.orientation[2]
        message.orientation.w = pose.orientation[3]
        return message

    def _pose_stamped(self, pose: PoseData, frame_id: str, stamp) -> PoseStamped:
        message = PoseStamped()
        message.header.stamp = stamp
        message.header.frame_id = frame_id
        message.pose = self._pose_message(pose)
        return message

    def _transform(
        self, pose: PoseData, frame_id: str, child_frame_id: str, stamp
    ) -> TransformStamped:
        message = TransformStamped()
        message.header.stamp = stamp
        message.header.frame_id = frame_id
        message.child_frame_id = child_frame_id
        message.transform.translation.x = pose.position[0]
        message.transform.translation.y = pose.position[1]
        message.transform.translation.z = pose.position[2]
        message.transform.rotation.x = pose.orientation[0]
        message.transform.rotation.y = pose.orientation[1]
        message.transform.rotation.z = pose.orientation[2]
        message.transform.rotation.w = pose.orientation[3]
        return message

    def _publish_diagnostics(self) -> None:
        stats = self._server.stats()
        age_ms = self._frame_buffer.age_ms()
        status = DiagnosticStatus()
        status.name = "xr_teleop_bridge/tcp_input"
        latest = self._frame_buffer.latest()
        status.hardware_id = latest.frame.source if latest is not None else "galaxy_xr"
        status.level, status.message = self._diagnostic_state(stats, age_ms)
        if status.message != self._last_diagnostic_message:
            self._log_diagnostic_transition(status.level, status.message)
            self._last_diagnostic_message = status.message
        status.values = [
            self._key("listening", stats.listening),
            self._key("connected", stats.connected),
            self._key("peer", stats.peer or "none"),
            self._key("bound_port", stats.bound_port),
            self._key("frame_age_ms", self._format_age(age_ms)),
            self._key("accepted_frames", stats.accepted_frames),
            self._key("malformed_frames", stats.malformed_frames),
            self._key("out_of_order_frames", stats.out_of_order_frames),
            self._key("sequence_gaps", stats.sequence_gaps),
            self._key("buffer_replacements", self._frame_buffer.replacements),
            self._key("last_sequence", stats.last_sequence),
            self._key("last_error", stats.last_error or "none"),
        ]
        message = DiagnosticArray()
        message.header.stamp = self.get_clock().now().to_msg()
        message.status = [status]
        self._diagnostics_publisher.publish(message)

    def _diagnostic_state(
        self, stats: ServerStats, age_ms: float
    ) -> tuple[int, str]:
        if not stats.listening:
            return DiagnosticStatus.ERROR, "TCP listener stopped"
        if stats.accepted_frames == 0:
            return DiagnosticStatus.WARN, "waiting for XR frames"
        if age_ms > self._stale_timeout_ms:
            return DiagnosticStatus.ERROR, "XR input stale"
        if not stats.connected:
            return DiagnosticStatus.WARN, "XR transport disconnected"
        return DiagnosticStatus.OK, "XR input fresh"

    @staticmethod
    def _key(key: str, value) -> KeyValue:
        return KeyValue(key=key, value=str(value))

    @staticmethod
    def _format_age(age_ms: float) -> str:
        return "inf" if math.isinf(age_ms) else f"{age_ms:.3f}"

    def _log_diagnostic_transition(self, level: int, message: str) -> None:
        if level == DiagnosticStatus.ERROR:
            self.get_logger().error(message)
        elif level == DiagnosticStatus.WARN:
            self.get_logger().warning(message)
        else:
            self.get_logger().info(message)

    def destroy_node(self) -> None:
        self._server.stop()
        super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = None
    try:
        node = XRTeleopBridge()
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
