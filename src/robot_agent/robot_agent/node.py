"""ROS 2 node that monitors robot topics and pushes versioned fleet heartbeats."""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import math
from pathlib import Path
import socket
import sys
import time
from typing import Mapping
from uuid import uuid4

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.utilities import remove_ros_args
from sensor_msgs.msg import CompressedImage, JointState
from trajectory_msgs.msg import JointTrajectory

from sang_security.refs import SecretReferenceError, SecretResolver
from robot_agent.health import camera_format_is_valid, joint_sample_is_valid
from robot_agent.heartbeat import (
    AgentTLSConfig,
    SystemMetrics,
    build_heartbeat,
    post_heartbeat,
    require_control_plane_transport,
    require_loopback_http,
    validate_agent_identity,
)
from robot_agent.manifest import RobotManifest, load_manifest
from robot_agent.local_health import (
    LocalHealthError,
    build_local_health_snapshot,
    load_mapping_health_digest,
    load_local_health_key,
)
from robot_agent.monitor import RobotMonitor
from robot_agent.preview import CameraPreviewServer
from robot_agent.recording_inventory import RecordingInventory


MESSAGE_CLASSES = {
    "sensor_msgs/msg/CompressedImage": CompressedImage,
    "sensor_msgs/msg/JointState": JointState,
    "trajectory_msgs/msg/JointTrajectory": JointTrajectory,
}


def sample_is_valid(source, topic: str, message) -> bool:
    if source.kind == "state":
        return joint_sample_is_valid(
            source.features_for(topic),
            message.name,
            message.position,
        )
    if source.kind == "action":
        if len(message.points) != 1:
            return False
        return joint_sample_is_valid(
            source.features_for(topic),
            message.joint_names,
            message.points[0].positions,
        )
    return camera_format_is_valid(message.format)


def source_timestamp_s(message) -> float | None:
    stamp = getattr(getattr(message, "header", None), "stamp", None)
    if stamp is None:
        return None
    seconds = int(stamp.sec)
    nanoseconds = int(stamp.nanosec)
    if seconds == 0 and nanoseconds == 0:
        return None
    timestamp = seconds + nanoseconds / 1_000_000_000.0
    return timestamp if math.isfinite(timestamp) else None


