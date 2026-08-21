"""ROS 2 subscriptions and process lifecycle for the read-only OPC UA gateway."""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import math
from pathlib import Path
import sys
import time

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.utilities import remove_ros_args
from sensor_msgs.msg import CompressedImage, JointState
from trajectory_msgs.msg import JointTrajectory

from robot_agent.local_health import LocalHealthError, load_local_health_key
from sang_security.opcua import OpcUaSecurityError, OpcUaServerSecurity
from sang_security.refs import SecretReferenceError, SecretResolver
from sang_opcua_gateway.contract import ContractError, load_gateway_contract
from sang_opcua_gateway.local_health_client import (
    LocalHealthReplayStore,
    fetch_local_health,
    snapshot_url,
)
from sang_opcua_gateway.server import OpcUaServerThread
from sang_opcua_gateway.state import GatewayStore


UTC = timezone.utc
MESSAGE_CLASSES = {
    "sensor_msgs/msg/CompressedImage": CompressedImage,
    "sensor_msgs/msg/JointState": JointState,
    "trajectory_msgs/msg/JointTrajectory": JointTrajectory,
}


def _source_timestamp(message) -> datetime | None:
    stamp = getattr(getattr(message, "header", None), "stamp", None)
    if stamp is None:
        return None
    seconds = int(stamp.sec)
    nanoseconds = int(stamp.nanosec)
    if seconds == 0 and nanoseconds == 0:
        return None
    try:
        return datetime.fromtimestamp(seconds + nanoseconds / 1_000_000_000.0, UTC)
    except (OSError, OverflowError, ValueError):
        return None


