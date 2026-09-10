import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import socket

import pytest
from asyncua import Client, Server, ua
from asyncua.ua.uaerrors import UaStatusCodeError

from sang_opcua_gateway.official_nodesets import (
    OfficialNodeSetError,
    import_official_nodesets,
    load_official_nodeset_bundle,
)
from sang_opcua_gateway.robotics_contract import load_robotics_profile_catalog
from sang_opcua_gateway.robotics_projection import (
    ProjectionValue,
    REFERENCE_REQUIRES,
    RoboticsAddressSpace,
    RoboticsProjectionError,
)


ROOT = Path(__file__).resolve().parents[3]
LOCK = ROOT / "contracts/opcua/nodesets.lock.yaml"
CATALOG = ROOT / "contracts/opcua/robotics-profiles.v1.yaml"
UTC = timezone.utc


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_official_release_bundle_is_pinned_and_catalog_agrees():
    bundle = load_official_nodeset_bundle(LOCK, catalog_path=CATALOG)

    assert bundle.release_tag == "Robotics-1.02-2025-09-08"
    assert bundle.import_order == ("di", "ia", "robotics")
    assert bundle.models["robotics"].version == "1.02"
    assert bundle.models["di"].version == "1.04.0"
    assert not bundle.models["core"].runtime_import


def test_official_bundle_detects_artifact_tampering(tmp_path):
    bundle = load_official_nodeset_bundle(LOCK)
    changed = tmp_path / "changed.xml"
    changed.write_bytes(bundle.models["robotics"].path.read_bytes() + b"\n")
    models = dict(bundle.models)
    models["robotics"] = replace(models["robotics"], path=changed)

    with pytest.raises(OfficialNodeSetError, match="digest changed"):
        replace(bundle, models=models).verify_files()


def test_partial_physical_profile_cannot_be_projected():
    profile = load_robotics_profile_catalog(CATALOG).for_robot("rb5_850e_01")
    server = Server()

    with pytest.raises(RoboticsProjectionError, match="is partial"):
        RoboticsAddressSpace(server, profile, {"di": 2, "ia": 3, "robotics": 4})


async def _browse_application_tree(node):
    result = {"methods": [], "placeholders": [], "variables": []}

    async def visit(current):
        for child in await current.get_children():
            node_class = await child.read_node_class()
            browse_name = await child.read_browse_name()
            if node_class == ua.NodeClass.Method:
                result["methods"].append(child.nodeid)
            if browse_name.Name.startswith("<"):
                result["placeholders"].append(child.nodeid)
            if node_class == ua.NodeClass.Variable:
                result["variables"].append(child)
            await visit(child)

    await visit(node)
    return result


async def _exercise_reference_projection():
    bundle = load_official_nodeset_bundle(LOCK, catalog_path=CATALOG)
    profile = load_robotics_profile_catalog(CATALOG).for_profile(
        "robotics-reference-fixture-v1"
    )
    endpoint = f"opc.tcp://127.0.0.1:{_free_port()}/robotics/"
    server = Server()
    await server.init()
    server.set_endpoint(endpoint)
    namespaces = await import_official_nodesets(server, bundle)
    projection = RoboticsAddressSpace(server, profile, namespaces)
    await projection.build()
    stamp = datetime(2026, 9, 1, 12, 30, tzinfo=UTC)
    await projection.publish_axis_values(
        {
            "axis-1": ProjectionValue(0.25, source_timestamp=stamp),
            "axis-2": ProjectionValue(
                -0.5,
                status_code=ua.StatusCodes.UncertainLastUsableValue,
                source_timestamp=stamp,
            ),
        }
    )
    await projection.publish_safety_values(
        "safety-state-fixture",
        emergency_stop=ProjectionValue(False, source_timestamp=stamp),
        operational_mode=ProjectionValue(3, source_timestamp=stamp),
        protective_stop=ProjectionValue(False, source_timestamp=stamp),
    )

    audit = await _browse_application_tree(projection.mds)
    assert audit["methods"] == []
    assert audit["placeholders"] == []
    for variable in audit["variables"]:
        assert await variable.get_access_level() == {ua.AccessLevel.CurrentRead}
        assert await variable.get_user_access_level() == {ua.AccessLevel.CurrentRead}

    assert await projection.mds.read_type_definition() == ua.NodeId(
        1002, namespaces["robotics"]
    )
    assert await projection.controllers["controller-robotics-fixture"].read_type_definition() == ua.NodeId(
        1003, namespaces["robotics"]
    )
    assert await projection.motion_devices["motion-device-fixture"].read_type_definition() == ua.NodeId(
        1004, namespaces["robotics"]
    )
    assert await projection.axes["axis-1"].read_type_definition() == ua.NodeId(
        16601, namespaces["robotics"]
    )
    assert await projection.power_trains["power-train-axis-1"].read_type_definition() == ua.NodeId(
        16794, namespaces["robotics"]
    )
    assert await projection.safety_states["safety-state-fixture"].read_type_definition() == ua.NodeId(
        1013, namespaces["robotics"]
    )
    required = await projection.axes["axis-1"].get_referenced_nodes(
        ua.NodeId(REFERENCE_REQUIRES, namespaces["robotics"]),
        direction=ua.BrowseDirection.Forward,
    )
    assert [node.nodeid for node in required] == [
        projection.power_trains["power-train-axis-1"].nodeid
    ]

    await server.start()
    try:
        async with Client(url=endpoint) as client:
            position = client.get_node(projection.axis_positions["axis-1"].nodeid)
            value = await position.read_data_value(raise_on_bad_status=False)
            assert value.Value.Value == pytest.approx(0.25)
            assert value.StatusCode.is_good()
            assert value.SourceTimestamp == stamp
            with pytest.raises(UaStatusCodeError):
                await position.write_value(1.0, ua.VariantType.Double)

            uncertain = await client.get_node(
                projection.axis_positions["axis-2"].nodeid
            ).read_data_value(raise_on_bad_status=False)
            assert uncertain.Value.Value == pytest.approx(-0.5)
            assert uncertain.StatusCode.value == ua.StatusCodes.UncertainLastUsableValue

            safety = client.get_node(
                projection.safety_values[("safety-state-fixture", "OperationalMode")].nodeid
            )
            assert await safety.read_value() == 3
    finally:
        await server.stop()


def test_official_reference_fixture_hierarchy_values_and_read_only_access():
    asyncio.run(_exercise_reference_projection())
