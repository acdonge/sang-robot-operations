"""Resolve published simulation intent against one pinned Unity scene inventory."""

from __future__ import annotations

from pydantic import Field, model_validator

from fleet_control_plane.asset_models import (
    SceneAssetBindingV1,
    SceneBindingManifestV1,
    UnityBindingId,
    contract_payload_sha256,
)
from fleet_control_plane.models import FleetModel, SafeId
from fleet_control_plane.orchestration_models import CapabilityRef, WorkflowDefinitionV2
from fleet_control_plane.skill_catalog import (
    RobotActionDefinitionV1,
    SkillCatalogV1,
    validate_skill_parameters,
)
from fleet_control_plane.unity_asset_inventory import validate_unity_inventory


class UnityAssetRouteV1(FleetModel):
    unity_binding_id: UnityBindingId
    agent_id: SafeId
    action_refs: list[CapabilityRef] = Field(min_length=1, max_length=256)


class UnityDynamicBindingV1(FleetModel):
    scene: SceneBindingManifestV1
    catalog: SkillCatalogV1
    assets: list[UnityAssetRouteV1] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def routes_are_exact(self):
        bindings = {item.unity_binding_id: item for item in self.scene.bindings}
        actions = {item.action_ref: item for item in self.catalog.actions}
        for key in ("unity_binding_id", "agent_id"):
            values = [getattr(item, key) for item in self.assets]
            if len(values) != len(set(values)):
                raise ValueError(f"dynamic routes require unique {key}")
        for route in self.assets:
            binding = bindings.get(route.unity_binding_id)
            if binding is None or binding.controller is None:
                raise ValueError("dynamic route requires a declared scene controller")
            if len(set(route.action_refs)) != len(route.action_refs):
                raise ValueError("dynamic route Action references must be unique")
            for ref in route.action_refs:
                action = actions.get(ref)
                if action is None:
                    raise ValueError(f"dynamic route references unknown Action {ref}")
                if action.execution_mode != "SIMULATION" or action.script.runtime != "UNITY_CSHARP":
                    raise ValueError("dynamic routes accept installed Unity simulation Actions only")
                if (action.asset_type != binding.asset_type
                        or action.robot_type != binding.expected_asset_profile):
                    raise ValueError(f"Action {ref} does not match the bound robot type")
        return self

    @property
    def scene_sha256(self) -> str:
        return contract_payload_sha256(self.scene)

    def validate_runtime(self, heartbeat) -> None:
        if (heartbeat.scene_manifest_sha256 != self.scene_sha256
                or heartbeat.inventory_valid is not True
                or heartbeat.inventory_errors
                or heartbeat.scene_inventory is None):
            raise ValueError("Unity runtime inventory is absent, invalid, or belongs to another scene digest")
        validate_unity_inventory(heartbeat.scene_inventory, self.scene)
        observed = {}
        for item in heartbeat.action_bindings:
            key = (item.unity_binding_id, item.action_key)
            if key in observed:
                raise ValueError("Unity runtime has duplicate installed Action bindings")
            observed[key] = item
        actions = {item.action_ref: item for item in self.catalog.actions}
        bindings = {item.unity_binding_id: item for item in self.scene.bindings}
        expected_keys = set()
        for route in self.assets:
            binding = bindings[route.unity_binding_id]
            for ref in route.action_refs:
                key = (route.unity_binding_id, ref)
                expected_keys.add(key)
                actual = observed.get(key)
                action = actions[ref]
                if (actual is None
                        or actual.controller_component_binding_id != binding.controller.component_binding_id
                        or actual.capability != action.capability
                        or actual.operation != action.fixed_parameters.get("operation")
                        or actual.source_sha256 != action.script.sha256):
                    raise ValueError(f"Unity installed Action binding/source mismatch: {ref}")
        if set(observed) != expected_keys:
            raise ValueError("Unity installed Action closure differs from the adapter profile")


class ResolvedUnityAction(FleetModel):
    binding: SceneAssetBindingV1
    agent_id: SafeId
    action: RobotActionDefinitionV1
    parameters: dict


def resolve_unity_workflow(
    workflow: WorkflowDefinitionV2, contract: UnityDynamicBindingV1,
) -> tuple[dict[str, UnityAssetRouteV1], dict[str, ResolvedUnityAction]]:
    """Reject ambiguity; workflow names never select Unity objects or script types."""
    actions = {item.action_ref: item for item in contract.catalog.actions}
    skills = {item.capability: item for item in contract.catalog.skills}
    bindings = {item.unity_binding_id: item for item in contract.scene.bindings}
    roles = {}
    used_assets = set()
    for role in workflow.roles:
        if role.cardinality != "1" or role.access != "exclusive" or role.selector.zone_id is not None:
            raise ValueError("dynamic Unity v1 requires one exclusive, unzoned asset per role")
        candidates = [route for route in contract.assets if set(role.selector.requires).issubset(
            {actions[ref].capability for ref in route.action_refs}
        )]
        if len(candidates) != 1:
            raise ValueError(f"role {role.role} must resolve to exactly one installed asset, found {len(candidates)}")
        route = candidates[0]
        if route.unity_binding_id in used_assets:
            raise ValueError("multiple exclusive roles cannot alias one Unity asset")
        used_assets.add(route.unity_binding_id)
        roles[role.role] = route

    resolved = {}
    previous = None
    for step in workflow.steps:
        if step.kind != "action":
            raise ValueError("dynamic Unity v1 accepts sequential Action steps only")
        if step.depends_on != ([] if previous is None else [previous]):
            raise ValueError("dynamic Unity v1 requires an explicit linear Action dependency chain")
        previous = step.step_id
        if (step.retry.max_total_attempts != 1 or step.compensation_step is not None
                or step.fan_out is not None or step.lifetime is not None):
            raise ValueError(f"step {step.step_id} uses unsupported execution controls")
        role = next(item for item in workflow.roles if item.role == step.role)
        if step.capability not in role.selector.requires:
            raise ValueError(f"step {step.step_id} capability is not required by its role")
        route = roles[step.role]
        matches = [actions[ref] for ref in route.action_refs
                   if actions[ref].capability == step.capability
                   and all(step.parameters.get(k) == v for k, v in actions[ref].fixed_parameters.items())]
        if len(matches) != 1:
            raise ValueError(f"step {step.step_id} must match exactly one installed Action")
        action = matches[0]
        if set(step.parameters) - (set(action.fixed_parameters) | set(action.editable_parameters)):
            raise ValueError(f"step {step.step_id} contains inputs outside its Action contract")
        validate_skill_parameters(skills[step.capability], step.parameters)
        resolved[step.step_id] = ResolvedUnityAction(
            binding=bindings[route.unity_binding_id], agent_id=route.agent_id,
            action=action, parameters=step.parameters,
        )
    return roles, resolved
