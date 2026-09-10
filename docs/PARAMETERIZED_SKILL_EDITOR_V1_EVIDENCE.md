# Parameterized Skill Editor v1 - Verification Evidence

Verified on 2026-08-31.

## Contract and UI

- The Skill catalog exposes four immutable Scenario 1 capabilities with closed parameter schemas.
- Workflow Studio generated typed controls from those schemas and exposed `speed_scale` from `0.50` through `1.00` in `0.05` steps.
- Fixed adapter operations were visible but not editable.
- Registered Skills showed a read-only JSON contract preview; only unknown capabilities retained advanced raw JSON editing.
- The Admin UI production build completed successfully.

## Backend regression

- Full Fleet Control Plane test suite: `280 passed`.
- Expected warning only: the existing Starlette `httpx` deprecation warning.
- Coverage includes catalog discovery, closed-schema validation, bounds, publication compatibility, system-owned binding metadata, Admin UI wiring, Unity orchestration, and simulation artifact isolation.

## Unity build and isolation

- Unity Editor: `6000.5.6f1`.
- Player role: `simulation_executor`.
- Compile define: `SANG_SIMULATION_PLAYER`.
- `physical_output_connected`: `false`.
- Artifact isolation scan: passed with no findings.
- `Assembly-CSharp.dll` SHA-256: `6cf57a5cc2ac0c8e47da03a226b95b5fd1f894b592670c9b619a71510b5ec36f`.
- Runtime bridge root: `/home/sa08050/app developer/v2_ws/runtime/phase3-lab1/unity-scenario1`.

## Browser-driven end-to-end run

- Published task/workflow: `IITP Scenario 1 Parameterized Skill Demo`, version `2`.
- Workflow digest: `42b6bf4f6a328f849006f360cf46389966ce0206d55f1936002a041c44632d75`.
- Successful run: `sim-209484299b3347ac9b9c15f68e8db2ec`.
- Started: `2026-08-31T18:10:58Z`.
- Finished: `2026-08-31T18:14:12Z`.
- All five steps succeeded: `mobile_delivery`, `omy_pre_pick_place`, `rb5_waam`, `omy_post_pick_place`, and `mobile_return`.
- Every dispatched command contained the exact adapter operation, `speed_scale=0.75`, a system-owned `bound_asset_id`, and `physical_output_connected=false`.
- Every Unity result reported `SUCCEEDED`, `output_isolation_confirmed=true`, and `physical_output_connected=false`.
- Reset control `simctl-406ef5cf87dd406f97da575d2357fb75` was acknowledged; the adapter returned to `ReadyDelivery` with no current run.

## Defect found during verification

The first run, `sim-172679b9c9534ae187b37f1de046bbc4`, failed because the Unity executor correctly rejected an undeclared system field, `bound_asset_id`. The contract was clarified so that the orchestrator may add that one read-only, non-empty binding identifier while every other undeclared parameter remains rejected. The Unity Player was rebuilt and the corrected run above completed successfully.

## Remaining scope

Behavior Tree conditions and fallback branches, 3D waypoint authoring, robot-local MoveIt/Tesseract planning, and physical robot execution remain intentionally outside v1. They require separate safety and commissioning gates rather than expansion of the current simulation-only editor.

## Non-blocking runtime warnings

The successful Linux Player still logs an Assimp `libdl` load exception for optional runtime model importing and a negative-scale `BoxCollider` warning for the generated XR START button. Neither warning interrupted the five-step run, but both should be removed before treating the Player log as warning-clean or using those specific runtime-import/XR collision paths in acceptance testing.
