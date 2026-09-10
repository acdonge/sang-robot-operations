# AAS and Simulation Asset Contracts (A0)

This directory is the machine-readable Phase A0 contract freeze for durable AAS identity links,
immutable simulation Asset Packages, stable Unity Scene Bindings, and exact Simulation Qualification
evidence.

## Authority boundary

- Central SQLite remains authoritative for operational `asset_id`, assignments, Tasks, and Runs.
- These identity documents are a read-only AAS projection contract, not a writable AAS Repository.
- Object Store holds immutable artifact bytes. Contracts contain object IDs, sizes, media types, and
  SHA-256 values, never host paths or embedded binaries.
- ROS 2 and MCAP remain the high-rate runtime and evidence paths.
- OPC UA remains a bounded status projection.
- Every v1 Scene Binding is simulation-only and literally requires
  `physical_output_connected: false`.

The models and schema exporter contain no DB, HTTP, ROS 2, Unity runtime, vendor SDK, or command
transport code. Runtime loading begins in Phase A1/A2 after a separate qualification gate.

## Schemas

- `global-asset-identity-v1.schema.json`: stable SANG, global, and AAS identity linkage plus durable
  relationship and Submodel slots.
- `asset-package-v1.schema.json`: immutable artifact and dependency closure for one simulated asset.
- `scene-binding-manifest-v1.schema.json`: stable Unity IDs, articulation order, frames, controller,
  signals, sensors, Reset, package versions, and output isolation.
- `simulation-qualification-v1.schema.json`: exact Run, scene, package, Action, build, report, and
  output-isolation evidence used for a verdict.

## Examples

The `examples/` directory models a physical OMY and its separate Scenario 1 simulation twin, one OMY
Unity package, one transitional full-scene binding, and one passing output-disconnected qualification.
The identifiers and digests are contract fixtures, not claims about a currently promoted build.

## Regeneration

From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src/fleet_control_plane \
python3 -m fleet_control_plane.asset_contracts \
  --output-dir contracts/assets

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src/fleet_control_plane \
python3 -m fleet_control_plane.asset_contracts \
  --output-dir contracts/assets --check
```

Phase A1 will inventory the real Scenario 1 Player and generate a binding report without changing its
fixed behavior.
