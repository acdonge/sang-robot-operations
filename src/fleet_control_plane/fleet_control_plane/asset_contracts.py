"""Deterministic JSON Schema export for AAS and simulation asset contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TypeAlias

from pydantic import BaseModel

from fleet_control_plane.asset_models import (
    AssetPackageV1,
    GlobalAssetIdentityV1,
    SceneBindingManifestV1,
    SimulationQualificationV1,
    UnitySceneInventoryV1,
)


SchemaModel: TypeAlias = type[BaseModel]

SCHEMA_MODELS: dict[str, tuple[SchemaModel, str]] = {
    "global-asset-identity-v1.schema.json": (
        GlobalAssetIdentityV1,
        "Read-only AAS identity projection linked to one stable SANG asset.",
    ),
    "asset-package-v1.schema.json": (
        AssetPackageV1,
        "Immutable object references and compatibility for one simulation asset package.",
    ),
    "scene-binding-manifest-v1.schema.json": (
        SceneBindingManifestV1,
        "Stable Unity binding IDs, frames, controllers, sensors, and output isolation.",
    ),
    "simulation-qualification-v1.schema.json": (
        SimulationQualificationV1,
        "Exact scene, package, Action, build, Run, and output-isolation evidence.",
    ),
    "unity-scene-inventory-v1.schema.json": (
        UnitySceneInventoryV1,
        "Runtime-observed Unity asset bindings checked against one Scene Manifest.",
    ),
}


def asset_contract_schemas() -> dict[str, dict[str, object]]:
    schemas: dict[str, dict[str, object]] = {}
    for filename, (model, description) in SCHEMA_MODELS.items():
        schema = model.model_json_schema(
            ref_template="#/$defs/{model}",
            mode="validation",
        )
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://sang.local/contracts/assets/{filename}"
        schema["description"] = description
        schemas[filename] = schema
    return schemas


def _serialized_schemas() -> dict[str, str]:
    return {
        filename: json.dumps(schema, indent=2, sort_keys=True) + "\n"
        for filename, schema in asset_contract_schemas().items()
    }


def export_schemas(output_dir: Path, *, check: bool = False) -> list[str]:
    """Write schemas atomically, or return mismatched paths in check mode."""

    serialized = _serialized_schemas()
    mismatches: list[str] = []
    if check:
        for filename, expected in serialized.items():
            path = output_dir / filename
            if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                mismatches.append(str(path))
        if output_dir.is_dir():
            expected_names = set(serialized)
            for path in output_dir.glob("*.schema.json"):
                if path.name not in expected_names:
                    mismatches.append(str(path))
        return mismatches

    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, payload in serialized.items():
        path = output_dir / filename
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
    return mismatches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when committed schemas differ from generated output",
    )
    args = parser.parse_args(argv)
    mismatches = export_schemas(args.output_dir, check=args.check)
    if mismatches:
        for mismatch in mismatches:
            print(mismatch)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
