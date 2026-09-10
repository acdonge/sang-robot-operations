# Unity External Workflow UI-Only Evidence

Date: 2026-08-31

## Result

PASS. The standalone Scenario 1 simulation renders the Unity scene assets but exposes no Unity/XR Canvas, overlay, button, pointer, or direct-input control surface. The SANG web Workflow UI queued and completed the full five-step task, then reset Unity to its initial ready state.

This change applies only to builds compiled with `SANG_SIMULATION_PLAYER`. The Unity Editor, Galaxy XR, and Meta Quest paths are intentionally unchanged.

## Runtime boundary

The simulation bootstrap removes existing Canvas, EventSystem, XR pointer, XR scenario controller, and overlay objects. It creates no visible Unity UI. A renderless compatibility state object remains because the existing Scenario 1 scripts exchange phase state through `Text` and `Button` references; it has no Canvas, EventSystem, raycaster, visible geometry, or user input path.

The runtime emitted:

```text
SANG_EXTERNAL_WORKFLOW_STATE_READY | removed_canvases=0; visual_components=0; direct_input=0.
SANG_EXTERNAL_WORKFLOW_UI_ONLY | Scene assets connected to the web Workflow UI; no Unity UI or direct input created.
```

The build manifest declares:

```json
{
  "build_role": "simulation_executor",
  "physical_output_connected": false,
  "compile_define": "SANG_SIMULATION_PLAYER",
  "control_surface": "external_workflow_ui",
  "unity_ui_visible": false
}
```

## Build and isolation

- Unity version: `6000.5.6f1`.
- Player: `output/unity-simulation-player-web-only-v2/SangScenario1Simulation.x86_64`.
- Build result: `Success` with `BuildOptions.StrictMode`.
- Compiled role assembly SHA-256: `0d988cd16c0cbf05b4796c3b014bb49179546dff1b4e587248e291fd0b698bb8`.
- Compiled artifact isolation scan: passed with no findings.
- Current heartbeat: `READY`, phase `ReadyDelivery`, no active command, and `physical_output_connected=false`.

## Browser-only task execution

Run `sim-6b3d9ced93e14ceea5945deb75fb258e` was queued from `workflow.html`. No Unity control was used.

| Step | Result |
| --- | --- |
| `mobile_delivery` | `SUCCEEDED` |
| `omy_pre_pick_place` | `SUCCEEDED` |
| `rb5_waam` | `SUCCEEDED` |
| `omy_post_pick_place` | `SUCCEEDED` |
| `mobile_return` | `SUCCEEDED` |

The Central result is `COMPLETE` with `physical_output_connected=false`. Hold and Resume were acknowledged during `PostPickPlace`; Reset was acknowledged after completion and returned the Unity heartbeat to `ReadyDelivery`.

## Regression checks

- Fleet Control Plane: `280 passed`, one dependency deprecation warning.
- Admin UI: TypeScript and Vite production build passed.
- Repository whitespace validation: `git diff --check` passed.
- Central health endpoint returned `ok`.

## Known non-blocking issue

The Player logs one `DllNotFoundException: libdl` from the optional Assimp runtime model importer. It did not affect scene loading, orchestration, task completion, reset, or the output-isolation scan. The failed Run created while replacing the Player during development remains in Central as intentional audit history; the latest run above is the final acceptance evidence.
