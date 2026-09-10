# A1 Unity Inventory Integration - 2026-09-09

## Plan

Goal: integrate stable, validated identity bindings for the actual Scenario 1 scene,
preserving output-disconnected legacy execution and existing motion trajectories.

Write scope: `unity/Sang.Observer`, the actual Unity project, new runtime examples
under `contracts/assets/examples`, and this evidence document. No Python/config,
physical robotics operations, fixture replacement, or user Editor termination.

- [x] M1: inspect actual scene; freeze v2 bridge fields with parent.
- [x] M2: install Observer; verify stable object/joint/frame/controller/reset/outcome references.
- [x] M3: build existing isolated Linux Player; generate real package/manifest digests.
- [x] M4: verify inventory rejection, fresh v1/v2 behavior, unchanged motion, reset inventory and isolation. Repeat-action-after-reset motion qualification remains explicitly open.

## Interface Decision

Legacy schema 1 commands remain fixed-profile. Schema 2 uses the same command/run
correlation fields plus `unity_binding_id`, `controller_component_binding_id`,
versioned catalog `action_key`, `scene_manifest_sha256`, and `action_source_sha256`.
Schema 2 results echo all five fields. No type-name or arbitrary method dispatch.

Heartbeat adds `scene_inventory` (UnitySceneInventoryV1), `scene_manifest_sha256`,
`inventory_valid`, `inventory_errors`, and `action_bindings` with stable selectors,
catalog action key, capability/operation, legacy step metadata, and `source_sha256`.

Manifest/package hashes must match `contract_payload_sha256`: complete Pydantic
JSON defaults, ordinal sorted keys, compact separators, SHA-256 UTF-8 bytes.

## Current State

Read root/project AGENTS, requirements, README and A1 design. No Unity-subtree
AGENTS found. Actual project has no Git repository; original touched files and
motion sources are preserved in `Builds/A1Evidence/baseline`. Existing v2 workspace
dirty changes remain untouched. No Unity Editor/Player was running at intake.

Actual carrier is a kinematic Rigidbody, not an ArticulationBody. Its binding must
identify a logical translation DOF and never claim a physical joint. The OMY URDF
camera mount is geometry, not automatically a render Camera.

## Final Build Identity

Actual project root:
`/home/sa08050/app developer/unity_IITP/unity_IITP/100.Whole_integration/Scenario1_app`

Final Player: `Builds/A1PlayerFinal/SangScenario1Simulation.x86_64` under that root.
This supersedes the unqualified `A1Player` and `A1PlayerV2` candidates. Do not mix
their earlier manifest hashes with this build.

| Artifact | SHA-256 / Value |
|---|---|
| Scene manifest canonical SHA-256 | `4fa52c13e174ba2f7072385575dcd3feeaa862042f27d9826436a7ede9fec05b` |
| Source ZIP SHA-256 | `1e336f095170e049e963af0452b96f50165571fff24add7876868eae37ef61f8` |
| Source ZIP byte count | `78785505` |
| Actual `Assembly-CSharp.dll` | `531e5b3fe0b8e30ad46c898c3c50aa56186eda89e67e198546e4fe4c36133c2e` |
| Actual `Sang.Observer.dll` | `ec4e2ac4b8f44e1465b67ccd3356f9d1d2480268c582f72c3fb8c76e1397a0be` |
| Scene identity | `iitp-scenario1`, scene version `2`, manifest schema `1` |
| Unity / fixed timestep | Actual `6000.5.6f1` / `0.02` seconds |

The runtime manifest is
`contracts/assets/examples/iitp-scenario1-runtime-scene-binding.v1.json`.
Seven profile-specific package documents and the exact reset/outcome/binding/build
reference payloads are in `contracts/assets/examples/scenario1-runtime-20260909/`.
The original A0 fixture examples were not overwritten.

The immutable source ZIP is the real
`Builds/A1Evidence/iitp-scenario1-source-package.zip`, with file index
`Builds/A1Evidence/source-files.json`. It includes the actual scene dependency
closure, scene scripts/streaming assets, project/package settings and Observer
sources. The generated Resources descriptor is derived from this snapshot rather
than included in the ZIP, avoiding a self-referential manifest digest. All seven
packages reference the same complete Scenario 1 source ZIP;
these are not extracted reusable A3 prefabs. Frame/device references describe the
local A1 binding inventory, not commissioned physical device manifests.

These bytes are local, not uploaded to Central Object Store. The build-recipe and
isolation references are source/build evidence, not signatures or a PASS
SimulationQualificationV1. A separate Object Store registration and qualification
gate remain necessary before claiming signed/published package promotion.

