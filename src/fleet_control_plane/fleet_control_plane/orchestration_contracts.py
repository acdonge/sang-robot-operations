"""Deterministic JSON Schema export for G0 orchestration contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TypeAlias

from pydantic import BaseModel

from fleet_control_plane.orchestration_models import (
    CapabilityProfileV1,
    CoordinatedGroupV1,
    DeviceManifestV1,
    ExecutionContractBundleV1,
    RuntimeBindingV1,
    SafetySnapshotV2,
    SpatialContractBundleV1,
    UnityBuildAttestationV1,
    WorkflowDefinitionV2,
)


SchemaModel: TypeAlias = type[BaseModel]

SCHEMA_MODELS: dict[str, tuple[SchemaModel, str]] = {
    "capability-profile-v1.schema.json": (
        CapabilityProfileV1,
        "Semantic capability profile for heterogeneous assets.",
    ),
    "coordinated-group-v1.schema.json": (
        CoordinatedGroupV1,
        "Transitive, all-or-nothing coordinated multi-asset group contract.",
    ),
    "device-manifest-v1.schema.json": (
        DeviceManifestV1,
        "Immutable technical manifest separated from deployment secrets.",
    ),
    "execution-contracts-v1.schema.json": (
        ExecutionContractBundleV1,
        "Delegated execution, lease, skill, group, envelope, and policy-demand messages.",
    ),
    "runtime-binding-v1.schema.json": (
        RuntimeBindingV1,
        "Immutable-per-Run capability and execution-mode binding.",
    ),
    "safety-snapshot-v2.schema.json": (
        SafetySnapshotV2,
        "Command-free, source-ordered safety evidence payload.",
    ),
    "spatial-contracts-v1.schema.json": (
        SpatialContractBundleV1,
        "Versioned zone and calibration contracts.",
    ),
    "unity-build-attestation-v1.schema.json": (
        UnityBuildAttestationV1,
        "Signed provenance for isolated Unity observer, simulation executor, or fixture builds.",
    ),
    "workflow-definition-v2.schema.json": (
        WorkflowDefinitionV2,
        "Vendor-neutral capability DAG with role cardinality and structured predicates.",
    ),
}


def orchestration_contract_schemas() -> dict[str, dict[str, object]]:
    schemas: dict[str, dict[str, object]] = {}
    for filename, (model, description) in SCHEMA_MODELS.items():
        schema = model.model_json_schema(
            ref_template="#/$defs/{model}",
            mode="validation",
        )
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://sang.local/contracts/orchestration/{filename}"
        schema["description"] = description
        schemas[filename] = schema
    return schemas


def _serialized_schemas() -> dict[str, str]:
    return {
        filename: json.dumps(schema, indent=2, sort_keys=True) + "\n"
        for filename, schema in orchestration_contract_schemas().items()
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
