# Generic Orchestration Contracts (G0)

This directory is the machine-readable G0 contract freeze for heterogeneous robots, cameras,
simulators, and future process equipment. It implements validation only. It does not persist a Run,
open ROS 2 or vendor transports, mint a cryptographically verified production lease, or expose a
physical command sink.

## Schemas

- `capability-profile-v1.schema.json`: semantic capabilities and their execution requirements.
- `device-manifest-v1.schema.json`: immutable technical contract, separate from connection secrets.
- `spatial-contracts-v1.schema.json`: versioned zones and calibrations.
- `workflow-definition-v2.schema.json`: vendor-neutral role/capability DAG.
- `runtime-binding-v1.schema.json`: immutable-per-Run asset and mode selection.
- `coordinated-group-v1.schema.json`: transitive exclusive multi-asset group.
- `execution-contracts-v1.schema.json`: envelope, grant, sublease, ExecuteSkill, and PolicyDemand.
- `safety-snapshot-v2.schema.json`: ordered, command-free safety evidence. It intentionally rejects
  producer-supplied `source_kind`.
- `unity-build-attestation-v1.schema.json`: signed role/build provenance for isolated Unity Players.

Pydantic performs the cross-document checks that JSON Schema cannot express: workflow DAG and
reference integrity, role cardinality/capability matching, execution-mode/runtime compatibility,
physical commissioning readiness, and parent-grant/sublease fencing.

Effectful HIL against a physical controller is permitted only with the same immutable
output-isolation evidence pinned by binding, grant, and sublease. HIL intentionally does not satisfy
physical commissioning: zero timing remains acceptable only because physical outputs are verified
disconnected. `PHYSICAL_*` continues to reject zero timing and uncommissioned command capability.

## Camera Scaling Example

`examples/articulated-arm-profile.v1.json` and `examples/example-arm-manifest.v1.json` are complete
schema-valid registry examples. Their zero timing values deliberately mean **not commissioned**, and
the manifest remains command-free.

`examples/generic-position-and-observe.v2.json` is unchanged when the `cell-view` role is bound to
either `examples/simulation-binding-3-cameras.v1.json` or
`examples/simulation-binding-12-cameras.v1.json`. Camera count is a Run binding concern; it is not a
workflow branch or a robot/vendor field. Every example is simulation-only and declares
`command_sink_present: false`.

## Regeneration

From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src/fleet_control_plane \
python3 -m fleet_control_plane.orchestration_contracts \
  --output-dir contracts/orchestration

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src/fleet_control_plane \
python3 -m fleet_control_plane.orchestration_contracts \
  --output-dir contracts/orchestration --check
```

Database/API registry integration is G1. A simulation-only Orchestrator and dev lease verifier are
G3. Physical motion remains prohibited until the attended G7 commissioning gate described in
`../../docs/MULTI_ROBOT_ORCHESTRATOR_IMPLEMENTATION_DESIGN.md`.
