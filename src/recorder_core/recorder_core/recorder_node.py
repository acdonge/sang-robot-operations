"""ROS 2 node entry point for the M4-1 recorder core skeleton."""

from enum import Enum, auto

import rclpy
from rclpy.node import Node

from recorder_core.manifest import ManifestValidationError, load_manifest


class RecorderState(Enum):
    IDLE = auto()
    PREFLIGHT = auto()
    WARMUP = auto()
    RECORDING = auto()
    COMMITTING = auto()
    READY = auto()
    ABORTING = auto()


class RecorderNode(Node):
    def __init__(self) -> None:
        super().__init__("recorder_core")
        self.declare_parameter("manifest_path", "")
        manifest_path = self.get_parameter("manifest_path").value
        if not manifest_path:
            raise ManifestValidationError("ROS parameter 'manifest_path' must not be empty")

        manifest = load_manifest(manifest_path)
        self.get_logger().info(
            "Loaded manifest: robot_id=%s, cameras=%d, feature_dimension=%d, fps=%d"
            % (
                manifest.robot.robot_id,
                len(manifest.interfaces.cameras),
                manifest.state_feature_dimension,
                manifest.recording.fps,
            )
        )


def main(args=None) -> None:
    rclpy.init(args=args)
    node = None
    try:
        node = RecorderNode()
        rclpy.spin(node)
    except ManifestValidationError as exc:
        if node is not None:
            node.get_logger().fatal(str(exc))
        else:
            rclpy.logging.get_logger("recorder_core").fatal(str(exc))
        raise
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()

