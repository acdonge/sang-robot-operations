# AAS Asset Package and Dynamic Unity Adapter Phase A0 Evidence

Date: 2026-08-31

## Result

PASS. Phase A0 freezes strict, machine-readable contracts for read-only global/AAS identity,
immutable Asset Packages, Unity Scene Binding Manifests, and exact Simulation Qualification evidence.
It adds no AAS Repository, database migration, artifact loader, Unity runtime, ROS 2 transport, OPC UA
write, browser path, vendor endpoint, or physical command sink.

## Delivered contracts

- `global-asset-identity-v1.schema.json` keeps SANG `asset_id`, global identity, AAS identity,
  runtime class, projection revision, Submodel slots, and relationships distinct.
- `asset-package-v1.schema.json` pins content-addressed objects, media type, size, SHA-256, runtime,
  dependency closure, frame contract, Device Manifest, and build attestation.
- `scene-binding-manifest-v1.schema.json` pins the Unity version, world/time convention, exact package
  digests, stable binding IDs, articulation order, frames, controller, workspace, Reset/outcome,
  signals, sensors, and literal output isolation.
- `simulation-qualification-v1.schema.json` pins the Run, Scene, packages, Action source/build,
  adapter/orchestrator builds, validation profile, attestations, report, evidence, and verdict.

The Pydantic cross-document validator computes canonical metadata digests and rejects identity
revision drift, missing relationship targets, missing or mismatched packages, profile mismatches,
Scene digest drift, incomplete package closure, missing build attestations, different output-isolation
evidence, and time-source mismatch.

## Fixtures

Five committed examples describe a physical OMY, its distinct Scenario 1 simulation twin, one
immutable OMY Unity package, one transitional Scenario 1 Scene Binding, and one passing disconnected
Simulation Qualification. Package and Scene references use the actual canonical fixture digests, not
placeholder references. They are schema fixtures and do not claim that the current Player is promoted
under the new A0 contract.

## Verification

- A0 positive, fail-closed, schema, source-boundary, and cross-document tests: `10 passed`.
- A0 plus existing G0 orchestration contract tests: `43 passed` after cross-document hardening.
- Full fleet-control-plane regression: `307 passed`.
- Existing non-blocking warning: Starlette TestClient/httpx deprecation.
- Deterministic schema regeneration with `--check`: passed.
- Python syntax compilation: passed.
- `git diff --check`: passed.
- Source-boundary test confirms the A0 modules import no HTTP, ROS 2, OPC UA, process, or socket
  runtime transport.

## Next phase

Phase A1 should inventory the current Scenario 1 Unity Player and scene, add stable
`SangAssetBinding` components for OMY, RB5, the carrier, cameras, and required environment objects,
and generate a binding report against these A0 contracts. It must preserve the current fixed behavior
and re-run the existing Scenario 1 qualification before any Dynamic Unity Adapter work begins.
