# PLAN - Workflow Studio to Unity Scenario 1 Live Run

Goal: Execute the full five-step Unity Scenario 1 from the manager Workflow Studio queue without enabling any physical output.

Done when:
- The live Central database is backed up and migrated to schema v17.
- Unity 6000.5.6f1 runs `SampleScene` in orchestrated mode with both Sim-to-Real flags unset.
- A canonical immutable Scenario 1 Task/Workflow publication exists in the live Central database.
- The durable Unity Worker claims a UI-queued Run and all five steps finish `SUCCEEDED`.
- Workflow Studio, Central events, Worker runtime evidence, Unity bridge results, and Editor logs agree on `COMPLETE` with `physical_output_connected=false`.

Out of scope:
- Physical OMY, RB5, carrier, welding, wire-feeder, policy, XR, or raw joint commands.
- G7 physical commissioning and process-output wiring.
- Changes to Scenario 1 motion implementations.

## Milestones
- [x] M1: Back up and migrate live Central - verify by: backup integrity, schema versions 1-17, Workflow API health.
- [x] M2: Start Unity in output-disconnected orchestrated mode - verify by: fresh ready heartbeat on a new bridge root.
- [x] M3: Publish canonical Scenario 1 contract and start Worker - verify by: immutable publication and fresh matching adapter status.
- [x] M4: Queue from Workflow Studio and complete all five steps - verify by: browser flow, Run `COMPLETE`, five `SUCCEEDED` steps/results.
- [x] M5: Record and cross-check evidence - verify by: DB, bridge, runtime summary, Editor log, safety-boundary checks.

## Decisions log
- 2026-08-28: Use a new `/tmp/sang-unity-*` bridge root so stale demo commands/results cannot affect the live Run.
- 2026-08-28: Recreate only Central Admin/Manager plus the opt-in Unity Worker; preserve ingestion and physical read-only services.
- 2026-08-28: Publish a dedicated simulation Task rather than changing the immutable observation-only Shadow Task.
- 2026-08-28: Concurrent Admin/Manager startup briefly raced on the migration ledger; restart policy recovered after one process completed v17. Record this as a post-run hardening item rather than changing migration code during the live simulation.
- 2026-08-28: Preserve the completed Run, then restart Unity Play Mode to leave the adapter in `ReadyDelivery` for the next UI-queued simulation.

## Current state

M1-M5 are complete. Run `sim-c655f0dc30e5453596f904dab6338726` was queued through Workflow Studio and completed all five steps in 163.084 seconds. Central, Worker runtime state, five Unity command results, and the Editor log all agree on `COMPLETE`; every result records `physical_output_connected=false` and `output_isolation_confirmed=true`. The completed Run remains visible, while Unity and the Worker have been reset to `ReadyDelivery` so the immutable publication can be queued again. See `docs/WORKFLOW_STUDIO_UNITY_LIVE_RESULT_2026-08-28.md`.
