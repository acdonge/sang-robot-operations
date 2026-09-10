# Simulation Operations v1 Evidence

Status: PASS

Date: 2026-08-31

## Scope and safety boundary

Simulation Operations v1 operates only the output-disconnected Unity Scenario 1 adapter. It does not provide a physical OMY, RB5, carrier, welding, wire-feeder, ROS, vendor SDK, XR, or raw-joint command sink. `Hold` is a cooperative simulation pause, not an E-stop or safety-rated stop.

Both physical forwarding environment variables were absent during validation:

- `IITP_RB5_SIM_TO_REAL`
- `IITP_OMY_SIM_TO_REAL`

Every live Unity heartbeat and final Run result reported `physical_output_connected=false`.

## Delivered contract

- Central schema migration 18 adds durable simulation control records and restart reconciliation state.
- Hold, Resume, Abort, and terminal Reset are idempotent, audited operations consumed by the Worker rather than the HTTP process.
- Accepted Unity commands are persisted before blocking execution and are reused during recovery without changing their immutable identity or issue timestamp.
- A RUNNING lease expiry enters reconciliation instead of silently requeuing or duplicating the active command.
- Step completion times come from Agent runtime evidence.
- Workflow Studio exposes lifecycle state, reconciliation state, per-step timestamps, control availability, and control audit history.
- The Linux Standalone Player compiles with `SANG_SIMULATION_PLAYER` and substitutes disconnected bridge components for legacy Sim-to-Real senders.
- OMY completion locks the ten measured joint positions, requires one continuous second within the 10-degree error bound, and does not recapture a stale home target before WAAM. Every next-step command remains blocked until that lock is healthy.
- The Unity file backend tolerates the short READY transition after a step, while an Abort event cancels a Worker blocked on a Unity result so terminal Reset does not require a Worker restart.

## Automated verification

Focused Python regression command:

```bash
docker exec -w /ws \
  -e PYTHONPATH=/ws/src/fleet_control_plane:/ws/src/robot_agent \
  sang-phase3-lab1-central-admin-1 \
  python3 -m pytest -p no:cacheprovider -q \
  src/fleet_control_plane/test/test_unity_scenario_orchestrator.py \
  src/fleet_control_plane/test/test_workflow_authoring.py \
  src/fleet_control_plane/test/test_simulation_build.py
```

Latest focused result: 29 passed, 1 upstream Starlette deprecation warning.

The complete `src/fleet_control_plane/test` suite was then run with the repository package paths, including `src/sang_mcap_recorder`. Result: 278 passed, 1 upstream Starlette deprecation warning.

The first complete-suite pass found stale schema/API expectations and a real migration-recovery defect: a database with version 18 metadata removed but the new column already present attempted to add the column again. Migration 18 now obtains an immediate database lock, rechecks the migration version, detects the existing column, and creates its table and indexes idempotently. This also prevents concurrent Central process startup from applying version 18 twice. The affected migration recovery tests and the complete suite pass after the fix.

Additional checks:

- Ten independent full five-step fixture Runs completed with physical output disconnected.
- Admin production build passed with `npm run build`.
- Browser verification showed final Run `sim-bd4a35105a684aafa719c55769f527dd` as `COMPLETE / READY`, all five step timestamps, Reset audit status, and zero console errors.
- `git diff --check` passed.
- Central SQLite `PRAGMA quick_check`, `PRAGMA integrity_check`, and `PRAGMA foreign_key_check` returned `ok` with no foreign-key rows.
- Central schema migration table reports version 18.
- The pre-migration database backup is `runtime/phase3-lab1/central/fleet.before-simulation-operations-v1-20260831.db` and its integrity check passed before migration.

## Standalone Player evidence

Build output:

`output/unity-simulation-player/SangScenario1Simulation.x86_64`

The final Unity build log contains both `Build Finished, Result: Success` and `SANG_SIMULATION_PLAYER_BUILT`.

Artifact SHA-256 values:

| Artifact | SHA-256 |
| --- | --- |
| `SangScenario1Simulation.x86_64` | `78fd4776b65e70e3e0ac9e2daf7af26b8c94372247ceaf0dd4edf8b1bb4d4096` |
| `Assembly-CSharp.dll` | `2abc4827bea0ef99347f14390c0ba88b73352819ae6069cd2f0002dda67765be` |
| `SangScenario1Simulation.x86_64.simulation.json` | `012098f666bebc82ef16006bf2203cc653179ed307212961bcbada8e9bed2e78` |

The manifest declares `build_role=simulation_executor`, `compile_define=SANG_SIMULATION_PLAYER`, and `physical_output_connected=false`. The compiled Player artifact scan passed with no findings. It found no `Rb5SimToRealUdpClient`, `OmySimToRealUdpSender`, physical forwarding environment variable, socket-send, ROS command-topic, or vendor-command SDK pattern. The disconnected bridge and Scenario Agent types are present.

## Live lifecycle evidence

