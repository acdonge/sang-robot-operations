# PLAN - Unity Scenario 1 Orchestrated Run

Goal: Drive the existing Unity Scenario 1 assets through the SANG G3 Cell Orchestrator without
enabling any physical robot, welder, or feeder output.

Done when:
- Scenario 1 is represented by a validated `WorkflowDefinitionV2` with five ordered action steps.
- A signed SIMULATION request crosses the Agent ledger and a loopback-only Unity bridge for each
  step.
- Unity accepts only the expected step, reports durable command results, and remains idempotent.
- The existing mobile carrier, OMY, and RB5 simulation assets complete the five-step flow without
  manual START clicks.
- The Orchestrator journal reports the Run and all five steps as `COMPLETE`/`SUCCEEDED`.

Out of scope:
- Physical OMY or RB5 commands and all Sim-to-Real UDP senders.
- Welding arc, wire feeder, vendor SDK, and process-output control.
- G6b physical safety evidence or G7 attended physical motion.
- Production Unity build attestation; this is an Editor simulation integration run.

## Milestones
- [x] M1: Persist Scenario 1 G3 workflow and bridge profile - verified by Pydantic validation and
  exact adapter-profile comparison.
- [x] M2: Add a durable Python Unity bridge backend and Runner - verified with a fake Unity responder
  and a complete in-process five-step Orchestrator run.
- [x] M3: Add Unity remote-step API and loopback bridge bootstrap - verified by Unity compilation and
  static output-isolation checks.
- [x] M4: Restart Editor in orchestrated mode and execute the full five-step workflow - verified by
  Unity result files, Orchestrator SQLite journal, and Editor log evidence.
- [x] M5: Run focused regression and document exact results and limitations.

## Decisions log
- 2026-08-28: Use a filesystem loopback bridge because the current Codex session exposes no Unity
  MCP tool and the Editor and Orchestrator container do not share a ROS 2 process. The bridge root is
  a mounted runtime directory, not an Asset directory.
- 2026-08-28: Python verifies signatures, grants, subleases, and idempotency before emitting a
  sanitized simulation command. Unity additionally allowlists step/capability pairs and rejects
  out-of-order commands.
- 2026-08-28: Keep the existing Unity motion implementations intact; only move phase ownership from
  the UI button to an explicit orchestrator API when the opt-in marker is present.
- 2026-08-28: Treat the Meta XR `Android SDK not Found` popup as an Editor automation issue, not a
  Scenario 1 failure. It was dismissed for this desktop Play Mode run; production automation must
  eliminate modal startup prompts.

## Current state

M1-M5 complete for the Editor simulation boundary. Run
`iitp-s1-sim-20260828T180925Z-9c27f376` completed all five steps without a manual START click. The
bridge contains five commands and five `SUCCEEDED` results, the Orchestrator SQLite Run is
`COMPLETE`, Unity reported zero runtime errors in the final console window, and
`physical_output_connected` remained false. The focused final regression passed (`86 passed`).
Exact evidence and remaining limitations are recorded in
`docs/UNITY_SCENARIO1_ORCHESTRATED_RUN_2026-08-28.md`.