class SangOpcUaGatewayNode(Node):
    def __init__(
        self,
        contract,
        *,
        publish_interval_s: float,
        security=None,
        local_health_verification_key: bytes | None = None,
        local_health_key_id: str | None = None,
        local_health_replay_store: LocalHealthReplayStore | None = None,
    ) -> None:
        super().__init__("sang_opcua_gateway")
        self.contract = contract
        self._local_health_verification_key = local_health_verification_key
        self._local_health_key_id = local_health_key_id
        self._local_health_replay_store = local_health_replay_store
        self._health_url = (
            None
            if contract.preview_base_url is None
            else snapshot_url(
                contract.preview_base_url,
                contract.manifest.robot.robot_id,
            )
        )
        self.store = GatewayStore(
            contract,
            authoritative_health_timeout_s=(
                contract.authoritative_health_timeout_s
                if self._health_url is not None
                else None
            ),
        )
        self._subscription_handles = []
        self._health_executor: ThreadPoolExecutor | None = None
        self._health_future: Future | None = None
        self._health_timer = None
        self._last_health_error_log = 0.0
        self._server = OpcUaServerThread(
            contract,
            self.store,
            publish_interval_s=publish_interval_s,
            security=security,
        )
        for source in contract.sources:
            message_class = MESSAGE_CLASSES[source.message_type]
            for topic in source.topics:
                self._subscription_handles.append(
                    self.create_subscription(
                        message_class,
                        topic.topic,
                        self._callback(topic.kind, topic.topic),
                        qos_profile_sensor_data,
                    )
                )
        self._graph_timer = self.create_timer(1.0, self._check_discovered_types)
        if self._health_url is not None:
            self._health_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="agent-health",
            )
            self._health_future = self._health_executor.submit(
                fetch_local_health,
                self._health_url,
                timeout_s=0.2,
                agent_id=contract.agent_id,
                manifest=contract.manifest,
                expected_mapping_digest=contract.mapping_digest,
                verification_key=self._local_health_verification_key,
                expected_key_id=self._local_health_key_id,
                require_authenticated=self._local_health_verification_key is not None,
            )
            self._health_timer = self.create_timer(0.25, self._poll_agent_health)
        self._server.start()
        self.get_logger().info(
            f"read-only OPC UA Edge {contract.endpoint} projects "
            f"{len(contract.topic_specs)} ROS topics for {contract.manifest.robot.robot_id}"
        )
        if self._health_url is not None:
            self.get_logger().info(
                f"projecting authoritative Agent health from {self._health_url}"
            )

    def _poll_agent_health(self) -> None:
        if self._health_future is None or self._health_executor is None:
            return
        if not self._health_future.done():
            return
        try:
            snapshot = self._health_future.result()
            if self._local_health_replay_store is not None:
                self._local_health_replay_store.accept(snapshot)
            self.store.apply_authoritative_health(snapshot)
        except Exception as exc:
            now = time.monotonic()
            if now - self._last_health_error_log >= 5.0:
                self.get_logger().warning(
                    f"Agent health snapshot unavailable: {type(exc).__name__}: {exc}"
                )
                self._last_health_error_log = now
        self._health_future = self._health_executor.submit(
            fetch_local_health,
            self._health_url,
            timeout_s=0.2,
            agent_id=self.contract.agent_id,
            manifest=self.contract.manifest,
            expected_mapping_digest=self.contract.mapping_digest,
            verification_key=self._local_health_verification_key,
            expected_key_id=self._local_health_key_id,
            require_authenticated=self._local_health_verification_key is not None,
        )

    def _callback(self, kind: str, topic: str):
        if kind == "state":
            def receive_state(message: JointState) -> None:
                self.store.ingest_joint(
                    topic,
                    message.name,
                    message.position,
                    source_timestamp=_source_timestamp(message),
                )

            return receive_state
        if kind == "action":
            def receive_action(message: JointTrajectory) -> None:
                values = message.points[0].positions if len(message.points) == 1 else ()
                self.store.ingest_joint(
                    topic,
                    message.joint_names,
                    values,
                    source_timestamp=_source_timestamp(message),
                )

            return receive_action

        def receive_camera(message: CompressedImage) -> None:
            self.store.ingest_camera(
                topic,
                message.format,
                source_timestamp=_source_timestamp(message),
            )

        return receive_camera

    def _check_discovered_types(self) -> None:
        self._server.raise_if_failed()
        discovered = dict(self.get_topic_names_and_types())
        for topic, spec in self.contract.topic_specs.items():
            announced = discovered.get(topic)
            mismatched = announced is not None and spec.message_type not in announced
            self.store.set_type_mismatch(topic, mismatched)

    def destroy_node(self) -> None:
        self._graph_timer.cancel()
        if self._health_timer is not None:
            self._health_timer.cancel()
        if self._health_executor is not None:
            self._health_executor.shutdown(wait=True, cancel_futures=True)
        self._server.stop()
        super().destroy_node()


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SANG read-only ROS 2 to OPC UA Edge gateway"
    )
    parser.add_argument("--manifest", type=Path, default=Path("/ws/manifests/bi_so101_01.yaml"))
    parser.add_argument(
        "--mapping",
        type=Path,
        default=Path("/ws/contracts/opcua/bi_so101_01.mapping.v2.yaml"),
    )
    parser.add_argument(
        "--model-contract",
        type=Path,
        default=Path("/ws/contracts/opcua/model-contract.yaml"),
    )
    parser.add_argument(
        "--status-codes",
        type=Path,
        default=Path("/ws/contracts/opcua/status-code-mapping.yaml"),
    )
    parser.add_argument(
        "--nodeset",
        type=Path,
        default=Path("/ws/contracts/opcua/SANG.NodeSet2.xml"),
    )
    parser.add_argument("--agent-id")
    parser.add_argument("--endpoint", default="opc.tcp://127.0.0.1:4840/sang/")
    parser.add_argument("--preview-base-url")
    parser.add_argument("--publish-interval-s", type=float, default=0.05)
    parser.add_argument(
        "--security-mode",
        choices=("none", "sign-and-encrypt"),
        default="none",
    )
    parser.add_argument("--secret-root", type=Path)
    parser.add_argument("--server-certificate-ref")
    parser.add_argument("--server-private-key-ref")
    parser.add_argument("--client-trust-directory-ref")
    parser.add_argument("--client-crl-directory-ref")
    parser.add_argument(
        "--authorized-user-certificate-ref",
        action="append",
        default=[],
    )
    parser.add_argument("--local-health-key-ref")
    parser.add_argument("--local-health-key-id")
    parser.add_argument("--local-health-replay-db", type=Path)
    options = parser.parse_args(args)
    if not math.isfinite(options.publish_interval_s) or options.publish_interval_s <= 0.0:
        parser.error("publish-interval-s must be positive")
    security_values = (
        options.secret_root,
        options.server_certificate_ref,
        options.server_private_key_ref,
        options.client_trust_directory_ref,
        options.client_crl_directory_ref,
        *options.authorized_user_certificate_ref,
        options.local_health_key_ref,
        options.local_health_key_id,
        options.local_health_replay_db,
    )
    if options.security_mode == "none" and any(value is not None for value in security_values):
        parser.error("security material requires --security-mode sign-and-encrypt")
    return options


