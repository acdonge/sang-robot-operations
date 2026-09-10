from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from fleet_control_plane.asset_contracts import (
    SCHEMA_MODELS,
    asset_contract_schemas,
    export_schemas,
)
from fleet_control_plane.asset_models import (
    AssetPackageV1,
    GlobalAssetIdentityV1,
    SceneBindingManifestV1,
    SimulationQualificationV1,
    UnitySceneInventoryV1,
    validate_asset_contract_bundle,
)
from fleet_control_plane.unity_asset_inventory import (
    UnityInventoryMismatch,
    validate_unity_inventory,
)


ROOT = Path(__file__).parents[3]
CONTRACT_DIR = ROOT / "contracts" / "assets"
EXAMPLES = CONTRACT_DIR / "examples"


def _payload(name: str) -> dict[str, object]:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def test_committed_a0_examples_are_valid_and_keep_twins_distinct():
    physical = GlobalAssetIdentityV1.model_validate(
        _payload("omy-physical-identity.v1.json")
    )
    simulation = GlobalAssetIdentityV1.model_validate(
        _payload("omy-simulation-identity.v1.json")
    )
    package = AssetPackageV1.model_validate(_payload("omy-unity-package.v1.json"))
    scene = SceneBindingManifestV1.model_validate(
        _payload("iitp-scenario1-scene-binding.v1.json")
    )
    qualification = SimulationQualificationV1.model_validate(
        _payload("iitp-scenario1-qualification.v1.json")
    )

    assert physical.asset_id != simulation.asset_id
    assert physical.global_asset_id != simulation.global_asset_id
    assert physical.aas_id != simulation.aas_id
    assert physical.runtime_class == "physical"
    assert simulation.runtime_class == "simulation"
    assert simulation.relationships[0].target_global_asset_id == physical.global_asset_id
    assert package.runtime.kind == "UNITY"
    assert scene.bindings[0].identity.global_asset_id == simulation.global_asset_id
    assert scene.output_isolation.physical_output_connected is False
    assert qualification.physical_output_connected is False
    assert qualification.verdict == "PASS"


def test_global_identity_rejects_aliases_self_links_and_physical_twin_direction():
    payload = _payload("omy-simulation-identity.v1.json")
    payload["aas_id"] = payload["global_asset_id"]
    with pytest.raises(ValidationError, match="distinct meanings"):
        GlobalAssetIdentityV1.model_validate(payload)

    payload = _payload("omy-simulation-identity.v1.json")
    payload["relationships"][0]["target_global_asset_id"] = payload[
        "global_asset_id"
    ]
    with pytest.raises(ValidationError, match="same global identity"):
        GlobalAssetIdentityV1.model_validate(payload)

    payload = _payload("omy-simulation-identity.v1.json")
    payload["runtime_class"] = "physical"
    with pytest.raises(ValidationError, match="simulation asset"):
        GlobalAssetIdentityV1.model_validate(payload)


