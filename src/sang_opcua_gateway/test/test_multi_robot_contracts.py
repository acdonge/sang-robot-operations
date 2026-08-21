import math
from pathlib import Path

import pytest

from robot_agent.local_health import load_mapping_health_digest
from sang_opcua_gateway.contract import GatewayContract, load_gateway_contract


ROOT = Path(__file__).resolve().parents[3]


def load_robot_contract(robot_id: str) -> GatewayContract:
    mapping_path = ROOT / f"contracts/opcua/{robot_id}.mapping.v2.yaml"
    contract = load_gateway_contract(
        manifest_path=ROOT / f"manifests/{robot_id}.yaml",
        mapping_path=mapping_path,
        model_contract_path=ROOT / "contracts/opcua/model-contract.yaml",
        status_codes_path=ROOT / "contracts/opcua/status-code-mapping.yaml",
        nodeset_path=ROOT / "contracts/opcua/SANG.NodeSet2.xml",
        endpoint="opc.tcp://127.0.0.1:4840/sang/",
        agent_id=f"validate-{robot_id}",
        preview_base_url="http://127.0.0.1:8101",
    )
    assert contract.mapping_digest == load_mapping_health_digest(
        mapping_path,
        expected_robot_id=robot_id,
    )
    return contract


@pytest.mark.parametrize(
    ("robot_id", "source_count", "topic_count", "feature_count", "camera_count"),
    (
        ("omy_01", 4, 4, 14, 2),
        ("omy_sim_01", 3, 3, 7, 2),
        ("rb5_850e_01", 2, 2, 6, 1),
        ("rb5_850e_sim_01", 3, 3, 6, 2),
    ),
)
def test_multi_robot_mapping_v2_covers_manifest_exactly(
    robot_id,
    source_count,
    topic_count,
    feature_count,
    camera_count,
):
    contract = load_robot_contract(robot_id)

    assert contract.manifest.robot.robot_id == robot_id
    assert contract.mapping_schema_version == "2.0.0"
    assert len(contract.sources) == source_count
    assert len(contract.topic_specs) == topic_count
    assert sum(
        len(topic.feature_specs) for topic in contract.topic_specs.values()
    ) == feature_count
    assert sum(source.kind == "camera" for source in contract.sources) == camera_count
    assert all(
        feature.engineering_unit == "rad"
        and feature.opcua_data_type == "Float"
        for topic in contract.topic_specs.values()
        for feature in topic.feature_specs
    )


def test_omy_mapping_uses_robotis_ranges_for_real_and_sim():
    contracts = (load_robot_contract("omy_01"), load_robot_contract("omy_sim_01"))

    for contract in contracts:
        state = contract.source_specs["observation.state"]
        assert tuple(feature.name for feature in state.topics[0].feature_specs) == (
            "joint1",
            "joint2",
            "joint3",
            "joint4",
            "joint5",
            "joint6",
            "rh_r1_joint",
        )
        for name in ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6"):
            feature = contract.feature_spec("observation.state", name)
            assert (feature.minimum, feature.maximum) == (-2 * math.pi, 2 * math.pi)
        gripper = contract.feature_spec("observation.state", "rh_r1_joint")
        assert (gripper.minimum, gripper.maximum) == (0.0, 1.13514578304)

    physical = contracts[0]
    for name in ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6"):
        state = physical.feature_spec("observation.state", name)
        action = physical.feature_spec("action", name)
        assert (action.minimum, action.maximum) == (state.minimum, state.maximum)
    action_gripper = physical.feature_spec("action", "rh_r1_joint")
    assert (action_gripper.minimum, action_gripper.maximum) == (0.0, 1.13514578304)


def test_rb5_mapping_uses_vendor_ranges_for_real_and_sim():
    for robot_id in ("rb5_850e_01", "rb5_850e_sim_01"):
        contract = load_robot_contract(robot_id)
        for name in ("base", "shoulder", "wrist1", "wrist2", "wrist3"):
            feature = contract.feature_spec("observation.state", name)
            assert (feature.minimum, feature.maximum) == (-2 * math.pi, 2 * math.pi)
        elbow = contract.feature_spec("observation.state", "elbow")
        elbow_limit = math.radians(165)
        assert (elbow.minimum, elbow.maximum) == (-elbow_limit, elbow_limit)


@pytest.mark.parametrize(
    ("robot_id", "configured_fps", "minimum_rate_hz"),
    (
        ("omy_01", 10.0, 8.0),
        ("omy_sim_01", 30.0, 25.0),
        ("rb5_850e_01", 10.0, 8.0),
        ("rb5_850e_sim_01", 30.0, 25.0),
    ),
)
def test_camera_contracts_pin_runtime_profiles(
    robot_id,
    configured_fps,
    minimum_rate_hz,
):
    cameras = [
        source
        for source in load_robot_contract(robot_id).sources
        if source.kind == "camera"
    ]

    assert cameras
    assert all(camera.configured_fps == configured_fps for camera in cameras)
    assert all(camera.minimum_rate_hz == minimum_rate_hz for camera in cameras)
    assert all(camera.encoding == "jpeg" for camera in cameras)