def main(args: list[str] | None = None) -> None:
    raw_args = sys.argv if args is None else [sys.argv[0], *args]
    cli_args = remove_ros_args(args=raw_args)[1:]
    options = parse_args(cli_args)
    security = None
    local_health_verification_key = None
    local_health_replay_store = None
    if options.security_mode == "sign-and-encrypt":
        required = {
            "secret-root": options.secret_root,
            "server-certificate-ref": options.server_certificate_ref,
            "server-private-key-ref": options.server_private_key_ref,
            "client-trust-directory-ref": options.client_trust_directory_ref,
            "client-crl-directory-ref": options.client_crl_directory_ref,
            "preview-base-url": options.preview_base_url,
            "local-health-key-ref": options.local_health_key_ref,
            "local-health-key-id": options.local_health_key_id,
            "local-health-replay-db": options.local_health_replay_db,
        }
        missing = [name for name, value in required.items() if value is None]
        if not options.authorized_user_certificate_ref:
            missing.append("authorized-user-certificate-ref")
        if missing:
            raise SystemExit(
                "secure OPC UA gateway is missing: " + ", ".join(missing)
            )
        try:
            resolver = SecretResolver(options.secret_root)
            security = OpcUaServerSecurity.from_secret_refs(
                resolver,
                certificate_ref=options.server_certificate_ref,
                private_key_ref=options.server_private_key_ref,
                trust_directory_ref=options.client_trust_directory_ref,
                crl_directory_ref=options.client_crl_directory_ref,
                authorized_user_certificate_refs=tuple(
                    options.authorized_user_certificate_ref
                ),
            )
            local_health_verification_key = load_local_health_key(
                resolver.resolve(options.local_health_key_ref, private=True)
            )
            local_health_replay_store = LocalHealthReplayStore(
                options.local_health_replay_db
            )
        except (
            LocalHealthError,
            SecretReferenceError,
            OpcUaSecurityError,
            OSError,
            ValueError,
        ) as exc:
            raise SystemExit(f"invalid OPC UA security material: {exc}") from exc
    try:
        contract = load_gateway_contract(
            manifest_path=options.manifest,
            mapping_path=options.mapping,
            model_contract_path=options.model_contract,
            status_codes_path=options.status_codes,
            nodeset_path=options.nodeset,
            endpoint=options.endpoint,
            agent_id=options.agent_id,
            preview_base_url=options.preview_base_url,
            allow_remote_endpoint=security is not None,
        )
    except ContractError as exc:
        raise SystemExit(f"invalid OPC UA gateway contract: {exc}") from exc

    rclpy.init(args=raw_args)
    node = None
    try:
        node = SangOpcUaGatewayNode(
            contract,
            publish_interval_s=options.publish_interval_s,
            security=security,
            local_health_verification_key=local_health_verification_key,
            local_health_key_id=options.local_health_key_id,
            local_health_replay_store=local_health_replay_store,
        )
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
