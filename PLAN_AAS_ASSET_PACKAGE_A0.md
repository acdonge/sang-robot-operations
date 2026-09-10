# PLAN - AAS Asset Package Contract Freeze A0

Goal: Implement Phase A0 of the canonical AAS Asset Package and Dynamic Unity Adapter design as strict, machine-readable, command-free contracts.

Done when:
- Global asset/AAS identity, Asset Package, Scene Binding Manifest, and Simulation Qualification models have deterministic JSON Schemas.
- Scenario 1-oriented examples validate and preserve separate physical/simulation identity, immutable object hashes, stable Unity binding IDs, and output isolation.
- Cross-document validation fails closed on digest mismatch, duplicate or missing bindings, incompatible package references, malformed joint/frame contracts, and incomplete qualification evidence.
- Existing Registry, orchestration, ROS 2, OPC UA, Action Library, and physical command boundaries are unchanged.
- Focused contract tests, existing orchestration contract tests, schema regeneration checks, and the fleet-control-plane regression suite pass.

Out of scope:
- SQLite migrations or writable AAS Repository integration.
- Uploading model artifacts or loading Unity scenes at runtime.
- Editing the current Unity scene or adding `SangAssetBinding` components; that is Phase A1.
- Dynamic action execution; that is Phase A2.
- ROS 2 simulation observations, MCAP parity, or physical commissioning.

## Milestones
- [x] M1: Add strict Pydantic asset/package/binding/qualification models - verified by nine positive and fail-closed unit tests.
- [x] M2: Export deterministic JSON Schemas and committed Scenario 1 examples - verified generated files exactly match source models.
- [x] M3: Document the A0 boundary and implementation status - canonical design, task plan, requirements, root README, and contract README now agree.
- [x] M4: Run focused and full regressions, review the diff, and record evidence - 10 A0 tests and 307 fleet-control-plane tests passed.

## Decisions log
- 2026-08-31: Implement only Phase A0 in this iteration because A1 changes the live Unity project and needs its own binding report and regression gate.
- 2026-08-31: Keep these contracts in `contracts/assets/`; do not mutate existing orchestration v1 schemas or introduce a second operational source of truth.
- 2026-08-31: A0 is simulation-only and represents output isolation as the literal value `physical_output_connected=false`.
- 2026-08-31: AAS identifiers and submodel slots are projection metadata only; no AAS server, control method, or high-rate payload is introduced.

## Current state
All milestones are complete. Four deterministic schemas, five Scenario 1-oriented examples, canonical cross-document digest validation, and evidence are committed. Phase A1 is next.
