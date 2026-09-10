from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fleet_control_plane.orchestration_models import WorkflowDefinitionV2
from fleet_control_plane.store import FleetStore
from fleet_control_plane.unity_dynamic_binding import UnityDynamicBindingV1, resolve_unity_workflow
from fleet_control_plane.unity_scenario_orchestrator import (
    UnityBridgeError, UnityBridgeHeartbeatV1, UnityScenarioFileBackend, _write_json_atomic,
)
from fleet_control_plane.workflow_authoring import (
    UnityAdapterProfileV2, WorkflowAuthoringStore, load_unity_adapter_profile,
)

ROOT = Path(__file__).resolve().parents[3]


def _fixture():
    scene = json.loads((ROOT / "contracts/assets/examples/iitp-scenario1-scene-binding.v1.json").read_text())
    catalog = json.loads((ROOT / "contracts/tasks/iitp-scenario1-skill-catalog.v2.json").read_text())
    catalog["actions"] = [item for item in catalog["actions"] if item["robot_type"] == "omy-f3m"]
    binding = scene["bindings"][0]
    contract = UnityDynamicBindingV1.model_validate(dict(
        scene=scene, catalog=catalog, assets=[dict(
            unity_binding_id=binding["unity_binding_id"], agent_id="test-omy-agent",
            action_refs=[item["action_ref"] for item in catalog["actions"]],
        )],
    ))
    workflow = WorkflowDefinitionV2.model_validate(dict(
        schema_version=2, workflow_id="custom-task", version=1,
        roles=[dict(role="my-handler", cardinality="1", access="exclusive",
                    selector=dict(requires=["simulate-pick-place@1"]))],
        steps=[dict(step_id="custom-step", kind="action", role="my-handler",
                    capability="simulate-pick-place@1", timeout_s=300,
                    parameters=dict(operation="place-on-waam-table", speed_scale=0.75))],
    ))
    inventory_binding = dict(
        identity=binding["identity"], asset_type=binding["asset_type"],
        package_ref=binding["package_ref"], unity_binding_id=binding["unity_binding_id"],
        hierarchy_path=binding["diagnostic_hierarchy_path"],
        articulation_root_binding_id=binding["articulation"]["root_binding_id"],
        ordered_joints=binding["articulation"]["ordered_joints"],
        controller_component_binding_id=binding["controller"]["component_binding_id"],
        frames=[dict(semantic=k, frame_id=v, present=True) for k, v in binding["frames"].items()],
        sensors=[],
    )
    heartbeat = dict(
        schema_version=1, scenario_id=scene["scene_id"], updated_at=datetime.now(timezone.utc).isoformat(),
        ready=True, observed_phase="ReadyPrePickPlace", physical_output_connected=False,
        scene_manifest_sha256=contract.scene_sha256, inventory_valid=True, inventory_errors=[],
        scene_inventory=dict(schema_version=1, scene_id=scene["scene_id"], scene_version=scene["version"],
                             unity_version=scene["unity_version"], physical_output_connected=False,
                             bindings=[inventory_binding]),
        action_bindings=[dict(unity_binding_id=binding["unity_binding_id"],
                             controller_component_binding_id=binding["controller"]["component_binding_id"],
                             action_key=action.action_ref, capability=action.capability,
                             operation=action.fixed_parameters["operation"], source_sha256=action.script.sha256,
                             legacy_step_id=action.step_id_template) for action in contract.catalog.actions],
    )
    return contract, workflow, heartbeat


def test_names_do_not_select_objects_and_fixed_parameters_select_versioned_action():
    contract, workflow, heartbeat = _fixture()
    roles, resolved = resolve_unity_workflow(workflow, contract)
    assert roles["my-handler"].agent_id == "test-omy-agent"
    selected = resolved["custom-step"]
    assert selected.action.action_ref == "omy-place-on-waam-table@1"
    assert selected.binding.identity.asset_id == "omy_sim_01"
    contract.validate_runtime(UnityBridgeHeartbeatV1.model_validate(heartbeat))
    heartbeat["scene_inventory"]["bindings"][0]["hierarchy_path"] = "RenamedScene/RenamedObject"
    contract.validate_runtime(UnityBridgeHeartbeatV1.model_validate(heartbeat))


@pytest.mark.parametrize("change,reason", [
    (lambda h: h.update(scene_manifest_sha256="0" * 64), "scene digest"),
    (lambda h: h.update(inventory_valid=False), "invalid"),
    (lambda h: h["scene_inventory"]["bindings"][0]["frames"][0].update(present=False), "absent"),
    (lambda h: h["scene_inventory"]["bindings"][0]["package_ref"].update(sha256="0" * 64), "package digest"),
    (lambda h: h["action_bindings"][0].update(source_sha256="0" * 64), "source mismatch"),
    (lambda h: h["action_bindings"].append(deepcopy(h["action_bindings"][0])), "duplicate"),
    (lambda h: h.update(action_bindings=[]), "source mismatch"),
])
def test_runtime_mismatch_fails_closed(change, reason):
    contract, _, heartbeat = _fixture()
    change(heartbeat)
    with pytest.raises(ValueError, match=reason):
        contract.validate_runtime(UnityBridgeHeartbeatV1.model_validate(heartbeat))