def test_asset_package_rejects_digest_paths_and_missing_runtime_artifact():
    payload = _payload("omy-unity-package.v1.json")
    payload["artifacts"][0]["sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="object_id must equal"):
        AssetPackageV1.model_validate(payload)

    payload = _payload("omy-unity-package.v1.json")
    payload["artifacts"][0]["file_name"] = "/tmp/untrusted.prefab"
    with pytest.raises(ValidationError):
        AssetPackageV1.model_validate(payload)

    payload = _payload("omy-unity-package.v1.json")
    payload["artifacts"] = [payload["artifacts"][1]]
    with pytest.raises(ValidationError, match="UNITY package requires"):
        AssetPackageV1.model_validate(payload)


def test_scene_binding_rejects_physical_duplicate_and_mismatched_assets():
    payload = _payload("iitp-scenario1-scene-binding.v1.json")
    payload["bindings"][0]["identity"]["runtime_class"] = "physical"
    with pytest.raises(ValidationError, match="simulation assets only"):
        SceneBindingManifestV1.model_validate(payload)

    payload = _payload("iitp-scenario1-scene-binding.v1.json")
    payload["bindings"].append(deepcopy(payload["bindings"][0]))
    with pytest.raises(ValidationError, match="scene asset IDs must be unique"):
        SceneBindingManifestV1.model_validate(payload)

    payload = _payload("iitp-scenario1-scene-binding.v1.json")
    payload["bindings"][0]["package_ref"]["sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="package digest does not match"):
        SceneBindingManifestV1.model_validate(payload)

    payload = _payload("iitp-scenario1-scene-binding.v1.json")
    payload["bindings"][0]["signals"] = []
    with pytest.raises(ValidationError, match="ready, success, failure, and fault"):
        SceneBindingManifestV1.model_validate(payload)


def test_scene_contract_cannot_enable_a_physical_output():
    payload = _payload("iitp-scenario1-scene-binding.v1.json")
    payload["output_isolation"]["physical_output_connected"] = True
    with pytest.raises(ValidationError):
        SceneBindingManifestV1.model_validate(payload)


def test_runtime_unity_inventory_matches_exact_scene_binding_contract():
    scene = SceneBindingManifestV1.model_validate(
        _payload("iitp-scenario1-scene-binding.v1.json")
    )
    binding = scene.bindings[0]
    inventory = UnitySceneInventoryV1.model_validate(
        {
            "schema_version": 1,
            "scene_id": scene.scene_id,
            "scene_version": scene.version,
            "unity_version": scene.unity_version,
            "time_source": "unity_simulation_clock",
            "physical_output_connected": False,
            "bindings": [
                {
                    "identity": binding.identity.model_dump(mode="json"),
                    "asset_type": binding.asset_type,
                    "package_ref": binding.package_ref.model_dump(mode="json"),
                    "unity_binding_id": binding.unity_binding_id,
                    "hierarchy_path": binding.diagnostic_hierarchy_path,
                    "articulation_root_binding_id": binding.articulation.root_binding_id,
                    "ordered_joints": binding.articulation.ordered_joints,
                    "controller_component_binding_id": binding.controller.component_binding_id,
                    "frames": [
                        {"semantic": semantic, "frame_id": frame_id, "present": True}
                        for semantic, frame_id in binding.frames.items()
                    ],
                    "sensors": [],
                }
            ],
        }
    )

    validate_unity_inventory(inventory, scene)

    changed = inventory.model_copy(deep=True)
    changed.bindings[0].package_ref.sha256 = "0" * 64
    with pytest.raises(UnityInventoryMismatch, match="package digest"):
        validate_unity_inventory(changed, scene)

    missing_frame = inventory.model_copy(deep=True)
    missing_frame.bindings[0].frames[0].present = False
    with pytest.raises(UnityInventoryMismatch, match="absent or mismatched"):
        validate_unity_inventory(missing_frame, scene)


def test_qualification_requires_exact_actions_evidence_and_verdict():
    payload = _payload("iitp-scenario1-qualification.v1.json")
    payload["actions"][0]["installed_source_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="must match publication"):
        SimulationQualificationV1.model_validate(payload)

    payload = _payload("iitp-scenario1-qualification.v1.json")
    payload["checks"][0]["state"] = "FAIL"
    with pytest.raises(ValidationError, match="every check to pass"):
        SimulationQualificationV1.model_validate(payload)

    payload = _payload("iitp-scenario1-qualification.v1.json")
    payload["verdict"] = "FAIL"
    with pytest.raises(ValidationError, match="at least one failed check"):
        SimulationQualificationV1.model_validate(payload)

    payload = _payload("iitp-scenario1-qualification.v1.json")
    payload["physical_output_connected"] = True
    with pytest.raises(ValidationError):
        SimulationQualificationV1.model_validate(payload)


def test_bundle_cross_validation_pins_identity_package_scene_and_attestations():
    identities = [
        GlobalAssetIdentityV1.model_validate(
            _payload("omy-physical-identity.v1.json")
        ),
        GlobalAssetIdentityV1.model_validate(
            _payload("omy-simulation-identity.v1.json")
        ),
    ]
    packages = [AssetPackageV1.model_validate(_payload("omy-unity-package.v1.json"))]
    scene = SceneBindingManifestV1.model_validate(
        _payload("iitp-scenario1-scene-binding.v1.json")
    )
    qualification = SimulationQualificationV1.model_validate(
        _payload("iitp-scenario1-qualification.v1.json")
    )
    validate_asset_contract_bundle(identities, packages, scene, qualification)

    payload = _payload("iitp-scenario1-scene-binding.v1.json")
    payload["required_packages"][0]["sha256"] = "0" * 64
    payload["bindings"][0]["package_ref"]["sha256"] = "0" * 64
    mismatched_scene = SceneBindingManifestV1.model_validate(payload)
    with pytest.raises(ValueError, match="Asset Package digest does not match"):
        validate_asset_contract_bundle(
            identities,
            packages,
            mismatched_scene,
            qualification,
        )

    payload = _payload("iitp-scenario1-qualification.v1.json")
    payload["scene_ref"]["sha256"] = "0" * 64
    mismatched_qualification = SimulationQualificationV1.model_validate(payload)
    with pytest.raises(ValueError, match="Scene digest does not match"):
        validate_asset_contract_bundle(
            identities,
            packages,
            scene,
            mismatched_qualification,
        )

    payload = _payload("iitp-scenario1-qualification.v1.json")
    payload["build_attestation_refs"] = payload["build_attestation_refs"][:1]
    incomplete_qualification = SimulationQualificationV1.model_validate(payload)
    with pytest.raises(ValueError, match="missing a required build attestation"):
        validate_asset_contract_bundle(
            identities,
            packages,
            scene,
            incomplete_qualification,
        )


def test_unknown_fields_and_high_rate_payload_fields_are_absent():
    payload = _payload("omy-simulation-identity.v1.json")
    payload["ip_address"] = "10.0.0.7"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GlobalAssetIdentityV1.model_validate(payload)

    forbidden_property_names = {
        "command",
        "endpoint",
        "image_bytes",
        "joint_states",
        "password",
        "private_key",
        "ros_topic",
        "token",
        "video_bytes",
    }

    def property_names(value: object) -> set[str]:
        if isinstance(value, dict):
            names = set(value.get("properties", {}))
            for child in value.values():
                names.update(property_names(child))
            return names
        if isinstance(value, list):
            names: set[str] = set()
            for child in value:
                names.update(property_names(child))
            return names
        return set()

    for schema in asset_contract_schemas().values():
        assert property_names(schema).isdisjoint(forbidden_property_names)


def test_generated_asset_schemas_match_committed_files():
    schemas = asset_contract_schemas()
    assert set(schemas) == set(SCHEMA_MODELS)
    for filename, schema in schemas.items():
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"].endswith(f"/contracts/assets/{filename}")
        committed = json.loads((CONTRACT_DIR / filename).read_text(encoding="utf-8"))
        assert committed == schema
    assert export_schemas(CONTRACT_DIR, check=True) == []


def test_a0_contract_modules_contain_no_runtime_transport():
    package = ROOT / "src" / "fleet_control_plane" / "fleet_control_plane"
    forbidden_roots = {
        "asyncua",
        "fastapi",
        "grpc",
        "http",
        "httpx",
        "paho",
        "rclpy",
        "requests",
        "socket",
        "subprocess",
        "urllib",
    }
    imported_roots: set[str] = set()
    for filename in ("asset_models.py", "asset_contracts.py"):
        tree = ast.parse((package / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots.isdisjoint(forbidden_roots)
