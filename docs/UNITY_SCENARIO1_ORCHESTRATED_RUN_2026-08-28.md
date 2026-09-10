# Unity Scenario 1 Orchestrated Run - 2026-08-28

## Result

The existing Unity `SampleScene` completed its full five-step Scenario 1 flow under the SANG G3
Cell Orchestrator. No manual START click was used after Play Mode began. The run finished
`COMPLETE`; every step and Agent command finished `SUCCEEDED`.

- Run ID: `iitp-s1-sim-20260828T180925Z-9c27f376`
- Execution mode: `SIMULATION`
- Unity Editor: `6000.5.6f1`
- Physical output connected: `false`
- Unity runtime errors in the final captured console window: `0`

## Executed workflow

| Order | Step | Unity completion phase | Completed at (UTC) |
|---:|---|---|---|
| 1 | `mobile_delivery` | `ReadyPrePickPlace` | `2026-08-28T18:09:32.9569650Z` |
| 2 | `omy_pre_pick_place` | `ReadyWaam` | `2026-08-28T18:10:21.7916000Z` |
| 3 | `rb5_waam` | `ReadyPostPickPlace` | `2026-08-28T18:11:18.4016720Z` |
| 4 | `omy_post_pick_place` | `ReadyMobileReturn` | `2026-08-28T18:12:02.0615850Z` |
| 5 | `mobile_return` | `Complete` | `2026-08-28T18:12:09.1791860Z` |

Unity and the file bridge agreed on the active step throughout the run. The WAAM capture showed the
RB5 following its configured path with `Safety Status: Safe`; the final capture showed
`Scenario 1 no-anomaly complete` and `DONE`.

## Safety boundary

This was an output-disconnected Editor simulation. `IITP_RB5_SIM_TO_REAL` and
`IITP_OMY_SIM_TO_REAL` were unset, every heartbeat and result declared
`physical_output_connected=false`, and no robot, welder, feeder, digital output, analog output, or
vendor controller command was enabled.

The Python side verified the signed SIMULATION grant, sublease, goal, capability allowlist,
idempotency, freshness, and result identity before accepting completion. Unity independently
enforced the scenario, phase order, capability allowlist, expiry, and physical-output boundary.

## Evidence

- Run summary:
  `runtime/unity-scenario1-orchestrator/iitp-s1-sim-20260828T180925Z-9c27f376/run_summary.json`
- Central journal:
  `runtime/unity-scenario1-orchestrator/iitp-s1-sim-20260828T180925Z-9c27f376/orchestrator.sqlite3`
- Agent ledgers: one SQLite file each for the mobile carrier, OMY, and RB5 simulation Agents in the
  same Run directory.
- Atomic bridge evidence: five command JSON files and five result JSON files under
  `/tmp/sang-unity-scenario1-orchestrated-g3-01`.
- Live WAAM capture:
  `runtime/unity-scenario1-orchestrator/evidence/rb5-waam-live.png`
- Completion capture:
  `runtime/unity-scenario1-orchestrator/evidence/scenario-complete.png`

The Orchestrator SQLite journal independently reported one `COMPLETE` Run, five `SUCCEEDED` steps,
and five `SUCCEEDED` command records in the expected Agent order.

## Verification

- Unity script compilation completed and `recompile_status` returned `idle`.
- The final Unity runtime console window contained zero `error` entries.
- The adapter profile in Unity `StreamingAssets` exactly matched the versioned SANG contract.
- Focused Orchestrator, Agent, Safety Shadow, Unity validation, contract, and store regression:
  `86 passed`.
- The bridge and Unity runtime both kept the physical-output declaration false.

## Remaining limitations

1. Meta XR opens an `Android SDK not Found` modal during Editor startup and the first Play Mode
   reload on this machine. It was dismissed because this run used desktop Play Mode, but unattended
   automation must launch in automated mode and remove or preconfigure this modal dependency.
2. Existing scene code emits non-fatal warnings when it sets velocity on a kinematic plate body and
   when Unity creates a partial convex hull for `link2_1`. Neither warning interrupted the workflow,
   but both should be cleaned before a production Player build.
3. The current synchronous Cell Orchestrator tick stores one logical decision timestamp for its
   step-state updates. The per-step Unity result files above carry the actual wall-clock completion
   times. A later asynchronous runtime should propagate Agent completion time into the Central
   journal.
4. This evidence covers an Editor simulation only. It is not a signed Player build attestation,
   G6b physical safety evidence, G7 physical motion authorization, or welding-process commissioning.
5. The generic XR HUD still shows `Path: Not Received` because that label tracks the separate
   SeoulTech path feed. The WAAM controller used its local versioned StreamingAssets trajectory and
   completed successfully. The two path sources should be named separately or the unrelated label
   should be hidden during Scenario 1 to avoid a misleading display.
