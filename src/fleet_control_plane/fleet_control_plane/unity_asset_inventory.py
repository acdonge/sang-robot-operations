"""Fail-closed comparison of Unity runtime inventory and Scene contracts."""

from __future__ import annotations

from fleet_control_plane.asset_models import (
    SceneBindingManifestV1,
    UnitySceneInventoryV1,
)


class UnityInventoryMismatch(ValueError):
    """The running Unity scene differs from its immutable binding manifest."""


def validate_unity_inventory(
    inventory: UnitySceneInventoryV1,
    scene: SceneBindingManifestV1,
) -> None:
    if (inventory.scene_id, inventory.scene_version) != (scene.scene_id, scene.version):
        raise UnityInventoryMismatch("Unity inventory references a different Scene version")
    if inventory.unity_version != scene.unity_version:
        raise UnityInventoryMismatch("Unity Editor/Player version differs from the Scene")
    if inventory.time_source != scene.time_source:
        raise UnityInventoryMismatch("Unity inventory time source differs from the Scene")
    if inventory.physical_output_connected or scene.output_isolation.physical_output_connected:
        raise UnityInventoryMismatch("Unity inventory is not output-isolated")

    expected = {item.unity_binding_id: item for item in scene.bindings}
    observed = {item.unity_binding_id: item for item in inventory.bindings}
    if set(expected) != set(observed):
        missing = sorted(set(expected) - set(observed))
        extra = sorted(set(observed) - set(expected))
        raise UnityInventoryMismatch(
            f"Unity binding closure differs; missing={missing}, extra={extra}"
        )

    for binding_id, expected_binding in expected.items():
        actual = observed[binding_id]
        if actual.identity != expected_binding.identity:
            raise UnityInventoryMismatch(f"{binding_id} asset identity differs")
        if actual.asset_type != expected_binding.asset_type:
            raise UnityInventoryMismatch(f"{binding_id} asset type differs")
        if actual.package_ref != expected_binding.package_ref:
            raise UnityInventoryMismatch(f"{binding_id} package digest differs")
        frame_map = {item.semantic: item for item in actual.frames}
        if set(frame_map) != set(expected_binding.frames):
            raise UnityInventoryMismatch(f"{binding_id} frame closure differs")
        for semantic, frame_id in expected_binding.frames.items():
            frame = frame_map[semantic]
            if frame.frame_id != frame_id or not frame.present:
                raise UnityInventoryMismatch(
                    f"{binding_id} frame {semantic} is absent or mismatched"
                )

        expected_sensor_ids = {item.sensor_id for item in expected_binding.sensors}
        actual_sensor_ids = {item.sensor_id for item in actual.sensors if item.present}
        if actual_sensor_ids != expected_sensor_ids:
            raise UnityInventoryMismatch(f"{binding_id} sensor closure differs")

        if expected_binding.articulation is None:
            if actual.articulation_root_binding_id is not None or actual.ordered_joints:
                raise UnityInventoryMismatch(f"{binding_id} has an unexpected articulation")
        elif (
            actual.articulation_root_binding_id
            != expected_binding.articulation.root_binding_id
            or actual.ordered_joints != expected_binding.articulation.ordered_joints
        ):
            raise UnityInventoryMismatch(f"{binding_id} articulation binding differs")

        expected_controller = (
            None
            if expected_binding.controller is None
            else expected_binding.controller.component_binding_id
        )
        if actual.controller_component_binding_id != expected_controller:
            raise UnityInventoryMismatch(f"{binding_id} controller binding differs")
