# PLAN - Simulation Operations v1

Goal: Turn the proven Workflow Studio to Unity Scenario 1 path into a repeatable, interruptible, restart-safe simulation operation without adding any physical command sink.

Done when:
- Manager UI can request Hold, Resume, Abort, and terminal Reset with idempotent audited operations.
- Unity cooperatively pauses/resumes the active simulation, abort prevents later steps, and Reset returns the scenario to `ReadyDelivery`.
- Worker or Central restart never duplicates an accepted Unity command and reconciles the existing Run.
- Central exposes accurate per-step dispatch and completion timestamps from runtime evidence.
- A Linux Standalone Player build path exists for the simulation role and excludes legacy Sim-to-Real sources.
- Focused regressions, repeated fixture Runs, and one live Unity lifecycle exercise pass with `physical_output_connected=false` throughout.

Out of scope:
- Physical OMY, RB5, carrier, welding, wire-feeder, ROS command, vendor SDK, policy, XR actuation, or raw joint control.
- Safety-rated stop claims, E-stop replacement, G6b measurements, or G7 commissioning.
- Conditional Workflow editing, multi-asset cardinality, or Scenario 2 safety logic.

## Milestones
- [x] M1: Freeze lifecycle semantics and add durable control/timestamp contracts - verified by: store/API model tests and append-only audit assertions.
- [x] M2: Add Worker and Unity cooperative control/reconciliation - verified by: fake-bridge Hold/Resume/Abort/Reset and restart tests with no duplicate command.
- [x] M3: Add Workflow Studio lifecycle controls and quiet live status - verified by: frontend build and live browser interaction with zero console errors.
- [x] M4: Add isolated Linux Standalone Player build entry point - verified by: successful Linux build, artifact scan, manifest check, and physical-sender exclusion.
- [x] M5: Exercise the live Unity lifecycle and repeated Runs - verified by: Hold/Resume, Abort/Reset, restart reconciliation, full completion, ten fixture repetitions, and output-isolation evidence.
- [x] M6: Cross-check evidence and document operation - verified by: 29 latest focused and 278 full control-plane regressions, Player artifact scanning, diff checks, database integrity, and the evidence report.

## Decisions log
- 2026-08-31: `Hold` is a cooperative simulation pause and never represents an E-stop or safety-rated stop.
- 2026-08-31: `Abort` is terminal for the current Run; resuming an aborted Run is prohibited.
- 2026-08-31: `Reset` is accepted only for terminal Runs and creates no new Run. A later Queue action creates a new immutable execution record.
- 2026-08-31: Control intent is durable in Central and consumed by the Worker; the HTTP process never writes directly to the Unity bridge.
- 2026-08-31: The first standalone target is a visible Linux Player on the Processing PC with the same output-disconnected adapter contract.

## Current state

Completed on 2026-08-31. Simulation Operations v1 now provides durable audited lifecycle controls, restart reconciliation without duplicate accepted commands, runtime-derived per-step timestamps, a Workflow Studio operations surface, and an output-disconnected Linux Standalone Player.

The final live Run `sim-bd4a35105a684aafa719c55769f527dd` completed all five Scenario 1 steps and then acknowledged Reset back to `ReadyDelivery`. OMY step handoff now uses a measured 10-joint lock with a continuous one-second settled dwell, the Worker cancels an active Unity result wait on Abort, and the backend tolerates the bounded transient READY interval between phases. Central schema migration 18 and database integrity checks passed. The latest focused Python suite passed 29 tests, the full control-plane suite passed 278 tests, the compiled Player artifact scan passed with no findings, and the in-app browser reported no console errors.

Full hashes, live Run evidence, known non-blocking warnings, and the physical-output boundary are recorded in `docs/SIMULATION_OPERATIONS_V1_EVIDENCE.md`.
