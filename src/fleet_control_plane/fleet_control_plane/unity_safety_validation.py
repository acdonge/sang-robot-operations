"""Cross-check a Unity Safety Shadow project against its versioned Task bundle."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator

from fleet_control_plane.models import FleetModel, SafeId
from fleet_control_plane.safety_models import SafetySnapshotV1
from fleet_control_plane.task_bundle import TaskBundleV1, load_task_bundle


class UnitySafetyShadowConfigV1(FleetModel):
    enabled: bool
    desktop_only: bool
    edge_endpoint: str
    source_kind: Literal["synthetic", "replay"]
    agent_id: SafeId
    run_id: SafeId
    coordinate_frame: SafeId
    ttl_ms: int = Field(ge=10, le=10_000)
    publish_interval_seconds: float = Field(ge=0.1, le=60.0)
    tracking_signal_asset_id: SafeId
    asset_ids: list[SafeId] = Field(min_length=1, max_length=256)
    zone_asset_ids: list[SafeId] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def references_are_safe_and_coherent(self) -> "UnitySafetyShadowConfigV1":
        endpoint = urlparse(self.edge_endpoint)
        if (
            endpoint.scheme != "http"
            or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
            or endpoint.path != "/safety/v1/snapshots"
        ):
            raise ValueError("edge_endpoint must target the loopback Safety Edge snapshot API")
        if len(self.asset_ids) != len(set(self.asset_ids)):
            raise ValueError("asset_ids must be unique")
        declared = set(self.asset_ids)
        if self.tracking_signal_asset_id not in declared:
            raise ValueError("tracking_signal_asset_id must be declared in asset_ids")
        if not set(self.zone_asset_ids).issubset(declared):
            raise ValueError("zone_asset_ids must be declared in asset_ids")
        return self


def _read_editor_version(project_path: Path) -> str:
    version_path = project_path / "ProjectSettings" / "ProjectVersion.txt"
    for line in version_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("m_EditorVersion:"):
            return line.partition(":")[2].strip()
    raise ValueError(f"Unity Editor version is missing from {version_path}")


def validate_unity_safety_project(
    bundle: TaskBundleV1,
    project_path: str | Path,
) -> dict[str, object]:
    project = Path(project_path).resolve()
    config_path = project / "Assets" / "StreamingAssets" / "sang_safety_shadow.json"
    config = UnitySafetyShadowConfigV1.model_validate_json(
        config_path.read_text(encoding="utf-8")
    )
    editor_version = _read_editor_version(project)

    if not config.enabled or not config.desktop_only:
        raise ValueError("the initial integration requires enabled desktop-only reporting")
    if editor_version != bundle.unity_editor_version:
        raise ValueError(
            f"Unity Editor version mismatch: {editor_version} != {bundle.unity_editor_version}"
        )
    if config.agent_id != bundle.processing_node_id:
        raise ValueError("Unity agent_id must match Task processing_node_id")
    if config.run_id != bundle.task_run.run_id:
        raise ValueError("Unity run_id must match the bundled Task Run")

    bundled_assets = {asset.asset_id: asset for asset in bundle.assets}
    unknown_assets = sorted(set(config.asset_ids) - set(bundled_assets))
    if unknown_assets:
        raise ValueError(f"Unity config references unknown Task assets: {unknown_assets}")
    non_simulation_assets = sorted(
        asset_id
        for asset_id in config.asset_ids
        if bundled_assets[asset_id].runtime_kind != "simulation"
    )
    if non_simulation_assets:
        raise ValueError(
            f"Unity Shadow config may use only simulation assets: {non_simulation_assets}"
        )

    now = datetime.now(timezone.utc)
    SafetySnapshotV1.model_validate(
        {
            "schema_version": 1,
            "snapshot_id": "unity-contract-validation-1",
            "source_kind": config.source_kind,
            "agent_id": config.agent_id,
            "run_id": config.run_id,
            "captured_at": now,
            "ttl_ms": config.ttl_ms,
            "coordinate_frame": config.coordinate_frame,
            "clock_quality": "synchronized",
            "asset_ids": config.asset_ids,
            "signals": [
                {
                    "signal_id": "xr-human-tracking",
                    "sampled_at": now,
                    "asset_id": config.tracking_signal_asset_id,
                    "required": True,
                    "valid": False,
                    "confidence": None,
                }
            ],
            "human_robot_distance_m": None,
            "tracking_confidence": None,
            "forbidden_zone_occupancies": [],
            "task_progress": {
                "expected_step": bundle.workflow[0].step_id,
                "observed_step": bundle.workflow[0].step_id,
            },
            "evidence_refs": [],
        }
    )

    return {
        "status": "ok",
        "project": str(project),
        "unity_editor_version": editor_version,
        "bundle_id": bundle.bundle_id,
        "run_id": config.run_id,
        "source_kind": config.source_kind,
        "asset_ids": config.asset_ids,
        "command_capability": False,
    }


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a Unity Safety Shadow project against a Task bundle"
    )
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--unity-project", type=Path, required=True)
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    result = validate_unity_safety_project(
        load_task_bundle(options.bundle),
        options.unity_project,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
