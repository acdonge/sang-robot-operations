"""ROS 2 recorder core with M4-② source health and frame gating."""

from enum import Enum, auto
import math
import threading
import time

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node

from recorder_core.assembly import (
    AssemblyError,
    assemble_joint_state,
    assemble_joint_trajectory,
)
from recorder_core.gate import check_frame
from recorder_core.manifest import ManifestValidationError, load_manifest
from recorder_core.subscriptions import create_subscriptions


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

        self.manifest = load_manifest(manifest_path)
        self._manifest_subscriptions = create_subscriptions(self, self.manifest)
        self._stats_lock = threading.Lock()
        self._gate_pass_count = 0
        self._gate_fail_count = 0
        self._last_failure_source: str | None = None
        self._last_failure_reason: str | None = None
        self._has_passed_gate = False
        self._active_failure_source: str | None = None

        self._sample_timer = self.create_timer(
            1.0 / self.manifest.recording.fps, self._sample_tick
        )
        self._health_timer = self.create_timer(1.0, self._log_health)

        self.get_logger().info(
            "Loaded manifest: robot_id=%s, cameras=%d, feature_dimension=%d, fps=%d"
            % (
                self.manifest.robot.robot_id,
                len(self.manifest.interfaces.cameras),
                self.manifest.state_feature_dimension,
                self.manifest.recording.fps,
            )
        )

    def _source_snapshots(self, source_key):
        source = self._manifest_subscriptions.sources[source_key]
        return tuple(topic.buffer.snapshot() for topic in source.topics)

    @staticmethod
    def _snapshot_age_ms(snapshot, now: float) -> float:
        if snapshot is None:
            return math.inf
        return max(0.0, (now - snapshot.received_monotonic) * 1000.0)

    def _sample_tick(self) -> None:
        now = time.monotonic()
        source_ages_ms = {}
        snapshots_by_source = {}
        for source_key, source in self._manifest_subscriptions.sources.items():
            snapshots = self._source_snapshots(source_key)
            snapshots_by_source[source_key] = snapshots
            source_ages_ms[source_key] = max(
                self._snapshot_age_ms(snapshot, now) for snapshot in snapshots
            )

        vectors = {}
        try:
            for interface in self.manifest.interfaces.state:
                snapshots = snapshots_by_source[interface.key]
                if all(snapshot is not None for snapshot in snapshots):
                    vectors[interface.key] = assemble_joint_state(
                        (snapshot.msg for snapshot in snapshots),
                        interface.feature_names,
                    )
            for interface in self.manifest.interfaces.action:
                snapshots = snapshots_by_source[interface.key]
                if all(snapshot is not None for snapshot in snapshots):
                    vectors[interface.key] = assemble_joint_trajectory(
                        (snapshot.msg for snapshot in snapshots),
                        interface.feature_names,
                    )
        except AssemblyError as exc:
            self._record_gate_failure(interface.key, str(exc))
            return

        cameras = {}
        for camera in self.manifest.interfaces.cameras:
            snapshot = snapshots_by_source[camera.key][0]
            if snapshot is not None:
                cameras[camera.key] = snapshot.msg

        source, reason = check_frame(
            self.manifest,
            source_ages_ms=source_ages_ms,
            vectors=vectors,
            cameras=cameras,
        )
        if source is not None:
            self._record_gate_failure(source, reason)
            return
        self._record_gate_pass()

    def _record_gate_pass(self) -> None:
        with self._stats_lock:
            self._gate_pass_count += 1
            self._has_passed_gate = True
            self._active_failure_source = None

    def _record_gate_failure(self, source: str, reason: str) -> None:
        should_log = False
        with self._stats_lock:
            self._gate_fail_count += 1
            self._last_failure_source = source
            self._last_failure_reason = reason
            if self._has_passed_gate and self._active_failure_source != source:
                should_log = True
            self._active_failure_source = source
        if should_log:
            self.get_logger().error(
                f"GATE_FAILURE source={source} reason={reason}; active episode must abort"
            )

    def get_health(self) -> dict:
        """Return source freshness and sampler counters for gateway consumers."""
        now = time.monotonic()
        sources = {}
        for source_key, source in self._manifest_subscriptions.sources.items():
            topic_health = {}
            for topic in source.topics:
                snapshot = topic.buffer.snapshot()
                age_ms = self._snapshot_age_ms(snapshot, now)
                topic_health[topic.topic] = {
                    "age_ms": age_ms,
                    "seq": 0 if snapshot is None else snapshot.seq,
                    "fresh": age_ms <= source.freshness_ms,
                }
            ages = [health["age_ms"] for health in topic_health.values()]
            sequences = [health["seq"] for health in topic_health.values()]
            sources[source_key] = {
                "age_ms": max(ages),
                "seq": min(sequences),
                "fresh": all(
                    health["fresh"] for health in topic_health.values()
                ),
                "topics": topic_health,
            }

        with self._stats_lock:
            gate = {
                "passed": self._gate_pass_count,
                "failed": self._gate_fail_count,
                "last_failure_source": self._last_failure_source,
                "last_failure_reason": self._last_failure_reason,
            }
        required = self.manifest.recording.required_sources
        return {
            "sources": sources,
            "all_sources_fresh": all(sources[key]["fresh"] for key in required),
            "gate": gate,
        }

    def _log_health(self) -> None:
        health = self.get_health()
        source_parts = []
        for source_key, source in health["sources"].items():
            age = source["age_ms"]
            age_text = "never" if math.isinf(age) else f"{age:.1f}"
            source_parts.append(
                "%s(age_ms=%s seq=%d fresh=%s)"
                % (
                    source_key,
                    age_text,
                    source["seq"],
                    str(source["fresh"]).lower(),
                )
            )
        gate = health["gate"]
        self.get_logger().info(
            "HEALTH all_sources_fresh=%s sources=[%s] gate_pass=%d gate_fail=%d "
            "last_failure_source=%s last_failure_reason=%s"
            % (
                str(health["all_sources_fresh"]).lower(),
                ", ".join(source_parts),
                gate["passed"],
                gate["failed"],
                gate["last_failure_source"] or "none",
                gate["last_failure_reason"] or "none",
            )
        )


def main(args=None) -> None:
    rclpy.init(args=args)
    node = None
    try:
        node = RecorderNode()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except ManifestValidationError as exc:
        if node is not None:
            node.get_logger().fatal(str(exc))
        else:
            rclpy.logging.get_logger("recorder_core").fatal(str(exc))
        raise
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
