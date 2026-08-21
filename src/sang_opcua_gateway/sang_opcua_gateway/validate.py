"""Bounded live ROS/OPC UA correlation check for Phase 1 evidence."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import threading
import time

from asyncua import Client, ua
from asyncua.ua.uaerrors import UaStatusCodeError
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.utilities import remove_ros_args
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectory

from sang_security.opcua import (
    OpcUaClientSecurity,
    OpcUaSecurityError,
    configure_secure_client,
)
from sang_security.refs import SecretReferenceError, SecretResolver
from sang_opcua_gateway.contract import load_gateway_contract


UTC = timezone.utc
WRITE_REJECTION_CODES = {
    ua.StatusCodes.BadNotWritable,
    ua.StatusCodes.BadUserAccessDenied,
}


def _stamp(message) -> datetime | None:
    stamp = message.header.stamp
    if stamp.sec == 0 and stamp.nanosec == 0:
        return None
    try:
        return datetime.fromtimestamp(
            stamp.sec + stamp.nanosec / 1_000_000_000.0,
            UTC,
        )
    except (OSError, OverflowError, ValueError):
        return None


class JointHistory(Node):
    def __init__(self, contract) -> None:
        super().__init__("sang_opcua_live_validator")
        self._lock = threading.Lock()
        self._history = {
            topic.topic: deque(maxlen=512)
            for source in contract.sources
            for topic in source.topics
            if source.kind in {"state", "action"}
        }
        self._subscription_handles = []
        for source in contract.sources:
            if source.kind not in {"state", "action"}:
                continue
            message_type = JointState if source.kind == "state" else JointTrajectory
            for topic in source.topics:
                self._subscription_handles.append(
                    self.create_subscription(
                        message_type,
                        topic.topic,
                        self._callback(source.kind, topic.topic),
                        qos_profile_sensor_data,
                    )
                )

    def _callback(self, kind: str, topic: str):
        def receive(message) -> None:
            timestamp = _stamp(message)
            if timestamp is None:
                return
            if kind == "state":
                names = tuple(message.name)
                values = tuple(message.position)
            else:
                names = tuple(message.joint_names)
                values = (
                    tuple(message.points[0].positions)
                    if len(message.points) == 1
                    else ()
                )
            if len(names) != len(values) or len(names) != len(set(names)):
                return
            sample = (timestamp, dict(zip(names, values, strict=True)))
            with self._lock:
                self._history[topic].append(sample)

        return receive

    def find(self, topic: str, timestamp: datetime) -> dict[str, float] | None:
        with self._lock:
            for sample_timestamp, values in reversed(self._history[topic]):
                if sample_timestamp == timestamp:
                    return dict(values)
        return None

    def has_samples(self, topic: str) -> bool:
        with self._lock:
            return bool(self._history[topic])


async def _walk_hierarchy(root) -> list:
    pending = [root]
    seen = set()
    nodes = []
    while pending:
        node = pending.pop()
        if node.nodeid in seen:
            continue
        seen.add(node.nodeid)
        nodes.append(node)
        pending.extend(await node.get_children())
    return nodes


def _local_node_id(expanded) -> ua.NodeId | None:
    if getattr(expanded, "ServerIndex", 0) != 0 or getattr(
        expanded, "NamespaceUri", None
    ):
        return None
    return ua.NodeId(expanded.Identifier, expanded.NamespaceIndex)


async def _is_data_type_or_subtype(
    client: Client,
    data_type: ua.NodeId,
    base_type: ua.NodeId,
) -> bool:
    pending = [data_type]
    seen = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if current == base_type:
            return True
        references = await client.get_node(current).get_references(
            refs=ua.ObjectIds.HasSubtype,
            direction=ua.BrowseDirection.Inverse,
        )
        for reference in references:
            parent = _local_node_id(reference.NodeId)
            if parent is not None:
                pending.append(parent)
    return False


async def _audit_application_tree(client: Client, root) -> dict:
    nodes = await _walk_hierarchy(root)
    classes = await client.read_attributes(nodes, ua.AttributeIds.NodeClass)
    classified = [
        (node, data.Value.Value)
        for node, data in zip(nodes, classes, strict=True)
        if data.StatusCode.is_good()
    ]
    variables = [node for node, node_class in classified if node_class == ua.NodeClass.Variable]
    methods = [node for node, node_class in classified if node_class == ua.NodeClass.Method]
    access = await client.read_attributes(variables, ua.AttributeIds.AccessLevel)
    user_access = await client.read_attributes(variables, ua.AttributeIds.UserAccessLevel)
    data_types = await client.read_attributes(variables, ua.AttributeIds.DataType)
    value_ranks = await client.read_attributes(variables, ua.AttributeIds.ValueRank)
    browse_names = await client.read_attributes(nodes, ua.AttributeIds.BrowseName)
    writable = []
    byte_payloads = []
    byte_string = ua.NodeId(ua.ObjectIds.ByteString)
    byte = ua.NodeId(ua.ObjectIds.Byte)
    type_cache: dict[tuple[ua.NodeId, ua.NodeId], bool] = {}
    for node, access_value, user_value, data_type, value_rank in zip(
        variables, access, user_access, data_types, value_ranks, strict=True
    ):
        access_bits = ua.AccessLevel.parse_bitfield(access_value.Value.Value)
        user_bits = ua.AccessLevel.parse_bitfield(user_value.Value.Value)
        if ua.AccessLevel.CurrentWrite in access_bits or ua.AccessLevel.CurrentWrite in user_bits:
            writable.append(node.nodeid.to_string())
        data_type_id = data_type.Value.Value
        rank = value_rank.Value.Value
        byte_string_key = (data_type_id, byte_string)
        if byte_string_key not in type_cache:
            type_cache[byte_string_key] = await _is_data_type_or_subtype(
                client, data_type_id, byte_string
            )
        byte_key = (data_type_id, byte)
        if byte_key not in type_cache:
            type_cache[byte_key] = await _is_data_type_or_subtype(
                client, data_type_id, byte
            )
        if type_cache[byte_string_key] or (
            type_cache[byte_key] and rank != ua.ValueRank.Scalar
        ):
            byte_payloads.append(node.nodeid.to_string())
    frame_nodes = []
    for node, browse_name in zip(nodes, browse_names, strict=True):
        identifier = str(node.nodeid.Identifier).lower()
        name = str(getattr(browse_name.Value.Value, "Name", "")).lower()
        if "frame_bytes" in identifier or name == "framebytes":
            frame_nodes.append(node.nodeid.to_string())
    return {
        "node_count": len(nodes),
        "variable_count": len(variables),
        "method_count": len(methods),
        "writable_variables": writable,
        "byte_string_variables": byte_payloads,
        "byte_payload_variables": byte_payloads,
        "frame_byte_nodes": frame_nodes,
    }


async def _verify_model_browse_paths(client: Client, contract, robot, edge_ns: int) -> int:
    model_ns = await client.get_namespace_index(contract.model_namespace_uri)
    model = lambda name: ua.QualifiedName(name, model_ns)
    edge = lambda name: ua.QualifiedName(name, edge_ns)
    verified = 0
    for name in ("Model", "FsmState", "Sources", "Clock"):
        await robot.get_child(model(name))
        verified += 1
    sources = await robot.get_child(model("Sources"))
    clock = await robot.get_child(model("Clock"))
    await clock.get_child(model("Quality"))
    verified += 1
    for source in contract.sources:
        source_node = await sources.get_child(edge(source.key))
        for name in ("Required", "FreshnessMs", "Fresh", "RateOk", "Topics"):
            await source_node.get_child(model(name))
            verified += 1
        topics = await source_node.get_child(model("Topics"))
        for topic in source.topics:
            topic_node = await topics.get_child(edge(topic.topic))
            await topic_node.get_child(model("Topic"))
            verified += 1
            for feature in topic.features:
                feature_node = await source_node.get_child(edge(feature))
                await feature_node.get_child(model("Value"))
                verified += 1
    return verified


async def _verify_write_rejection(probe) -> tuple[bool, str | None]:
    if await probe.read_node_class() != ua.NodeClass.Variable:
        return False, "probe_is_not_a_variable"
    await probe.read_data_value(raise_on_bad_status=False)
    access = await probe.get_access_level()
    user_access = await probe.get_user_access_level()
    if ua.AccessLevel.CurrentWrite in access or ua.AccessLevel.CurrentWrite in user_access:
        return False, "probe_is_writable"
    try:
        await probe.write_value(0.0, ua.VariantType.Float)
    except UaStatusCodeError as exc:
        return exc.code in WRITE_REJECTION_CODES, f"0x{exc.code:08X}"
    return False, None


async def _measure_monitored_item(
    client: Client,
    node,
    *,
    requested_sampling_ms: float,
    requested_publishing_ms: float,
) -> dict:
    create = ua.CreateSubscriptionParameters()
    create.RequestedPublishingInterval = requested_publishing_ms
    create.RequestedLifetimeCount = 10000
    create.RequestedMaxKeepAliveCount = client.get_keepalive_count(
        requested_publishing_ms
    )
    create.MaxNotificationsPerPublish = 10000
    create.PublishingEnabled = True
    create.Priority = 0

    async def discard_notifications(_result) -> None:
        return None

    subscription = await client.uaclient.create_subscription(
        create, discard_notifications
    )
    try:
        item = ua.MonitoredItemCreateRequest()
        item.ItemToMonitor.NodeId = node.nodeid
        item.ItemToMonitor.AttributeId = ua.AttributeIds.Value
        item.MonitoringMode = ua.MonitoringMode.Reporting
        item.RequestedParameters.ClientHandle = 1
        item.RequestedParameters.SamplingInterval = requested_sampling_ms
        item.RequestedParameters.QueueSize = 1
        item.RequestedParameters.DiscardOldest = True
        parameters = ua.CreateMonitoredItemsParameters()
        parameters.SubscriptionId = subscription.SubscriptionId
        parameters.TimestampsToReturn = ua.TimestampsToReturn.Both
        parameters.ItemsToCreate = [item]
        result = (await client.uaclient.create_monitored_items(parameters))[0]
        result.StatusCode.check()
        minimum = await node.read_attribute(ua.AttributeIds.MinimumSamplingInterval)
        return {
            "requested_sampling_ms": requested_sampling_ms,
            "revised_sampling_ms": float(result.RevisedSamplingInterval),
            "requested_publishing_ms": requested_publishing_ms,
            "revised_publishing_ms": float(subscription.RevisedPublishingInterval),
            "revised_queue_size": int(result.RevisedQueueSize),
            "minimum_sampling_interval_ms": float(minimum.Value.Value),
        }
    finally:
        await client.uaclient.delete_subscriptions([subscription.SubscriptionId])


async def _verify_sampling_profiles(client: Client, contract, edge_ns: int) -> dict:
    ids = contract.node_ids
    joint = client.get_node(
        ua.NodeId(
            ids.feature_field(
                "observation.state", "left_shoulder_pan", "value"
            ),
            edge_ns,
        )
    )
    camera = client.get_node(
        ua.NodeId(ids.source_field("top", "fresh"), edge_ns)
    )
    robot = client.get_node(ua.NodeId(ids.robot_field("fsm_state"), edge_ns))
    return {
        "joint": await _measure_monitored_item(
            client,
            joint,
            requested_sampling_ms=50.0,
            requested_publishing_ms=100.0,
        ),
        "camera_health": await _measure_monitored_item(
            client,
            camera,
            requested_sampling_ms=500.0,
            requested_publishing_ms=1000.0,
        ),
        "robot_health": await _measure_monitored_item(
            client,
            robot,
            requested_sampling_ms=1000.0,
            requested_publishing_ms=1000.0,
        ),
    }


async def _find_history_sample(
    history: JointHistory,
    topic: str,
    timestamp: datetime,
) -> dict[str, float] | None:
    for _ in range(6):
        sample = history.find(topic, timestamp)
        if sample is not None:
            return sample
        await asyncio.sleep(0.01)
    return None


async def _validate(
    contract,
    history: JointHistory,
    duration_s: float,
    client_security: OpcUaClientSecurity | None = None,
) -> dict:
    started = datetime.now(UTC)
    matched: dict[str, set[str]] = {
        topic.topic: set()
        for source in contract.sources
        for topic in source.topics
        if source.kind in {"state", "action"}
    }
    unmatched_reads = {topic: 0 for topic in matched}
    matched_reads = {topic: 0 for topic in matched}
    expected_idle_reads = {topic: 0 for topic in matched}
    value_mismatches = []
    status_mismatches = []
    camera_fresh_seen = {
        source.key: False for source in contract.sources if source.kind == "camera"
    }
    write_rejected = False
    write_rejection_status = None
    model_browse_paths_verified = 0
    tree_audit = {}
    sampling_profiles = {}
    structural_failures = []

    client = Client(url=contract.endpoint)
    if client_security is not None:
        await configure_secure_client(client, client_security)
    async with client:
        edge_ns = await client.get_namespace_index(contract.edge_namespace_uri)
        ids = contract.node_ids
        agent = client.get_node(ua.NodeId(ids.agent, edge_ns))
        robot = client.get_node(ua.NodeId(ids.robot, edge_ns))
        tree_audit = await _audit_application_tree(client, agent)
        try:
            model_browse_paths_verified = await _verify_model_browse_paths(
                client, contract, robot, edge_ns
            )
        except UaStatusCodeError as exc:
            structural_failures.append(f"mandatory model BrowsePath failed: {exc}")
        probe = client.get_node(
            ua.NodeId(
                ids.feature_field(
                    "observation.state", "left_shoulder_pan", "value"
                ),
                edge_ns,
            )
        )
        write_rejected, write_rejection_status = await _verify_write_rejection(probe)
        sampling_profiles = await _verify_sampling_profiles(client, contract, edge_ns)

        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            for source in contract.sources:
                if source.kind not in {"state", "action"}:
                    continue
                for topic in source.topics:
                    nodes = []
                    for feature in topic.features:
                        nodes.extend(
                            (
                                client.get_node(
                                    ua.NodeId(
                                        ids.feature_field(source.key, feature, "value"),
                                        edge_ns,
                                    )
                                ),
                                client.get_node(
                                    ua.NodeId(
                                        ids.feature_field(
                                            source.key,
                                            feature,
                                            "timestamp_provenance",
                                        ),
                                        edge_ns,
                                    )
                                ),
                            )
                        )
                    values = await client.read_attributes(nodes, ua.AttributeIds.Value)
                    feature_values = values[::2]
                    provenances = values[1::2]
                    if all(value.StatusCode.is_bad() for value in feature_values):
                        status_values = {value.StatusCode.value for value in feature_values}
                        if not history.has_samples(topic.topic) and not source.required:
                            if status_values == {
                                contract.status_codes["BadWaitingForInitialData"]
                            }:
                                expected_idle_reads[topic.topic] += 1
                            else:
                                status_mismatches.append(
                                    f"{topic.topic}: idle status "
                                    + ",".join(f"0x{value:08X}" for value in status_values)
                                )
                        else:
                            status_mismatches.append(
                                f"{topic.topic}: all feature values have Bad status"
                            )
                        continue
                    for feature, value, provenance in zip(
                        topic.features, feature_values, provenances, strict=True
                    ):
                        if value.StatusCode.is_bad() or value.SourceTimestamp is None:
                            status_mismatches.append(
                                f"{topic.topic}/{feature}: Bad status or missing timestamp"
                            )
                            continue
                        if (
                            provenance.StatusCode.is_bad()
                            or provenance.SourceTimestamp != value.SourceTimestamp
                            or provenance.Value.Value
                            != contract.required_timestamp_provenance
                        ):
                            status_mismatches.append(
                                f"{topic.topic}/{feature}: invalid timestamp provenance"
                            )
                            continue
                        ros_values = await _find_history_sample(
                            history, topic.topic, value.SourceTimestamp
                        )
                        if ros_values is None:
                            unmatched_reads[topic.topic] += 1
                            continue
                        matched_reads[topic.topic] += 1
                        matched[topic.topic].add(value.SourceTimestamp.isoformat())
                        if not math.isclose(
                            float(value.Value.Value),
                            float(ros_values[feature]),
                            rel_tol=0.0,
                            abs_tol=1e-4,
                        ):
                            value_mismatches.append(
                                f"{topic.topic}/{feature}: OPC UA value differs from ROS"
                            )

            for source in contract.sources:
                if source.kind != "camera":
                    continue
                sequence = await client.get_node(
                    ua.NodeId(
                        ids.source_field(source.key, "last_sequence"),
                        edge_ns,
                    )
                ).read_data_value(raise_on_bad_status=False)
                fresh = await client.get_node(
                    ua.NodeId(
                        ids.source_field(source.key, "fresh"),
                        edge_ns,
                    )
                ).read_data_value(raise_on_bad_status=False)
                if (
                    sequence.StatusCode.is_good()
                    and fresh.StatusCode.is_good()
                    and sequence.Value.Value > 0
                    and fresh.Value.Value is True
                ):
                    camera_fresh_seen[source.key] = True
            await asyncio.sleep(0.1)

    failures = list(structural_failures)
    for source in contract.sources:
        if source.kind not in {"state", "action"}:
            continue
        for topic in source.topics:
            if history.has_samples(topic.topic):
                if len(matched[topic.topic]) < contract.minimum_matched_samples:
                    failures.append(f"no ROS/OPC UA timestamp match for {topic.topic}")
                total_reads = matched_reads[topic.topic] + unmatched_reads[topic.topic]
                unmatched_ratio = (
                    1.0 if total_reads == 0 else unmatched_reads[topic.topic] / total_reads
                )
                if unmatched_ratio > contract.maximum_unmatched_ratio:
                    failures.append(
                        f"unmatched read ratio {unmatched_ratio:.3%} exceeds "
                        f"{contract.maximum_unmatched_ratio:.3%} for {topic.topic}"
                    )
            elif source.required:
                failures.append(f"required ROS topic had no samples: {topic.topic}")
            elif expected_idle_reads[topic.topic] == 0:
                failures.append(f"idle optional topic status was not verified: {topic.topic}")
    if value_mismatches:
        failures.append(f"{len(value_mismatches)} value mismatch(es)")
    if status_mismatches:
        failures.append(f"{len(status_mismatches)} status/timestamp mismatch(es)")
    missing_cameras = [key for key, seen in camera_fresh_seen.items() if not seen]
    if missing_cameras:
        failures.append("camera health never became Good/Fresh: " + ", ".join(missing_cameras))
    if not write_rejected:
        failures.append("application Variable write was not rejected")
    if tree_audit.get("method_count"):
        failures.append("application tree exposes Method nodes")
    if tree_audit.get("writable_variables"):
        failures.append("application tree exposes writable Variables")
    if tree_audit.get("byte_string_variables") or tree_audit.get("frame_byte_nodes"):
        failures.append("application tree exposes camera/frame byte nodes")
    joint_profile = sampling_profiles.get("joint", {})
    health_profile = sampling_profiles.get("camera_health", {})
    robot_profile = sampling_profiles.get("robot_health", {})
    if joint_profile.get("revised_sampling_ms", math.inf) > 100.0:
        failures.append("joint monitored-item sampling was revised above 100 ms")
    if health_profile.get("revised_publishing_ms", math.inf) > 2000.0:
        failures.append("health subscription publishing was revised above 2000 ms")
    if joint_profile.get("minimum_sampling_interval_ms") != 50.0:
        failures.append("joint instance MinimumSamplingInterval is not 50 ms")
    if health_profile.get("minimum_sampling_interval_ms") != 500.0:
        failures.append("camera health MinimumSamplingInterval is not 500 ms")
    if robot_profile.get("revised_publishing_ms", math.inf) > 2000.0:
        failures.append("robot health subscription publishing was revised above 2000 ms")
    if robot_profile.get("minimum_sampling_interval_ms") != 1000.0:
        failures.append("robot health MinimumSamplingInterval is not 1000 ms")

    return {
        "schema_version": 1,
        "mode": "sang_opcua_phase1_live_correlation",
        "passed": not failures,
        "agent_id": contract.agent_id,
        "robot_id": contract.manifest.robot.robot_id,
        "endpoint": contract.endpoint,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "duration_s": duration_s,
        "matched_sample_counts": {
            topic: len(timestamps) for topic, timestamps in matched.items()
        },
        "matched_read_counts": matched_reads,
        "unmatched_read_counts": unmatched_reads,
        "maximum_unmatched_ratio": contract.maximum_unmatched_ratio,
        "minimum_matched_samples": contract.minimum_matched_samples,
        "expected_idle_read_counts": expected_idle_reads,
        "camera_fresh_seen": camera_fresh_seen,
        "value_mismatches": value_mismatches,
        "status_mismatches": status_mismatches,
        "write_rejected": write_rejected,
        "write_rejection_status": write_rejection_status,
        "model_browse_paths_verified": model_browse_paths_verified,
        "application_node_count": tree_audit.get("node_count", 0),
        "application_variable_count": tree_audit.get("variable_count", 0),
        "application_method_count": tree_audit.get("method_count", 0),
        "application_writable_variables": tree_audit.get("writable_variables", []),
        "application_byte_string_variables": tree_audit.get(
            "byte_string_variables", []
        ),
        "application_byte_payload_variables": tree_audit.get(
            "byte_payload_variables", []
        ),
        "camera_frame_bytes_absent": not (
            tree_audit.get("frame_byte_nodes", [])
            or tree_audit.get("byte_string_variables", [])
        ),
        "sampling_profiles": sampling_profiles,
        "failures": failures,
    }


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate live SO-101 ROS/OPC UA correlation")
    parser.add_argument("--manifest", type=Path, default=Path("/ws/manifests/bi_so101_01.yaml"))
    parser.add_argument("--mapping", type=Path, default=Path("/ws/contracts/opcua/bi_so101_01.mapping.v2.yaml"))
    parser.add_argument("--model-contract", type=Path, default=Path("/ws/contracts/opcua/model-contract.yaml"))
    parser.add_argument("--status-codes", type=Path, default=Path("/ws/contracts/opcua/status-code-mapping.yaml"))
    parser.add_argument("--nodeset", type=Path, default=Path("/ws/contracts/opcua/SANG.NodeSet2.xml"))
    parser.add_argument("--agent-id", default="workcell-soarm-01")
    parser.add_argument("--endpoint", default="opc.tcp://127.0.0.1:4840/sang/")
    parser.add_argument("--duration-s", type=float, default=10.0)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--security-mode",
        choices=("none", "sign-and-encrypt"),
        default="none",
    )
    parser.add_argument("--secret-root", type=Path)
    parser.add_argument("--application-uri", default="urn:sang:central:validator")
    parser.add_argument("--application-certificate-ref")
    parser.add_argument("--application-private-key-ref")
    parser.add_argument("--server-trust-directory-ref")
    parser.add_argument("--server-crl-directory-ref")
    parser.add_argument("--user-certificate-ref")
    parser.add_argument("--user-private-key-ref")
    parser.add_argument("--server-certificate-ref")
    options = parser.parse_args(args)
    if not math.isfinite(options.duration_s) or options.duration_s <= 0.0:
        parser.error("duration-s must be positive")
    security_values = (
        options.secret_root,
        options.application_certificate_ref,
        options.application_private_key_ref,
        options.server_trust_directory_ref,
        options.server_crl_directory_ref,
        options.user_certificate_ref,
        options.user_private_key_ref,
        options.server_certificate_ref,
    )
    if options.security_mode == "none" and any(
        value is not None for value in security_values
    ):
        parser.error("security material requires --security-mode sign-and-encrypt")
    if options.security_mode == "sign-and-encrypt" and any(
        value is None for value in security_values[:-1]
    ):
        parser.error("sign-and-encrypt requires all security references except server pin")
    return options


def main(args: list[str] | None = None) -> None:
    raw_args = sys.argv if args is None else [sys.argv[0], *args]
    options = parse_args(remove_ros_args(args=raw_args)[1:])
    client_security = None
    if options.security_mode == "sign-and-encrypt":
        try:
            client_security = OpcUaClientSecurity.from_secret_refs(
                SecretResolver(options.secret_root),
                application_uri=options.application_uri,
                expected_server_application_uri=f"urn:sang:agent:{options.agent_id}",
                application_certificate_ref=options.application_certificate_ref,
                application_private_key_ref=options.application_private_key_ref,
                trust_directory_ref=options.server_trust_directory_ref,
                crl_directory_ref=options.server_crl_directory_ref,
                user_certificate_ref=options.user_certificate_ref,
                user_private_key_ref=options.user_private_key_ref,
                server_certificate_ref=options.server_certificate_ref,
            )
        except (SecretReferenceError, OpcUaSecurityError, OSError, ValueError) as exc:
            raise SystemExit(f"invalid OPC UA client security material: {exc}") from exc
    contract = load_gateway_contract(
        manifest_path=options.manifest,
        mapping_path=options.mapping,
        model_contract_path=options.model_contract,
        status_codes_path=options.status_codes,
        nodeset_path=options.nodeset,
        endpoint=options.endpoint,
        agent_id=options.agent_id,
        allow_remote_endpoint=client_security is not None,
    )
    rclpy.init(args=raw_args)
    history = JointHistory(contract)
    spin_thread = threading.Thread(
        target=rclpy.spin,
        args=(history,),
        name="opcua-validation-ros-spin",
        daemon=True,
    )
    spin_thread.start()
    try:
        time.sleep(1.0)
        report = asyncio.run(
            _validate(
                contract,
                history,
                options.duration_s,
                client_security=client_security,
            )
        )
    finally:
        if rclpy.ok():
            rclpy.shutdown()
        spin_thread.join(timeout=5.0)
        history.destroy_node()
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    options.report.parent.mkdir(parents=True, exist_ok=True)
    options.report.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