@pytest.mark.parametrize("change,reason", [
    (lambda w: w["steps"][0]["parameters"].update(operation="unregistered"), "installed Action"),
    (lambda w: w["steps"][0]["parameters"].update(speed_scale=3), "must be <="),
    (lambda w: w["steps"][0]["parameters"].update(script="evil"), "outside"),
    (lambda w: w["steps"][0].update(retry=dict(max_total_attempts=2)), "unsupported"),
    (lambda w: w["roles"][0]["selector"].update(zone_id="elsewhere"), "unzoned"),
    (lambda w: w["roles"][0]["selector"].update(requires=["unknown@1"]), "absent from role selector"),
])
def test_unsupported_intent_is_rejected(change, reason):
    contract, workflow, _ = _fixture()
    payload = workflow.model_dump(mode="json")
    change(payload)
    with pytest.raises(ValueError, match=reason):
        resolve_unity_workflow(WorkflowDefinitionV2.model_validate(payload), contract)


def test_two_matching_robots_require_an_unambiguous_binding():
    contract, workflow, _ = _fixture()
    payload = contract.model_dump(mode="json", by_alias=True)
    twin = deepcopy(payload["scene"]["bindings"][0])
    twin["unity_binding_id"] += "-second"
    for key in ("asset_id", "global_asset_id", "aas_id"):
        twin["identity"][key] += "-second"
    payload["scene"]["bindings"].append(twin)
    route = deepcopy(payload["assets"][0])
    route.update(unity_binding_id=twin["unity_binding_id"], agent_id="second-agent")
    payload["assets"].append(route)
    with pytest.raises(ValueError, match="exactly one"):
        resolve_unity_workflow(workflow, UnityDynamicBindingV1.model_validate(payload))


def test_v2_command_pins_asset_action_and_result_evidence(tmp_path):
    contract, workflow, heartbeat = _fixture()
    _, resolved = resolve_unity_workflow(workflow, contract)
    selected = resolved["custom-step"]
    backend = UnityScenarioFileBackend(
        scenario_id=contract.scene.scene_id, bridge_root=tmp_path,
        allowed_steps={"custom-step": selected.action.capability},
        heartbeat_max_age_s=5, result_timeout_s=0.2,
        dynamic_binding=contract, dispatches=resolved,
    )
    _write_json_atomic(tmp_path / "heartbeat.json", heartbeat)
    request = SimpleNamespace(
        execution_mode="SIMULATION", step_id="custom-step", capability_ref=selected.action.capability,
        agent_id=selected.agent_id, command_id="command-1", run_id="run-1",
        parameters={**selected.parameters, "bound_asset_id": selected.binding.identity.asset_id},
        goal=SimpleNamespace(asset_id=selected.binding.identity.asset_id,
                             not_valid_after=datetime.now(timezone.utc) + timedelta(minutes=1)),
    )
    command, result_path = backend._command_for(request, require_ready=True)
    assert command.schema_version == 2
    assert command.action_key == selected.action.action_ref
    result = dict(schema_version=2, scenario_id=command.scenario_id, command_id=command.command_id,
                  run_id=command.run_id, step_id=command.step_id, state="SUCCEEDED", detail="completed",
                  observed_phase="Complete", completed_at=datetime.now(timezone.utc).isoformat(),
                  physical_output_connected=False, output_isolation_confirmed=True)
    for key in ("unity_binding_id", "controller_component_binding_id", "action_key",
                "scene_manifest_sha256", "action_source_sha256"):
        result[key] = getattr(command, key)
    _write_json_atomic(result_path, result)
    assert backend.recover_skill(request)["state"] == "SUCCEEDED"
    result["unity_binding_id"] = "wrong-asset"
    _write_json_atomic(result_path, result)
    with pytest.raises(UnityBridgeError, match="evidence differs"):
        backend.recover_skill(request)
    request.agent_id = "wrong-agent"
    with pytest.raises(UnityBridgeError, match="resolved Unity"):
        backend._command_for(request, require_ready=False)
    request.agent_id = selected.agent_id
    request.goal.asset_id = "another-robot"
    with pytest.raises(UnityBridgeError, match="resolved Unity"):
        backend._command_for(request, require_ready=False)
    request.goal.asset_id = selected.binding.identity.asset_id
    request.parameters = {**request.parameters, "speed_scale": 1.0}
    with pytest.raises(UnityBridgeError, match="resolved Unity"):
        backend._command_for(request, require_ready=False)


def test_dynamic_compatibility_preserves_publication_pin_and_v1_digest(tmp_path):
    contract, workflow, _ = _fixture()
    path = ROOT / "contracts/tasks/iitp-scenario1-unity-adapter.v1.json"
    legacy, old_digest = load_unity_adapter_profile(path)
    payload = legacy.model_dump(mode="json")
    payload.update(schema_version=2, dynamic_binding=contract.model_dump(mode="json", by_alias=True))
    profile = UnityAdapterProfileV2.model_validate(payload)
    FleetStore(tmp_path / "fleet.db")
    store = WorkflowAuthoringStore(tmp_path / "fleet.db", skill_catalog_paths=[
        ROOT / "contracts/tasks/iitp-scenario1-skill-catalog.v2.json"])
    assert store.compatibility_issues(workflow, profile) == []
    profile.dynamic_binding.catalog.actions[0].script.sha256 = "0" * 64
    assert "publication differs" in store.compatibility_issues(workflow, profile)[0]
    assert load_unity_adapter_profile(path)[1] == old_digest