## Resolved Inventory

| Stable Binding | Runtime Asset | Verified References |
|---|---|---|
| `scenario1.omy.material-handler` | `iitp-s1-omy-sim` | 6 arm + 4 gripper ArticulationBodies; base/flange/tool; typed controller |
| `scenario1.rb5.waam` | `iitp-s1-rb5-sim` | 6 ArticulationBodies; base/flange/TCP; typed controller |
| `scenario1.mobile.carrier` | `iitp-s1-mobile-sim` | Rigidbody `carrier_translation` logical DOF; base/tray attachment; typed controller |
| `scenario1.camera.overview` | `scenario1_overview_camera` | Actual Main Camera component and camera frame, one RGB render sensor |
| `scenario1.omy.camera-mount` | `scenario1_omy_camera_mount` | Actual URDF camera-link frame; geometry only, no invented video stream |
| `scenario1.environment.omy-table` | `scenario1_omy_table` | Actual OMY table transform |
| `scenario1.environment.rb5-table` | `scenario1_rb5_table` | Actual RB5 table transform |
| `scenario1.environment.cell` | `scenario1_cell` | Existing cell layout frame |
| `scenario1.workpiece.base-plate` | `scenario1_base_plate` | Runtime-created plate transform |

Total: nine assets, fourteen frames, sixteen real articulation joints plus one
explicit kinematic carrier DOF, three bound controllers and one render sensor.
Controller IDs append `.controller` to their corresponding robot binding IDs.
The typed `SangScenarioBoundController` exposes `IsReady`, `DidSucceed`, `DidFail`,
`HasFault`, and whole-scene `TryReset(out string)`. Its bound action route calls
the existing registered action implementation; legacy v1 results continue to use
the original action state path.

Stable metadata components are installed after the existing runtime bootstrap.
Unity-only initial selectors require unique URDF links/articulation components,
not merely matching visual-mesh names. Once installed, inventory follows live
references and survives hierarchy renaming (tested). Renaming an authored scene
before initial installation requires updating the compiled installation catalog;
this work does not claim an arbitrary scene/prefab loader.

## Developer

### Summary

Installed the local Observer package and connected actual live references to its
validated inventory. Added the compiled Scenario 1 binding/action catalog, typed
controller facade, deterministic package builder, negative tests and additive v2
dispatch. `step_id` is correlation-only for v2. Existing catalog action refs and
source hashes are exact, and results echo all five binding provenance selectors.
Clean v2 parameters contain operation/speed only; an optional legacy
`bound_asset_id`, when present, must match the already selected asset.

### Files Changed

- `v2_ws/unity/Sang.Observer/Runtime/SangAssetBinding.cs` and `SangSceneInventory.cs`.
- `v2_ws/unity/Sang.Observer/Tests/runtime_bridge_test.mjs`.
- Unity `Packages/manifest.json` and generated `packages-lock.json`.
- Unity `Assets/Scripts/SangUnityScenarioAgent.cs`, `SangUnityPreviewPublisher.cs`.
- Unity `Assets/Scripts/SangScenarioBindingCatalog.cs`, `SangScenarioBoundController.cs`, `SangScenarioInventoryIntegration.cs` (new).
- Unity `Assets/Scripts/XrMobileBasePlateCarrier.cs`: one read-only `IsSceneBindingReady` property only.
- Unity `Assets/Editor/SangSimulationPlayerBuild.cs`; new `SangA1PackageBuild.cs`, `SangA1InventoryTests.cs`, `SangA1SceneInspection.cs`.
- Generated Unity `Assets/Resources/SangA1/runtime.json` and Unity `.meta` files for added assets.
- New runtime manifest/package/reference examples listed above, and this document.

### Commands Run

Unity batch entrypoints: `SangA1SceneInspection.Inspect`, then existing
`SangSimulationPlayerBuild.BuildLinux` with physical-output environment flags
unset and the `SANG_SIMULATION_PLAYER` build define retained. Builds used the
actual project, not a stub C# compiler or copied mock scene.

Runtime harness: `node unity/Sang.Observer/Tests/runtime_bridge_test.mjs` with
an explicit isolated Player path, a new private evidence directory, and selected
`1`, `2`, `reset` or combined stress mode. The harness starts/stops only its own
Player process and uses process-local `SANG_UNITY_BRIDGE_ROOT`; it never modifies
the shared marker or the parent supervisor bridge.

Python read-only checks imported the existing contract models,
`contract_payload_sha256`, `validate_unity_inventory` and
`scan_unity_player_artifacts`. No Python/config files were edited.

