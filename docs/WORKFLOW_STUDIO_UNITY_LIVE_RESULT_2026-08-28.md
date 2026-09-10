# Workflow Studio to Unity Scenario 1 Live Result

Date: 2026-08-28

## Verdict

PASS. Workflow Studio queued the immutable Scenario 1 publication, the local Worker claimed it, and Unity completed all five simulation-only steps. No physical robot or process output was connected.

## Identity

- Task: `iitp-scenario1-coordinated-waam-simulation@1`
- Workflow: `iitp-scenario1-orchestrated-simulation@1`
- Workflow SHA-256: `0af120ca28d800e1094611e545b317e1f7181a863e2d9e073a952bec8fa17235`
- Adapter: `unity-iitp-scenario1`
- Adapter profile SHA-256: `61fdf3a5e0280b4c3b96b2ffc5a24a0923862e3485aa3e3dc7cc2ed7d406d5e6`
- Run: `sim-c655f0dc30e5453596f904dab6338726`
- Started: `2026-08-28T21:58:33.608Z`
- Finished: `2026-08-28T22:01:16.692Z`
- Duration: 163.084 seconds

## Step Result

| Order | Step | Unity completion | Result |
| --- | --- | --- | --- |
| 1 | `mobile_delivery` | `2026-08-28T21:58:40.672Z` | `SUCCEEDED` |
| 2 | `omy_pre_pick_place` | `2026-08-28T21:59:29.510Z` | `SUCCEEDED` |
| 3 | `rb5_waam` | `2026-08-28T22:00:25.811Z` | `SUCCEEDED` |
| 4 | `omy_post_pick_place` | `2026-08-28T22:01:09.518Z` | `SUCCEEDED` |
| 5 | `mobile_return` | `2026-08-28T22:01:16.631Z` | `SUCCEEDED` |

## Safety Boundary

- Unity was launched with `IITP_RB5_SIM_TO_REAL` and `IITP_OMY_SIM_TO_REAL` absent.
- The Editor confirmed `OMY sim-to-real sender is disarmed`.
- The Run result records `physical_output_connected=false`.
- All five bridge result files record both `physical_output_connected=false` and `output_isolation_confirmed=true`.
- The Worker container exposes only the file bridge and Central database; this run did not invoke ROS, vendor robot control, welding, feeder, XR intervention, policy, or raw joint command paths.

## Evidence

- Central Run state: `runtime/phase3-lab1/central/workflow-simulations/sim-c655f0dc30e5453596f904dab6338726/run_summary.json`
- Unity bridge: `/tmp/sang-unity-scenario1-workflow-live-20260828-01`
- Completed Editor log: `output/playwright/workflow-studio-unity-live/Editor-complete.log`
- Ready-state Editor log: `output/playwright/workflow-studio-unity-live/Editor-ready.log`
- Workflow Studio completion screenshot: `output/playwright/workflow-studio-unity-live/run-complete.png`
- Unity completion screenshot: `output/playwright/workflow-studio-unity-live/unity-scenario1-complete.png`
- Workflow Studio ready screenshot: `output/playwright/workflow-studio-unity-live/ready-for-next-run.png`
- Unity ready screenshot: `output/playwright/workflow-studio-unity-live/unity-ready-for-next-run.png`
- Pre-migration database backup: `output/workflow-studio-live/pre-v17-fleet-20260828T215206Z.db`
- Backup SHA-256: `a53a86100f6b23ec3d331551c4cdc53670418bdc6c4b40ef1e9b99c8c685750f`

The live database passed `PRAGMA integrity_check`, returned no foreign-key violations, and contains schema migrations 1 through 17. The append-only Run event ledger contains the `QUEUED -> RUNNING -> COMPLETE` sequence.

Focused regression verification also passed: `test_workflow_authoring.py` and `test_unity_scenario_orchestrator.py` completed with 16 tests passing. The three emitted warnings were one upstream Starlette deprecation and two expected read-only pytest cache warnings from the container mount.

## Current State

- Central Admin, Manager, ingestion services, and `unity-workflow-worker` are running.
- Unity is in Play Mode at `ReadyDelivery` with `ready=true` and `physical_output_connected=false`.
- Workflow Studio retains the completed Run and shows one available simulation adapter.
- `Queue simulation` is enabled again at `http://127.0.0.1:18091/workflow.html`.

## Non-Blocking Follow-up

- Admin and Manager can race when both attempt a new schema migration simultaneously. Restart policy recovered this run; migration ownership should be serialized before unattended deployment.
- Unity desktop startup shows Meta XR Android SDK/onboarding prompts when the Android module or headset runtime is unavailable. They do not affect this Linux simulation, but should be suppressed or preconfigured for unattended startup.
- Optional desktop plugins report missing `libdl.so` and `OVRPlugin`. Scenario motion completed despite these messages; resolve them before XR device validation or runtime mesh import is treated as an acceptance requirement.
- Workflow progress currently preserves one common step `updated_at` value. The bridge result files contain accurate per-step completion timestamps, but Central should copy those timestamps into step progress in a later hardening change.
- The only Workflow Studio browser console error was the missing optional `favicon.ico` resource.