class RobotAgentNode(Node):
    def __init__(
        self,
        manifests: list[RobotManifest],
        *,
        agent_id: str,
        agent_name: str,
        control_plane_url: str,
        storage_path: Path,
        capture_root: Path | None,
        interval_s: float,
        timeout_s: float,
        startup_grace_s: float,
        preview_host: str,
        preview_port: int,
        preview_public_base_url: str | None,
        advertise_preview: bool = True,
        heartbeat_tls: AgentTLSConfig | None = None,
        heartbeat_schema_version: int | None = None,
        local_health_signing_key: bytes | None = None,
        local_health_key_id: str | None = None,
        mapping_digests: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__("robot_agent")
        self._agent_id = agent_id
        self._agent_name = agent_name
        self._control_plane_url = control_plane_url
        self._timeout_s = timeout_s
        self._heartbeat_tls = heartbeat_tls
        self._heartbeat_schema_version = (
            1 if heartbeat_tls is None else 2
        ) if heartbeat_schema_version is None else heartbeat_schema_version
        if heartbeat_tls is None and self._heartbeat_schema_version != 1:
            raise ValueError("loopback heartbeat requires schema v1")
        if heartbeat_tls is not None and self._heartbeat_schema_version not in {2, 3}:
            raise ValueError("mTLS heartbeat requires schema v2 or v3")
        self._local_health_signing_key = local_health_signing_key
        self._local_health_key_id = local_health_key_id
        self._mapping_digests = dict(mapping_digests or {})
        self._metrics = SystemMetrics(storage_path)
        self._recording_inventory = RecordingInventory(
            capture_root,
            agent_id=agent_id,
        )
        self._heartbeat_enabled_at = time.monotonic() + startup_grace_s
        self._monitors = [RobotMonitor(manifest) for manifest in manifests]
        self._boot_id = str(uuid4())
        self._health_sequence = 0
        self._heartbeat_sequence = 0
        self._source_subscriptions = []
        self._sender = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fleet-heartbeat")
        self._send_future: Future | None = None
        self._preview = None
        self._advertise_preview = advertise_preview
        if preview_port > 0:
            self._preview = CameraPreviewServer(
                preview_host,
                preview_port,
                public_base_url=preview_public_base_url,
            )
            self._preview.start()

        for monitor in self._monitors:
            for source in monitor.manifest.sources:
                message_class = MESSAGE_CLASSES[source.message_type]
                for topic in source.topics:
                    callback = self._callback(monitor, source, topic)
                    self._source_subscriptions.append(
                        self.create_subscription(
                            message_class,
                            topic,
                            callback,
                            qos_profile_sensor_data,
                        )
                    )
        self._timer = self.create_timer(interval_s, self._heartbeat_tick)
        self.get_logger().info(
            f"monitoring {len(self._monitors)} robot(s) as Agent {agent_id}; "
            f"heartbeat v{self._heartbeat_schema_version}; "
            f"startup grace {startup_grace_s:g}s"
        )
        if self._preview is not None:
            self.get_logger().info(
                f"camera preview listening on {preview_host}:{self._preview.port}"
            )

    def _callback(self, monitor: RobotMonitor, source, topic: str):
        def receive(message) -> None:
            valid = sample_is_valid(source, topic, message)
            monitor.mark(
                source.key,
                topic,
                valid=valid,
                rate_timestamp_s=source_timestamp_s(message),
            )
            if self._preview is not None and source.kind == "camera" and valid:
                try:
                    self._preview.frame_store.update(
                        monitor.manifest.robot.robot_id,
                        source.key,
                        message.data,
                    )
                except ValueError as exc:
                    self.get_logger().warning(
                        f"camera preview ignored for {source.key}: {exc}"
                    )

        return receive

    def _robot_snapshots(self, now: float) -> list[dict]:
        snapshots = []
        for monitor in self._monitors:
            snapshot = monitor.robot_snapshot(
                now_monotonic=now,
                include_runtime_kind=self._heartbeat_schema_version == 3,
            )
            if self._preview is not None and self._advertise_preview:
                for source in snapshot["sources"]:
                    if source["kind"] == "camera":
                        source["preview_url"] = self._preview.stream_url(
                            snapshot["robot_id"], source["key"]
                        )
            snapshots.append(snapshot)
        return snapshots

    def _heartbeat_tick(self) -> None:
        now = time.monotonic()
        if now < self._heartbeat_enabled_at:
            return
        if self._send_future is not None and not self._send_future.done():
            self.get_logger().warning("previous heartbeat is still in flight")
            return
        self._heartbeat_sequence += 1
        payload = build_heartbeat(
            agent_id=self._agent_id,
            agent_name=self._agent_name,
            robots=self._robot_snapshots(now),
            storage=self._metrics.sample(),
            recordings=self._recording_inventory.sample(now_monotonic=now),
            boot_id=(self._boot_id if self._heartbeat_tls is not None else None),
            sequence=(
                self._heartbeat_sequence if self._heartbeat_tls is not None else None
            ),
            schema_version=self._heartbeat_schema_version,
        )
        if self._preview is not None:
            self._health_sequence += 1
            for monitor, robot in zip(
                self._monitors,
                payload["robots"],
                strict=True,
            ):
                snapshot = build_local_health_snapshot(
                    agent_id=self._agent_id,
                    boot_id=self._boot_id,
                    sequence=self._health_sequence,
                    snapshot_time_utc=payload["sent_at"],
                    manifest=monitor.manifest,
                    robot=robot,
                    mapping_digest=self._mapping_digests.get(
                        monitor.manifest.robot.robot_id
                    ),
                    signing_key=self._local_health_signing_key,
                    key_id=self._local_health_key_id,
                )
                self._preview.health_store.update(snapshot)
        self._send_future = self._sender.submit(
            post_heartbeat,
            self._control_plane_url,
            payload,
            timeout_s=self._timeout_s,
            tls=self._heartbeat_tls,
        )
        self._send_future.add_done_callback(self._heartbeat_done)

    def _heartbeat_done(self, future: Future) -> None:
        try:
            future.result()
        except Exception as exc:
            self.get_logger().error(f"heartbeat failed: {exc}")

    def destroy_node(self) -> None:
        self._timer.cancel()
        self._sender.shutdown(wait=True, cancel_futures=True)
        if self._preview is not None:
            self._preview.stop()
        super().destroy_node()


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SANG read-only ROS 2 Robot Agent")
    parser.add_argument("--manifest", action="append", required=True, type=Path)
    parser.add_argument("--mapping", action="append", default=[], type=Path)
    parser.add_argument("--agent-id")
    parser.add_argument("--agent-name", default=socket.gethostname())
    parser.add_argument("--control-plane-url", default="http://127.0.0.1:18091")
    parser.add_argument("--storage-path", type=Path, default=Path("/"))
    parser.add_argument("--capture-root", type=Path)
    parser.add_argument("--interval-s", type=float, default=5.0)
    parser.add_argument("--timeout-s", type=float, default=2.0)
    parser.add_argument("--startup-grace-s", type=float, default=3.0)
    parser.add_argument("--preview-host", default="127.0.0.1")
    parser.add_argument("--preview-port", type=int, default=0)
    parser.add_argument("--preview-public-base-url")
    parser.add_argument("--suppress-preview-advertisement", action="store_true")
    parser.add_argument(
        "--heartbeat-security-mode",
        choices=("loopback", "mtls"),
        default="loopback",
    )
    parser.add_argument(
        "--heartbeat-schema-version",
        type=int,
        choices=(1, 2, 3),
        help="explicit wire contract; defaults to v1 for loopback and v2 for mTLS",
    )
    parser.add_argument("--secret-root", type=Path)
    parser.add_argument("--tls-ca-bundle-ref")
    parser.add_argument("--tls-client-certificate-ref")
    parser.add_argument("--tls-client-private-key-ref")
    parser.add_argument("--local-health-key-ref")
    parser.add_argument("--local-health-key-id")
    options = parser.parse_args(args)
    if any(
        not math.isfinite(value) or value <= 0.0
        for value in (options.interval_s, options.timeout_s)
    ):
        parser.error("interval-s and timeout-s must be positive")
    if not math.isfinite(options.startup_grace_s) or options.startup_grace_s < 0.0:
        parser.error("startup-grace-s must be non-negative")
    if options.preview_host not in {"127.0.0.1", "localhost"}:
        parser.error("preview-host must be loopback in this release")
    if options.preview_port < 0 or options.preview_port > 65535:
        parser.error("preview-port must be between 0 and 65535")
    if options.preview_public_base_url is not None:
        try:
            require_loopback_http(options.preview_public_base_url)
        except ValueError:
            parser.error("preview-public-base-url must use loopback HTTP in this release")
    tls_values = (
        options.secret_root,
        options.tls_ca_bundle_ref,
        options.tls_client_certificate_ref,
        options.tls_client_private_key_ref,
        options.local_health_key_ref,
        options.local_health_key_id,
    )
    if options.heartbeat_security_mode == "loopback":
        if options.heartbeat_schema_version is None:
            options.heartbeat_schema_version = 1
        if options.heartbeat_schema_version != 1:
            parser.error("loopback heartbeat ingress only supports schema v1")
        if any(value is not None for value in tls_values):
            parser.error("TLS material requires --heartbeat-security-mode mtls")
        try:
            require_control_plane_transport(options.control_plane_url, None)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        if options.heartbeat_schema_version is None:
            options.heartbeat_schema_version = 2
        if options.heartbeat_schema_version not in {2, 3}:
            parser.error("mTLS heartbeat ingress requires schema v2 or v3")
        required_tls = tls_values[:4]
        if any(value is None for value in required_tls):
            parser.error("mTLS heartbeat requires secret root, CA, certificate and key")
        if (options.local_health_key_ref is None) != (
            options.local_health_key_id is None
        ):
            parser.error("local health key ref and key id must be supplied together")
        if options.preview_port > 0 and options.local_health_key_ref is None:
            parser.error("mTLS Agent preview requires an authenticated local health key")
        marker = AgentTLSConfig(Path("."), Path("."), Path("."))
        try:
            require_control_plane_transport(options.control_plane_url, marker)
        except ValueError as exc:
            parser.error(str(exc))
    return options


def main(args: list[str] | None = None) -> None:
    raw_args = sys.argv if args is None else [sys.argv[0], *args]
    clean_args = remove_ros_args(args=raw_args)[1:]
    options = parse_args(clean_args)
    heartbeat_tls = None
    local_health_signing_key = None
    if options.heartbeat_security_mode == "mtls":
        try:
            resolver = SecretResolver(options.secret_root)
            heartbeat_tls = AgentTLSConfig(
                ca_bundle=resolver.resolve(options.tls_ca_bundle_ref),
                client_certificate_chain=resolver.resolve(
                    options.tls_client_certificate_ref
                ),
                client_private_key=resolver.resolve(
                    options.tls_client_private_key_ref,
                    private=True,
                ),
            )
            heartbeat_tls.context()
            if options.local_health_key_ref is not None:
                local_health_signing_key = load_local_health_key(
                    resolver.resolve(
                        options.local_health_key_ref,
                        private=True,
                    )
                )
        except (SecretReferenceError, LocalHealthError, OSError, ValueError) as exc:
            raise SystemExit(f"invalid heartbeat mTLS material: {exc}") from exc
    manifests = [load_manifest(path) for path in options.manifest]
    if options.mapping and len(options.mapping) != len(manifests):
        raise SystemExit("--mapping must be supplied once per --manifest in the same order")
    mapping_digests = {}
    try:
        for manifest, path in zip(manifests, options.mapping):
            mapping_digests[manifest.robot.robot_id] = load_mapping_health_digest(
                path,
                expected_robot_id=manifest.robot.robot_id,
            )
    except LocalHealthError as exc:
        raise SystemExit(f"invalid OPC UA mapping binding: {exc}") from exc
    host_ids = {manifest.robot.host_id for manifest in manifests}
    if options.agent_id is None:
        if len(host_ids) != 1:
            raise SystemExit("--agent-id is required when manifests use different host_id values")
        options.agent_id = next(iter(host_ids))
    validate_agent_identity(options.agent_id, options.agent_name)

    rclpy.init(args=raw_args)
    node = RobotAgentNode(
        manifests,
        agent_id=options.agent_id,
        agent_name=options.agent_name,
        control_plane_url=options.control_plane_url,
        storage_path=options.storage_path,
        capture_root=options.capture_root,
        interval_s=options.interval_s,
        timeout_s=options.timeout_s,
        startup_grace_s=options.startup_grace_s,
        preview_host=options.preview_host,
        preview_port=options.preview_port,
        preview_public_base_url=options.preview_public_base_url,
        advertise_preview=not options.suppress_preview_advertisement,
        heartbeat_tls=heartbeat_tls,
        heartbeat_schema_version=options.heartbeat_schema_version,
        local_health_signing_key=local_health_signing_key,
        local_health_key_id=options.local_health_key_id,
        mapping_digests=mapping_digests,
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
