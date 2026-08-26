import asyncio
from datetime import datetime, timezone
import socket

from asyncua import Client, ua
import pytest

from fleet_control_plane.central_opcua import (
    NAMESPACE_URI,
    parse_args,
    serve,
)
from fleet_control_plane.models import AgentHeartbeatV1
from fleet_control_plane.operations_models import (
    AssetCreate,
    ObservedBinding,
    ProcessingNodeCreate,
)
from fleet_control_plane.status import FleetTTLs
from fleet_control_plane.store import FleetStore


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def heartbeat(*, fsm_state="READY", fault=None):
    now = datetime.now(timezone.utc)
    topic = {
        "topic": "/robot/joint_states",
        "age_ms": 10.0,
        "rate_hz": 20.0,
        "minimum_rate_hz": 10.0,
        "rate_ok": True,
        "seq": 1,
        "fresh": True,
    }
    return AgentHeartbeatV1.model_validate(
        {
            "schema_version": 1,
            "agent_id": "agent-central-test",
            "agent_name": "Central Test Agent",
            "sw_version": "0.1.0",
            "sent_at": now.isoformat(),
            "storage": {"disk_free_gb": 10, "ram_free_gb": 5, "cpu_pct": 10},
            "datasets": {"count": 0, "total_episodes": 0, "total_size_mb": 0},
            "robots": [
                {
                    "robot_id": "robot-central-test",
                    "model": "test_arm",
                    "fsm_state": fsm_state,
                    "phase": "monitoring",
                    "sources_fresh": True,
                    "rates_ok": True,
                    "sources": [
                        {
                            "key": "observation.state",
                            "kind": "state",
                            "required": True,
                            "freshness_ms": 1000,
                            "fresh": True,
                            "rate_ok": True,
                            "topics": [topic],
                        }
                    ],
                    "fault": fault,
                    "recording": {
                        "active": False,
                        "episode_index": 0,
                        "episodes_total": 0,
                        "frames": 0,
                        "elapsed_s": 0,
                        "current_instruction": None,
                    },
                }
            ],
        }
    )


async def walk(root):
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


def test_central_opcua_projects_updates_and_has_no_write_surface(tmp_path):
    asyncio.run(_exercise_central_opcua(tmp_path))


async def _exercise_central_opcua(tmp_path):
    store = FleetStore(tmp_path / "fleet.db")
    received = datetime.now(timezone.utc)
    store.upsert(heartbeat(), received_at=received)
    store.create_processing_node(
        ProcessingNodeCreate(
            node_id="node-central-test",
            display_name="Central Test Node",
            agent_ids=["agent-central-test"],
        ),
        occurred_at=received,
    )
    store.create_asset(
        AssetCreate(
            asset_id="asset-central-test",
            asset_type="robot",
            display_name="Central Test Robot",
            node_id="node-central-test",
            binding=ObservedBinding(
                agent_id="agent-central-test",
                robot_id="robot-central-test",
                source_key=None,
            ),
            capabilities=["telemetry"],
        ),
        occurred_at=received,
    )
    port = free_port()
    endpoint = f"opc.tcp://127.0.0.1:{port}/sang/central/"
    stop = asyncio.Event()
    ready = asyncio.Event()
    task = asyncio.create_task(
        serve(
            store=store,
            endpoint=endpoint,
            publish_interval_s=0.05,
            ttls=FleetTTLs(online_s=12, offline_s=30),
            security=None,
            stop=stop,
            ready=ready,
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        async with Client(endpoint, timeout=5) as client:
            namespace = await client.get_namespace_index(NAMESPACE_URI)
            agent_count = client.get_node(
                ua.NodeId("central/fleet/summary/fleet/field/agent_count", namespace)
            )
            fsm_state = client.get_node(
                ua.NodeId(
                    "central/fleet/robots/agent-central-test--robot-central-test/field/fsm_state",
                    namespace,
                )
            )
            assert await agent_count.read_value() == 1
            assert await fsm_state.read_value() == "READY"

            store.upsert(
                heartbeat(fsm_state="DEGRADED", fault="observation delayed"),
                received_at=datetime.now(timezone.utc),
            )
            for _ in range(30):
                if await fsm_state.read_value() == "DEGRADED":
                    break
                await asyncio.sleep(0.05)
            assert await fsm_state.read_value() == "DEGRADED"

            root = client.get_node(ua.NodeId("central/fleet", namespace))
            nodes = await walk(root)
            classes = await client.read_attributes(nodes, ua.AttributeIds.NodeClass)
            variables = [
                node
                for node, node_class in zip(nodes, classes, strict=True)
                if node_class.Value.Value == ua.NodeClass.Variable
            ]
            methods = [
                node
                for node, node_class in zip(nodes, classes, strict=True)
                if node_class.Value.Value == ua.NodeClass.Method
            ]
            assert methods == []
            for node in variables:
                assert ua.AccessLevel.CurrentWrite not in await node.get_access_level()
                assert ua.AccessLevel.CurrentWrite not in await node.get_user_access_level()
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


def test_central_opcua_cli_requires_security_for_remote_endpoint():
    with pytest.raises(SystemExit):
        parse_args(["--endpoint", "opc.tcp://192.0.2.10:4850/sang/central/"])

    options = parse_args(
        [
            "--endpoint",
            "opc.tcp://192.0.2.10:4850/sang/central/",
            "--security-mode",
            "sign-and-encrypt",
            "--secret-root",
            "/tmp/secrets",
            "--server-certificate-ref",
            "secret://opcua/server/cert.pem",
            "--server-private-key-ref",
            "secret://opcua/server/key.pem",
            "--client-trust-directory-ref",
            "secret://opcua/trust/clients",
            "--client-crl-directory-ref",
            "secret://opcua/crl",
            "--authorized-user-certificate-ref",
            "secret://opcua/users/reader.pem",
        ]
    )
    assert options.security_mode == "sign-and-encrypt"
