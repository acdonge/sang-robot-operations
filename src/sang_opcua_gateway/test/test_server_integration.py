import asyncio
from datetime import datetime, timezone
from pathlib import Path
import socket
import time

import pytest
from asyncua import Client, Server, ua
from asyncua.ua.uaerrors import UaStatusCodeError

from sang_opcua_gateway.contract import load_gateway_contract
from sang_opcua_gateway.server import OpcUaServerThread
from sang_opcua_gateway.state import GatewayStore
from sang_opcua_gateway.validate import (
    _audit_application_tree,
    _verify_model_browse_paths,
    _verify_sampling_profiles,
)


ROOT = Path(__file__).resolve().parents[3]
UTC = timezone.utc
LEFT_TOPIC = "/bi_so101/follower_left/joint_states_norm"
RIGHT_TOPIC = "/bi_so101/follower_right/joint_states_norm"
LEFT_NAMES = (
    "left_shoulder_pan",
    "left_shoulder_lift",
    "left_elbow_flex",
    "left_wrist_flex",
    "left_wrist_roll",
    "left_gripper",
)
RIGHT_NAMES = tuple(name.replace("left_", "right_") for name in LEFT_NAMES)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def contract(endpoint):
    return load_gateway_contract(
        manifest_path=ROOT / "manifests/bi_so101_01.yaml",
        mapping_path=ROOT / "contracts/opcua/bi_so101_01.mapping.v2.yaml",
        model_contract_path=ROOT / "contracts/opcua/model-contract.yaml",
        status_codes_path=ROOT / "contracts/opcua/status-code-mapping.yaml",
        nodeset_path=ROOT / "contracts/opcua/SANG.NodeSet2.xml",
        endpoint=endpoint,
        agent_id="workcell-soarm-01",
        preview_base_url="http://127.0.0.1:8101",
    )


async def inspect_server(gateway_contract, stamp, store):
    async with Client(url=gateway_contract.endpoint) as client:
        edge_ns = await client.get_namespace_index(gateway_contract.edge_namespace_uri)
        ids = gateway_contract.node_ids
        value_node = client.get_node(
            ua.NodeId(
                ids.feature_field(
                    "observation.state", "left_gripper", "value"
                ),
                edge_ns,
            )
        )
        value = await value_node.read_data_value(raise_on_bad_status=False)
        assert value.Value.Value == pytest.approx(42.0)
        assert value.StatusCode.value == gateway_contract.status_codes["Good"]
        assert value.SourceTimestamp == stamp
        assert await value_node.get_access_level() == {ua.AccessLevel.CurrentRead}
        assert await value_node.get_user_access_level() == {ua.AccessLevel.CurrentRead}

        with pytest.raises(UaStatusCodeError):
            await value_node.write_value(7.0, ua.VariantType.Float)

        assert not store.ingest_joint(
            LEFT_TOPIC,
            tuple(reversed(LEFT_NAMES)),
            (0.0,) * 6,
            source_timestamp=stamp,
        )
        await asyncio.sleep(0.1)
        invalid = await value_node.read_data_value(raise_on_bad_status=False)
        assert invalid.Value.Value is None
        assert invalid.StatusCode.value == gateway_contract.status_codes[
            "BadDecodingError"
        ]

        for field in ("rate_hz", "rate_ok"):
            health_node = client.get_node(
                ua.NodeId(
                    ids.topic_field("observation.state", RIGHT_TOPIC, field),
                    edge_ns,
                )
            )
            health = await health_node.read_data_value(raise_on_bad_status=False)
            assert health.Value.Value is None
            assert health.StatusCode.value == gateway_contract.status_codes[
                "BadWaitingForInitialData"
            ]

        action_node = client.get_node(
            ua.NodeId(
                ids.feature_field("action", "left_gripper", "value"),
                edge_ns,
            )
        )
        action = await action_node.read_data_value(raise_on_bad_status=False)
        assert action.Value.Value is None
        assert action.StatusCode.value == gateway_contract.status_codes[
            "BadWaitingForInitialData"
        ]

        robot = client.get_node(ua.NodeId(ids.robot, edge_ns))
        assert await robot.get_methods() == []
        type_definition = await robot.read_type_definition()
        model_ns = await client.get_namespace_index(gateway_contract.model_namespace_uri)
        assert type_definition == ua.NodeId(
            "type/SangRobotObservationType", model_ns
        )

        assert await _verify_model_browse_paths(
            client, gateway_contract, robot, edge_ns
        ) == 73
        audit = await _audit_application_tree(client, client.get_node(
            ua.NodeId(ids.agent, edge_ns)
        ))
        assert audit["node_count"] > 300
        assert audit["variable_count"] > 300
        assert audit["method_count"] == 0
        assert audit["writable_variables"] == []
        assert audit["byte_string_variables"] == []
        assert audit["frame_byte_nodes"] == []

        sampling = await _verify_sampling_profiles(
            client, gateway_contract, edge_ns
        )
        assert sampling["joint"]["revised_sampling_ms"] <= 100.0
        assert sampling["joint"]["minimum_sampling_interval_ms"] == 50.0
        assert sampling["camera_health"]["revised_publishing_ms"] <= 2000.0
        assert sampling["camera_health"]["minimum_sampling_interval_ms"] == 500.0
        assert sampling["robot_health"]["revised_publishing_ms"] <= 2000.0
        assert sampling["robot_health"]["minimum_sampling_interval_ms"] == 1000.0

        model = lambda name: ua.QualifiedName(name, model_ns)
        edge = lambda name: ua.QualifiedName(name, edge_ns)
        assert await (await robot.get_child(model("RealOrSim"))).read_value() == "real"
        sources = await robot.get_child(model("Sources"))
        state_source = await sources.get_child(edge("observation.state"))
        left_gripper = await state_source.get_child(edge("left_gripper"))
        assert await (await left_gripper.get_child(model("Unit"))).read_value() == (
            "lerobot_normalized"
        )

        frame_bytes = client.get_node(
            ua.NodeId(
                ids.source_field("top", "frame_bytes"),
                edge_ns,
            )
        )
        with pytest.raises(UaStatusCodeError):
            await frame_bytes.read_node_class()


