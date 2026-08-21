from pathlib import Path

import pytest
import yaml

from robot_agent.local_health import load_mapping_health_digest
from sang_opcua_gateway.contract import (
    ContractError,
    encode_component,
    load_gateway_contract,
    require_loopback_endpoint,
)


ROOT = Path(__file__).resolve().parents[3]


def load_contract(**overrides):
    values = {
        "manifest_path": ROOT / "manifests/bi_so101_01.yaml",
        "mapping_path": ROOT / "contracts/opcua/bi_so101_01.mapping.v2.yaml",
        "model_contract_path": ROOT / "contracts/opcua/model-contract.yaml",
        "status_codes_path": ROOT / "contracts/opcua/status-code-mapping.yaml",
        "nodeset_path": ROOT / "contracts/opcua/SANG.NodeSet2.xml",
        "endpoint": "opc.tcp://127.0.0.1:4840/sang/",
        "agent_id": "workcell-soarm-01",
        "preview_base_url": "http://127.0.0.1:8101",
    }
    values.update(overrides)
    return load_gateway_contract(**values)


def test_contract_covers_so101_sources_features_and_cameras():
    contract = load_contract()

    assert len(contract.sources) == 7
    assert len(contract.topic_specs) == 9
    assert sum(len(topic.features) for topic in contract.topic_specs.values()) == 24
    assert sum(source.kind == "camera" for source in contract.sources) == 5
    assert contract.application_uri == "urn:sang:agent:workcell-soarm-01"
    assert contract.mapping_schema_version == "2.0.0"
    assert len(contract.mapping_digest) == 64
    assert contract.mapping_digest == load_mapping_health_digest(
        ROOT / "contracts/opcua/bi_so101_01.mapping.v2.yaml",
        expected_robot_id="bi_so101_01",
    )
    gripper = contract.feature_spec("observation.state", "left_gripper")
    assert (gripper.minimum, gripper.maximum) == (0.0, 100.0)
    assert gripper.engineering_unit == "lerobot_normalized"
    assert contract.stream_uri("right_wrist").endswith(
        "/robots/bi_so101_01/cameras/right_wrist.mjpg"
    )


def test_node_id_encoding_is_canonical_and_deterministic():
    assert encode_component("camera/top") == "camera%2Ftop"
    assert encode_component("left wrist") == "left%20wrist"
    assert encode_component("100%") == "100%25"
    assert encode_component("손목") == "%EC%86%90%EB%AA%A9"

    ids = load_contract().node_ids
    assert ids.topic("top", "/camera/top") == (
        "agent/workcell-soarm-01/robot/bi_so101_01/source/top/topic/%2Fcamera%2Ftop"
    )


@pytest.mark.parametrize(
    "endpoint",
    (
        "opc.tcp://0.0.0.0:4840/sang/",
        "opc.tcp://192.168.0.10:4840/sang/",
        "http://127.0.0.1:4840/sang/",
        "opc.tcp://127.0.0.1:0/sang/",
    ),
)
def test_non_loopback_or_invalid_endpoint_is_rejected(endpoint):
    with pytest.raises(ContractError):
        require_loopback_endpoint(endpoint)


def test_remote_endpoint_requires_explicit_secure_contract_mode():
    endpoint = "opc.tcp://soarm-edge.sang.local:4840/sang/"
    with pytest.raises(ContractError):
        load_contract(endpoint=endpoint)
    assert load_contract(
        endpoint=endpoint,
        allow_remote_endpoint=True,
    ).endpoint == endpoint


def test_contract_rejects_non_loopback_preview():
    with pytest.raises(ContractError, match="preview"):
        load_contract(preview_base_url="http://192.168.0.10:8101")


def test_contract_enforces_declared_coverage(tmp_path):
    mapping_path = ROOT / "contracts/opcua/bi_so101_01.mapping.v2.yaml"
    mapping = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))
    mapping["expected_coverage"]["topics"] += 1
    invalid_mapping = tmp_path / "mapping.yaml"
    invalid_mapping.write_text(
        yaml.safe_dump(mapping, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ContractError, match="expected_coverage"):
        load_contract(mapping_path=invalid_mapping)


def test_contract_rejects_unknown_mapping_schema_version(tmp_path):
    mapping_path = ROOT / "contracts/opcua/bi_so101_01.mapping.v2.yaml"
    mapping = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))
    mapping["schema_version"] = "9.0.0"
    invalid_mapping = tmp_path / "mapping.yaml"
    invalid_mapping.write_text(
        yaml.safe_dump(mapping, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ContractError, match="mapping schema_version"):
        load_contract(mapping_path=invalid_mapping)


def test_mapping_v1_remains_a_read_only_rollback_contract():
    v1_contract = load_contract(
        mapping_path=ROOT / "contracts/opcua/bi_so101_01.mapping.yaml"
    )
    v2_contract = load_contract()

    assert v1_contract.mapping_schema_version == "1.0.0"
    assert v1_contract.mapping_digest is None
    assert v1_contract.node_ids == v2_contract.node_ids
    assert tuple(v1_contract.topic_specs) == tuple(v2_contract.topic_specs)
    assert v1_contract.feature_spec(
        "observation.state", "left_gripper"
    ).maximum == 100.0


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        (lambda feature: feature.update(valid_range=[10.0, -10.0]), "valid_range"),
        (lambda feature: feature.update(engineering_unit="unknown"), "engineering unit"),
        (lambda feature: feature.update(order=99), "order is invalid"),
        (
            lambda feature: feature.update(
                selector={"type": "joint_name", "value": "wrong_joint"}
            ),
            "selector is invalid",
        ),
        (lambda feature: feature.update(opcua_data_type="Double"), "data type"),
    ),
)
def test_mapping_v2_rejects_invalid_feature_metadata(tmp_path, mutation, error):
    mapping_path = ROOT / "contracts/opcua/bi_so101_01.mapping.v2.yaml"
    mapping = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))
    mutation(mapping["sources"][0]["topics"][0]["features"][0])
    invalid_mapping = tmp_path / "mapping.yaml"
    invalid_mapping.write_text(
        yaml.safe_dump(mapping, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match=error):
        load_contract(mapping_path=invalid_mapping)


def test_mapping_v2_rejects_unknown_feature_contract_keys(tmp_path):
    mapping_path = ROOT / "contracts/opcua/bi_so101_01.mapping.v2.yaml"
    mapping = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))
    mapping["feature_contract"]["future_unreviewed_option"] = True
    invalid_mapping = tmp_path / "mapping.yaml"
    invalid_mapping.write_text(
        yaml.safe_dump(mapping, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="feature_contract keys"):
        load_contract(mapping_path=invalid_mapping)