| Run | Exercise | Result |
| --- | --- | --- |
| `sim-f50400f95e4f463f93529badc3348e66` | Hold, Worker restart, Resume | Hold froze the active phase and Resume continued the same local command. This exercise exposed the old ACTIVE-return bug; the failed Central record is intentionally retained and the bug has a regression test. |
| `sim-49b7f5341f5d49748a17984e8279ad19` | Abort, Reset | Abort acknowledged, the active command became ABORTED, no later step was dispatched, and Reset returned Unity to Ready. |
| `sim-60085ec415b440f2ba184d727aec0954` | Worker restart reconciliation | The expired lease entered reconciliation, the Worker reclaimed the Run, and the accepted command count did not increase. The bounded exercise was then aborted and reset. |
| `sim-5b4f5457e06a41b89b44dd27cfd47145` | Full completion, Reset | All five steps succeeded, Central reached COMPLETE, and Reset was acknowledged back to ReadyDelivery. |
| `omyfix-20260831-runtime-regression` | OMY idle lock, input isolation, Reset | UDP 5055 stayed closed, mobile delivery and OMY pre pick-and-place succeeded output-disconnected, the 10-joint idle lock was reacquired, two 15-second READY comparisons scored SSIM 0.999294 and 0.998856, and Abort plus Reset returned Unity to ReadyDelivery. |
| `omyhardening-20260831-warningfix` | Related-error hardening and final deployment | The final compiled Player kept UDP 5055/5056/5057 closed, completed mobile delivery and OMY pre pick-and-place, reacquired the 10-joint idle lock, emitted zero kinematic-body velocity warnings, acknowledged Abort/Reset, and returned to ReadyDelivery. Post-reset idle SSIM was 0.999522 over 15 seconds. |
| `sim-76a73ad6436e410cb97ef9c9be8d9443` | OMY lock regression discovery | The retained failed Run exposed that step success could be reported before the OMY lock was stable. Readiness and step completion are now gated on the settled lock. |
| `sim-d6b637a053e54b758c471a3878719221` | Hold, Resume, Abort, Reset defect discovery | Hold froze the active phase, Resume continued it, and Abort blocked later steps. Its Reset exposed a blocked Worker result wait; the Abort event now cancels that wait. |
| `sim-032a26e65f0540889d778f974a2c83ba` | Abort and Reset regression | Abort and terminal Reset were both acknowledged without restarting the Worker, and Unity returned to ReadyDelivery. |
| `sim-e7cc096af7664e3b97c823253cccebda` | Transient READY race discovery | A retained failed Run exposed the short not-ready interval between Unity phases. The backend now waits for bounded fresh readiness instead of failing a one-shot check. |
| `sim-f24d19c72f414344a86615fa49a236ea` | OMY stale home-target discovery | A retained failed Run showed 11-13 degree drift when the old drive target was recaptured. OMY now locks its measured completion pose and observes a one-second position dwell. |
| `sim-bd4a35105a684aafa719c55769f527dd` | Final five-step completion and Reset | All five steps succeeded with zero OMY idle, kinematic-velocity, orchestrator-failure, or NullReference log entries. Reset was acknowledged and the canonical Player returned to ReadyDelivery. |

Final full Run timeline:

| Step | Runtime completion |
| --- | --- |
| `mobile_delivery` | `2026-08-31T16:47:49.775Z` |
| `omy_pre_pick_place` | `2026-08-31T16:48:39.769Z` |
| `rb5_waam` | `2026-08-31T16:49:36.060Z` |
| `omy_post_pick_place` | `2026-08-31T16:50:17.444Z` |
| `mobile_return` | `2026-08-31T16:50:24.563Z` |

The Run was created at `2026-08-31T16:47:42.603Z`, completed at `2026-08-31T16:50:24.563Z`, and retained its immutable COMPLETE result after terminal Reset. Reset `simctl-6f92b762c7f54f40846351ad2dd7e046` completed at `2026-08-31T16:50:50.492Z`. The validated build was then promoted to `output/unity-simulation-player` and restarted from that canonical path. Its heartbeat is `READY`, `ReadyDelivery`, with no active command, no current Run identity, and `physical_output_connected=false`.

The successful end-to-end Player log is `/tmp/sang-unity-final-measured-lock-live.log`, the exact Unity 6000.5.6f1 build log is `/tmp/sang-unity-final-measured-lock-rebuild.log`, and the currently running canonical Player log is `/tmp/sang-unity-final-canonical-live.log`.

## Operator entry point

Workflow Studio is available at `http://127.0.0.1:18091/workflow.html`. The current local Central admin, Workflow Worker, and Unity Standalone Player remain running. The Player is intentionally visible and output-disconnected.

## Known non-blocking warnings

- The Standalone Player logs an Assimp `libdl` lookup warning on this host. Scenario execution and lifecycle control are unaffected.
- The imported OpenXR settings contain missing Android-only script references and serialization-layout warnings in the Linux desktop Player. Desktop Scenario 1 execution is unaffected; package migration remains separate maintenance.
- Imported mesh assets log convex-hull and non-readable collision-mesh warnings. The current Scenario 1 regression passes, but collision geometry should be re-authored before claiming safety-grade contact fidelity.
- The XR HUD START button has a BoxCollider under a negatively scaled hierarchy. Remote orchestration is unaffected; XR pointer collider cleanup remains separate maintenance.
- The frontend build reports a chunk-size warning above 500 kB. It is a performance optimization item, not a functional failure.
- This evidence makes no safety-rated stop, physical commissioning, or G7 readiness claim.