async def inspect_byte_payload_detector(endpoint):
    server = Server()
    await server.init()
    server.set_endpoint(endpoint)
    namespace = await server.register_namespace("urn:sang:test:byte-payloads")
    root = await server.nodes.objects.add_object(
        ua.NodeId("audit", namespace),
        ua.QualifiedName("Audit", namespace),
    )
    byte_string = await root.add_variable(
        ua.NodeId("byte-string", namespace),
        ua.QualifiedName("ByteString", namespace),
        b"jpeg",
        ua.VariantType.ByteString,
    )
    image_jpg = await root.add_variable(
        ua.NodeId("image-jpg", namespace),
        ua.QualifiedName("ImageJPG", namespace),
        b"jpeg",
        ua.VariantType.ByteString,
    )
    await image_jpg.write_attribute(
        ua.AttributeIds.DataType,
        ua.DataValue(
            ua.Variant(
                ua.NodeId(ua.ObjectIds.ImageJPG),
                ua.VariantType.NodeId,
            )
        ),
    )
    byte_array = await root.add_variable(
        ua.NodeId("byte-array", namespace),
        ua.QualifiedName("ByteArray", namespace),
        ua.Variant([1, 2, 3], ua.VariantType.Byte),
    )
    await server.start()
    try:
        async with Client(url=endpoint) as client:
            audit = await _audit_application_tree(
                client, client.get_node(root.nodeid)
            )
    finally:
        await server.stop()
    return audit, {byte_string.nodeid, image_jpg.nodeid, byte_array.nodeid}


async def inspect_unavailable_health(gateway_contract):
    async with Client(url=gateway_contract.endpoint) as client:
        edge_ns = await client.get_namespace_index(gateway_contract.edge_namespace_uri)
        ids = gateway_contract.node_ids
        nodes = (
            ids.robot_field("fsm_state"),
            ids.source_field("observation.state", "fresh"),
            ids.topic_field("observation.state", LEFT_TOPIC, "rate_ok"),
        )
        for identifier in nodes:
            value = await client.get_node(
                ua.NodeId(identifier, edge_ns)
            ).read_data_value(raise_on_bad_status=False)
            assert value.Value.Value is None
            assert value.StatusCode.value == gateway_contract.status_codes[
                "BadNoCommunication"
            ]


def test_loopback_server_browse_read_timestamp_status_and_write_rejection():
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/sang/"
    gateway_contract = contract(endpoint)
    store = GatewayStore(gateway_contract, started_monotonic=time.monotonic())
    stamp = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    assert store.ingest_joint(
        LEFT_TOPIC,
        LEFT_NAMES,
        (0.0, 1.0, 2.0, 3.0, 4.0, 42.0),
        source_timestamp=stamp,
    )
    assert not store.ingest_joint(
        RIGHT_TOPIC,
        tuple(reversed(RIGHT_NAMES)),
        (0.0,) * 6,
        source_timestamp=stamp,
    )
    server = OpcUaServerThread(gateway_contract, store, publish_interval_s=0.02)
    server.start()
    try:
        asyncio.run(inspect_server(gateway_contract, stamp, store))
    finally:
        server.stop()


def test_server_thread_failure_is_reported_to_the_ros_lifecycle():
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/sang/"
    gateway_contract = contract(endpoint)
    server = OpcUaServerThread(gateway_contract, GatewayStore(gateway_contract))
    failure = RuntimeError("test failure")
    server._error = failure

    with pytest.raises(RuntimeError, match="server thread failed") as raised:
        server.raise_if_failed()

    assert raised.value.__cause__ is failure


def test_health_evaluator_unavailable_uses_bad_no_communication():
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/sang/"
    gateway_contract = contract(endpoint)
    store = GatewayStore(
        gateway_contract,
        authoritative_health_timeout_s=12.0,
    )
    server = OpcUaServerThread(gateway_contract, store, publish_interval_s=0.02)
    server.start()
    try:
        asyncio.run(inspect_unavailable_health(gateway_contract))
    finally:
        server.stop()


def test_recursive_audit_detects_byte_string_subtype_and_byte_array_payloads():
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/audit/"
    audit, expected = asyncio.run(inspect_byte_payload_detector(endpoint))

    assert set(audit["byte_payload_variables"]) == {
        node_id.to_string() for node_id in expected
    }
