"""Build a pinned v2 adapter profile from trusted local scene/catalog/config files."""

import argparse
import json
from pathlib import Path

from fleet_control_plane.asset_models import SceneBindingManifestV1
from fleet_control_plane.skill_catalog import load_skill_catalog
from fleet_control_plane.unity_scenario_orchestrator import load_scenario_configuration
from fleet_control_plane.workflow_authoring import UnityAdapterProfileV2, load_unity_adapter_profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("scene", "template", "catalog", "config", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    scene = SceneBindingManifestV1.model_validate_json(args.scene.read_text())
    template, _ = load_unity_adapter_profile(args.template)
    catalog, _ = load_skill_catalog(args.catalog)
    config = load_scenario_configuration(args.config)
    if scene.scene_id != template.scenario_id or scene.scene_id != config.scenario_id:
        raise ValueError("Scene, adapter, and config identities differ")
    bindings = {item.identity.asset_id: item for item in scene.bindings}
    assets = []
    for role, target in config.roles.items():
        binding = bindings[target.asset_id]
        assets.append(dict(
            unity_binding_id=binding.unity_binding_id, agent_id=target.agent_id,
            action_refs=[action.action_ref for action in catalog.actions
                         if action.role_template == role and action.robot_type == binding.expected_asset_profile],
        ))
    raw = template.model_dump(mode="json", by_alias=True)
    raw.update(schema_version=2, dynamic_binding=dict(scene=scene, catalog=catalog, assets=assets))
    profile = UnityAdapterProfileV2.model_validate(raw)
    encoded = json.dumps(profile.model_dump(mode="json", by_alias=True), indent=2) + "\n"
    # Generated profiles are immutable; replacing a prior version is explicit work.
    if args.output.exists() and args.output.read_text() != encoded:
        raise ValueError("Output profile already exists with different content; choose a new version")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded)
    _, digest = load_unity_adapter_profile(args.output)
    print(json.dumps(dict(path=str(args.output), profile_sha256=digest,
                          scene_sha256=profile.dynamic_binding.scene_sha256)))


if __name__ == "__main__":
    main()
