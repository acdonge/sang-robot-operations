"""ROS 2 recorder node driving the manifest sampler and recorder FSM."""

import math
from pathlib import Path
import subprocess
import sys
import threading
import time

import numpy as np
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import qos_profile_sensor_data
from std_srvs.srv import Trigger
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

from recorder_core.assembly import (
    AssemblyError,
    assemble_joint_state,
    assemble_joint_trajectory,
)
from recorder_core.fsm import (
    FrameSampleError,
    RecorderFSM,
    RecorderState,
    Transition,
)
from recorder_core.gate import check_frame
from recorder_core.manifest import ManifestValidationError, load_manifest
from recorder_core.subscriptions import create_subscriptions
from recorder_core.writer import LeRobotV3Writer, lerobot_revision


class RecorderNode(Node):
    def __init__(
        self,
        manifest_path: str | Path | None = None,
        *,
        node_name: str = "recorder_core",
    ) -> None:
        super().__init__(node_name)
        self.declare_parameter(
            "manifest_path", "" if manifest_path is None else str(manifest_path)
        )
        manifest_path = str(self.get_parameter("manifest_path").value)
        if not manifest_path:
            raise ManifestValidationError("ROS parameter 'manifest_path' must not be empty")

        self.manifest_path = Path(manifest_path)
        self.manifest = load_manifest(self.manifest_path)
        self.declare_parameter("task", self.manifest.recording.task)
        self.declare_parameter("warmup_s", 1.0)
        self.declare_parameter("min_staging_free_gb", 1.0)

        warmup_s = float(self.get_parameter("warmup_s").value)
        min_free_gb = float(self.get_parameter("min_staging_free_gb").value)
        self._manifest_subscriptions = create_subscriptions(self, self.manifest)
        self._policy_action_publishers = []
        action_offset = 0
        for interface in self.manifest.interfaces.action:
            quotient, remainder = divmod(
                len(interface.feature_names), len(interface.topics)
            )
            interface_offset = 0
            for index, topic in enumerate(interface.topics):
                size = quotient + (1 if index < remainder else 0)
                names = interface.feature_names[
                    interface_offset : interface_offset + size
                ]
                self._policy_action_publishers.append(
                    (
                        self.create_publisher(
                            JointTrajectory, topic, qos_profile_sensor_data
                        ),
                        names,
                        slice(action_offset, action_offset + size),
                    )
                )
                interface_offset += size
                action_offset += size
        self._control_lock = threading.RLock()
        self._stats_lock = threading.Lock()
        self._gate_pass_count = 0
        self._gate_fail_count = 0
        self._episode_gate_pass_count = 0
        self._episode_gate_fail_count = 0
        self._episode_gate_active = False
        self._last_failure_source: str | None = None
        self._last_failure_reason: str | None = None
        self._has_passed_gate = False
        self._active_failure_source: str | None = None
        self._episode_start_sequences: dict[str, tuple[int, ...]] | None = None

        self.fsm = RecorderFSM(
            self.manifest,
            manifest_path=self.manifest_path,
            writer_factory=self._create_writer,
            sample_supplier=self._build_sample,
            gate_checker=self._check_sample,
            warmup_ready=self._required_sources_advanced,
            validation_runner=self._validate_staging,
            warmup_s=warmup_s,
            min_free_gb=min_free_gb,
            transition_callback=self._on_transition,
            frame_timing_callback=self._on_frame_timing,
            lerobot_sha=lerobot_revision(),
        )

        self._start_service = self.create_service(
            Trigger, "~/record_start", self._record_start
        )
        self._stop_service = self.create_service(
            Trigger, "~/record_stop", self._record_stop
        )
        self._abort_service = self.create_service(
            Trigger, "~/record_abort", self._record_abort
        )
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

    def _create_writer(self, session_id: str, dataset_id: str) -> LeRobotV3Writer:
        return LeRobotV3Writer(self.manifest, session_id, dataset_id)

    def _validate_staging(self, staging_path: Path) -> None:
        command = [
            sys.executable,
            "-m",
            "recorder_core.validate_cli",
            "--dataset-root",
            str(staging_path),
            "--manifest",
            str(self.manifest_path),
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"independent dataset validation failed (exit={completed.returncode}): "
                f"{detail}"
            )

    def _on_transition(self, transition: Transition) -> None:
        self.get_logger().info(
            "FSM_TRANSITION from=%s to=%s reason=%s"
            % (
                transition.previous.value,
                transition.current.value,
                transition.reason or "none",
            )
        )
        if transition.current is RecorderState.READY:
            self.get_logger().info(f"DATASET_READY path={self.fsm.raw_path}")
        if transition.current is RecorderState.WARMUP:
            self._episode_start_sequences = self._capture_required_sequences()
            with self._stats_lock:
                self._episode_gate_pass_count = 0
                self._episode_gate_fail_count = 0
                self._episode_gate_active = True
        if transition.current in (
            RecorderState.RESETTING,
            RecorderState.READY,
            RecorderState.FAULT,
        ):
            with self._stats_lock:
                self._episode_gate_active = False

    def _on_frame_timing(self, elapsed_ms: float) -> None:
        message = f"FRAME_TICK add_frame_ms={elapsed_ms:.1f}"
        if elapsed_ms > 50.0:
            self.get_logger().warning(message + " over_50ms=true")
        else:
            self.get_logger().debug(message + " over_50ms=false")

    def _source_snapshots(self, source_key):
        source = self._manifest_subscriptions.sources[source_key]
        return tuple(topic.buffer.snapshot() for topic in source.topics)

    def _capture_required_sequences(self) -> dict[str, tuple[int, ...]]:
        return {
            source_key: tuple(
                0 if snapshot is None else snapshot.seq
                for snapshot in self._source_snapshots(source_key)
            )
            for source_key in self.manifest.recording.required_sources
        }

    def _required_sources_advanced(self) -> bool:
        """Return true after every required topic has updated since record_start."""
        if self._episode_start_sequences is None:
            return False
        for source_key, start_sequences in self._episode_start_sequences.items():
            snapshots = self._source_snapshots(source_key)
            if any(
                snapshot is None or snapshot.seq <= start_seq
                for snapshot, start_seq in zip(snapshots, start_sequences)
            ):
                return False
        return True

    @staticmethod
    def _snapshot_age_ms(snapshot, now: float) -> float:
        if snapshot is None:
            return math.inf
        return max(0.0, (now - snapshot.received_monotonic) * 1000.0)

    def _build_sample(self, *, include_action: bool = True):
        now = time.monotonic()
        source_ages_ms = {}
        snapshots_by_source = {}
        for source_key in self._manifest_subscriptions.sources:
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
            if include_action:
                for interface in self.manifest.interfaces.action:
                    snapshots = snapshots_by_source[interface.key]
                    if all(snapshot is not None for snapshot in snapshots):
                        vectors[interface.key] = assemble_joint_trajectory(
                            (snapshot.msg for snapshot in snapshots),
                            interface.feature_names,
                        )
        except AssemblyError as exc:
            raise FrameSampleError(interface.key, str(exc)) from exc

        cameras = {}
        for camera in self.manifest.interfaces.cameras:
            snapshot = snapshots_by_source[camera.key][0]
            if snapshot is not None:
                cameras[camera.key] = snapshot.msg

        return {
            **vectors,
            **cameras,
            "_source_ages_ms": source_ages_ms,
            "_vectors": vectors,
            "_cameras": cameras,
        }

    def get_inference_sample(self):
        """Return the latest state/camera sample without requiring action input."""
        return self._build_sample(include_action=False)

    def publish_policy_action(self, action: np.ndarray) -> None:
        """Publish a manifest-ordered normalized action to leader topics."""
        vector = np.asarray(action, dtype=np.float32).reshape(-1)
        expected = sum(
            interface.dimension for interface in self.manifest.interfaces.action
        )
        if vector.shape != (expected,) or not np.all(np.isfinite(vector)):
            raise ValueError(
                f"policy action must be finite with shape {(expected,)}, got {vector.shape}"
            )
        stamp = self.get_clock().now().to_msg()
        for publisher, names, action_slice in self._policy_action_publishers:
            message = JointTrajectory()
            message.header.stamp = stamp
            message.joint_names = list(names)
            point = JointTrajectoryPoint()
            point.positions = [float(value) for value in vector[action_slice]]
            message.points = [point]
            publisher.publish(message)

    def _check_sample(self, sample):
        source, reason = check_frame(
            self.manifest,
            source_ages_ms=sample["_source_ages_ms"],
            vectors=sample["_vectors"],
            cameras=sample["_cameras"],
        )
        if source is None:
            self._record_gate_pass()
        else:
            self._record_gate_failure(source, reason or "frame gate failed")
        return source, reason

    def _sample_tick(self) -> None:
        with self._control_lock:
            if self.fsm.state in (
                RecorderState.PREFLIGHT,
                RecorderState.WARMUP,
                RecorderState.RECORDING,
                RecorderState.RESETTING,
                RecorderState.COMMITTING,
                RecorderState.ABORTING,
            ):
                self.fsm.tick()
                return
            try:
                sample = self._build_sample()
                self._check_sample(sample)
            except FrameSampleError as exc:
                self._record_gate_failure(exc.source, exc.reason)

    def _record_gate_pass(self) -> None:
        with self._stats_lock:
            self._gate_pass_count += 1
            if self._episode_gate_active:
                self._episode_gate_pass_count += 1
            self._has_passed_gate = True
            self._active_failure_source = None

    def _record_gate_failure(self, source: str, reason: str) -> None:
        should_log = False
        episode_active = False
        with self._stats_lock:
            self._gate_fail_count += 1
            if self._episode_gate_active:
                self._episode_gate_fail_count += 1
            self._last_failure_source = source
            self._last_failure_reason = reason
            if self._has_passed_gate and self._active_failure_source != source:
                should_log = True
            episode_active = self._episode_gate_active
            self._active_failure_source = source
        if should_log:
            consequence = (
                "active episode must abort"
                if episode_active
                else "source health gate failed"
            )
            self.get_logger().error(
                f"GATE_FAILURE source={source} reason={reason}; {consequence}"
            )

    def start_recording(
        self,
        task: str,
        *,
        task_instructions: list[str] | None = None,
        episode_time_s: float = 0.0,
        reset_time_s: float = 0.0,
        num_episodes: int = 0,
        rosbag: bool = False,
    ) -> bool:
        """Set the task and start directly, serialized with the sampler timer."""
        with self._control_lock:
            if self.fsm.state is RecorderState.READY:
                self.fsm.reset()
            if self.fsm.state is not RecorderState.IDLE:
                raise RuntimeError(
                    f"record_start requires IDLE or READY, got {self.fsm.state.value}"
                )
            parameter_result = self.set_parameters(
                [Parameter("task", Parameter.Type.STRING, task)]
            )[0]
            if not parameter_result.successful:
                raise ValueError(parameter_result.reason or "task parameter was rejected")
            return self._start_recording_locked(
                task_instructions=task_instructions,
                episode_time_s=episode_time_s,
                reset_time_s=reset_time_s,
                num_episodes=num_episodes,
                rosbag=rosbag,
            )

    def _start_recording_locked(
        self,
        *,
        task_instructions: list[str] | None = None,
        episode_time_s: float = 0.0,
        reset_time_s: float = 0.0,
        num_episodes: int = 0,
        rosbag: bool = False,
    ) -> bool:
        task = str(self.get_parameter("task").value)
        with self._stats_lock:
            self._episode_gate_pass_count = 0
            self._episode_gate_fail_count = 0
            self._episode_gate_active = True
            self._last_failure_source = None
            self._last_failure_reason = None
            self._has_passed_gate = False
            self._active_failure_source = None
        self._episode_start_sequences = self._capture_required_sequences()
        try:
            return self.fsm.start(
                task,
                task_instructions=task_instructions,
                episode_time_s=episode_time_s,
                reset_time_s=reset_time_s,
                num_episodes=num_episodes,
                rosbag=rosbag,
            )
        except (RuntimeError, ValueError):
            with self._stats_lock:
                self._episode_gate_active = False
            raise

    def stop_recording(self) -> bool:
        """Backward-compatible finish alias."""
        return self.finish_recording()

    def save_next_recording(self) -> bool:
        with self._control_lock:
            return self.fsm.save_and_next()

    def retry_recording(self) -> bool:
        with self._control_lock:
            return self.fsm.retry()

    def continue_recording(self) -> bool:
        with self._control_lock:
            return self.fsm.continue_after_reset()

    def finish_recording(self) -> bool:
        """Begin the asynchronous final commit from the gateway thread."""
        with self._control_lock:
            return self.fsm.finish()

    def abort_recording(self) -> bool:
        """Abort an active session, or explicitly reset a FAULT latch."""
        with self._control_lock:
            if self.fsm.state is RecorderState.FAULT:
                self.fsm.reset()
                return True
            return self.fsm.abort("gateway abort requested")

    def get_camera_jpeg(self, camera_key: str) -> bytes | None:
        """Return the latest compressed camera bytes without re-encoding."""
        camera_keys = {camera.key for camera in self.manifest.interfaces.cameras}
        if camera_key not in camera_keys:
            raise KeyError(camera_key)
        topic = self._manifest_subscriptions.sources[camera_key].topics[0]
        snapshot = topic.buffer.snapshot()
        if snapshot is None:
            return None
        return bytes(snapshot.msg.data)

    def _record_start(self, request, response):
        del request
        try:
            response.success = self.start_recording(
                str(self.get_parameter("task").value)
            )
            response.message = (
                f"session_id={self.fsm.session_id} state={self.fsm.state.value}"
                if response.success
                else f"preflight failed: {self.fsm.failure_reason}"
            )
        except (RuntimeError, ValueError) as exc:
            response.success = False
            response.message = str(exc)
        return response

    def _record_stop(self, request, response):
        del request
        try:
            response.success = self.stop_recording()
            response.message = (
                f"state={self.fsm.state.value} raw_path={self.fsm.raw_path}"
                if response.success
                else f"commit failed: {self.fsm.failure_reason}"
            )
        except RuntimeError as exc:
            response.success = False
            response.message = str(exc)
        return response

    def _record_abort(self, request, response):
        del request
        response.success = self.abort_recording()
        response.message = (
            f"state={self.fsm.state.value}"
            if response.success
            else f"nothing to abort in state={self.fsm.state.value}"
        )
        return response

    def reset(self) -> None:
        """Explicit reset hook for the gateway; every start still runs PREFLIGHT."""
        with self._control_lock:
            self.fsm.reset()

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
                "fresh": all(health["fresh"] for health in topic_health.values()),
                "topics": topic_health,
            }

        with self._stats_lock:
            gate = {
                "passed": self._gate_pass_count,
                "failed": self._gate_fail_count,
                "episode_passed": self._episode_gate_pass_count,
                "episode_failed": self._episode_gate_fail_count,
                "last_failure_source": self._last_failure_source,
                "last_failure_reason": self._last_failure_reason,
            }
        required = self.manifest.recording.required_sources
        return {
            "sources": sources,
            "all_sources_fresh": all(sources[key]["fresh"] for key in required),
            "gate": gate,
            "episode_gate_pass": gate["episode_passed"],
            "episode_gate_fail": gate["episode_failed"],
            "add_frame": self.fsm.get_frame_write_health(),
        }

    def get_status(self) -> dict:
        """Return FSM and source status for the future gateway."""
        return {"fsm": self.fsm.get_status(), **self.get_health()}

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
        add_frame = health["add_frame"]
        last_tick_ms = add_frame["last_tick_ms"]
        self.get_logger().info(
            "HEALTH all_sources_fresh=%s fsm_state=%s session_id=%s sources=[%s] "
            "gate_pass=%d gate_fail=%d last_failure_source=%s last_failure_reason=%s "
            "episode_gate_pass=%d episode_gate_fail=%d "
            "add_frame_last_ms=%s add_frame_max_ms=%.1f add_frame_over_50ms=%d"
            % (
                str(health["all_sources_fresh"]).lower(),
                self.fsm.state.value,
                self.fsm.session_id or "none",
                ", ".join(source_parts),
                gate["passed"],
                gate["failed"],
                gate["last_failure_source"] or "none",
                gate["last_failure_reason"] or "none",
                health["episode_gate_pass"],
                health["episode_gate_fail"],
                "none" if last_tick_ms is None else f"{last_tick_ms:.1f}",
                add_frame["max_tick_ms"],
                add_frame["ticks_over_50ms"],
            )
        )

    def destroy_node(self):
        if self.fsm.state in (
            RecorderState.PREFLIGHT,
            RecorderState.WARMUP,
            RecorderState.RECORDING,
            RecorderState.RESETTING,
            RecorderState.COMMITTING,
        ):
            self.fsm.abort("recorder node shutdown")
        self.fsm.close()
        return super().destroy_node()


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