### Test / Build Results

- Actual final Unity build: exit 0, `Builds/A1Evidence/build-6.log`.
- Twelve actual Unity reference tests PASS: `Builds/A1Evidence/inventory-tests.json`.
- Manifest and seven packages validate with full Pydantic defaults; canonical hashes and source ZIP size/hash match exactly.
- Actual Player inventory validates against the manifest in Python.
- Both compiled role assemblies pass the existing forbidden-sender artifact scan with zero findings.
- Focused canonical-container regression: `test_asset_contracts.py`, `test_aas_projection.py`, and `test_simulation_build.py`: 24 passed (one upstream Starlette/httpx deprecation warning). The first invocation omitted the `robot_agent` import path and was corrected using the full workspace PYTHONPATH.
- `Builds/A1Evidence/build-verification.json` contains exact identity/hash checks and byte-preservation results.
- `SampleScene.unity`, `Scenario1_noAnomaly.cs` and all existing `SangActions/*.cs` remain byte-identical to intake copies. Carrier movement method bodies are unchanged; its only source addition is the read-only readiness property.
- Runtime rejected wrong scene/action digests, wrong asset/controller/action/capability, physical mode/output, expired deadline, cross-asset parameters, unsafe phase reorder, and schema downgrade. Correlation-only step names execute successfully.
- Final reset smoke restored all nine references before any action.
- Fresh legacy v1: all five actions SUCCEEDED, terminal reset restored identical nine-asset inventory; 19 checks PASS in `Builds/A1Evidence/runtime-legacy-fresh/runtime-tests.json`.
- Three consecutive inventory-only resets: all nine bindings restored after every reset, no action auto-started; 16 checks PASS in `Builds/A1Evidence/runtime-reset-only/runtime-tests.json`.
- Fresh binding-key v2: all five actions SUCCEEDED with arbitrary correlation step IDs and clean operation/speed-only parameters; result provenance echoes matched; terminal reset restored identical nine-asset inventory. 19 checks PASS in `Builds/A1Evidence/runtime-dynamic-fresh/runtime-tests.json`.
- Parent-reported independent UI acceptance: `sim-95bf4a8aef1a433e8be1e865f0797da5` completed all five steps in 162.034 seconds against final scene `4fa52c...fec05b`. Parent owns the UI/worker/profile evidence and reported 358 Fleet tests passing; those are not counted as this workstream's private tests.

### Open Issues

The unchanged OMY IK guard rejected action replay after reset on two candidate
stress runs (`0.209m` and `0.207m` against the existing `0.20m` bound). Inventory
remained valid and the result correctly reported FAILED. Cause has not been
isolated to baseline versus runtime timing; do not claim deterministic repeat-run
qualification. No trajectory, IK threshold or physical output code was changed
to suppress these failures. Candidate evidence is retained in
`Builds/A1Evidence/runtime-final/` and `runtime-verified/`.

Existing Unity content warnings include convex-hull limits, unreadable collision
meshes and optional Assimp `libdl` loading in the headless Player. These do not
constitute certified collision/safety evidence. No camera or environment geometry
was added or modified by this work.

## Evaluator

### Verdict

PASS for scoped A1 inventory/package integration and binding-key dispatch on the
existing output-disconnected Player. This is not deterministic repeat-run motion
qualification or a signed asset promotion.

### Summary

All nine required assets resolve with live references; the actual project builds;
invalid binding/source/scene/physical requests fail closed; both fresh v1 and v2
five-action workflows complete; terminal and repeated inventory-only resets
re-establish the exact same inventory. Verification comprises 12 Editor reference
checks, 19 fresh v1 runtime checks, 19 fresh v2 runtime checks, 16 reset runtime
checks, 24 focused Python tests, and exact source/package/assembly hash checks.

### Critical Issues

- None for the scoped binding/dispatch acceptance or output-disconnection boundary.

### Non-Critical Issues

- Action replay after Reset can fail the unchanged OMY IK guard; cause remains unisolated. Deterministic repeated-motion qualification remains blocked by that separate issue.
- Local source/package metadata has not been uploaded to Central Object Store or signed/promoted; A3 extraction, A4 observation parity and physical commissioning are not implemented here.
- Existing collider/importer warnings prevent any claim of certified geometric or physical safety validation.

### Revision Instruction

No further A1 source edits required for this scope. Investigate OMY post-reset
repeat reliability separately without weakening the IK guard, and register/sign
exact source artifacts only through the parent-controlled package/qualification
workflow. The parent owns final cross-workstream review; no Claude call was made
by this workstream.
