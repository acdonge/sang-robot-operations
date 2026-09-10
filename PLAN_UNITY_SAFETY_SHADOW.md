# PLAN - Unity Safety Shadow Phase 0

Goal: Add a versioned, transport-neutral safety assessment contract and a command-free Shadow Mode reference path that Unity can later drive from a Processing PC.

Done when:
- The contract represents Task Run and asset identity, freshness, confidence, rule reasons, validity, and explicit command-free Shadow Mode semantics.
- A deterministic reference evaluator covers stale/invalid input, human-robot distance, forbidden-zone occupancy, and Task sequence mismatch.
- Central stores only the latest assessment plus bounded meaningful state/reason transitions in SQLite and exposes authenticated read APIs.
- Focused tests and the existing Fleet Control Plane suite pass without introducing robot, vendor, welding, or process command routes.

Out of scope:
- Unity scene creation or Unity-to-Central transport deployment.
- Physical Task Pause, safe-stop, E-stop, robot motion, or welding I/O.
- Human pose inference, camera calibration, and world-frame registration.
- Safety certification or replacement of the independent hardwired safety chain.

## Milestones
- [x] M1: Versioned safety snapshot, policy, and assessment models - verified by schema/model tests.
- [x] M2: Deterministic reference evaluator and JSON replay CLI - verified SAFE/WARNING/UNSAFE/UNKNOWN cases and no-command invariants.
- [x] M3: Additive SQLite latest/transition persistence - verified idempotency, transition deduplication, scope validation, and reopen behavior.
- [x] M4: Authenticated Central ingest/read API - verified role/scope handling and route-surface safety checks.
- [x] M5: Contract export, examples, architecture/user handoff documentation - verified checked-in schema matches generated schema.
- [x] M6: Focused/full regression and Phase-end review - verified 409 workspace tests, clean colcon build, diff review, and Fable PASS plus hardening fixes.

## Decisions log
- 2026-08-27: Phase 0 is Shadow Mode only. An UNSAFE or UNKNOWN result is evidence and a recommendation, never a command.
- 2026-08-27: Unity remains the eventual local scene evaluator; the Python evaluator is a deterministic reference/replay oracle, not a safety-rated runtime.
- 2026-08-27: SQLite keeps one latest row per Run/evaluator and at most 10,000 meaningful transitions rather than high-rate frame-by-frame samples.
- 2026-08-27: Central rejects assessed timestamps more than two seconds in the future and live assessments whose reporting Agent is not bound to a Task Run-assigned asset or Processing Node.
- 2026-08-27: MCAP/Object Storage remains the source for high-rate observations and incident media; safety rows contain references only.

## Current state
M1-M6 are complete. The focused safety suite passes 13 tests, Fleet Control Plane passes 169 tests,
the mounted full workspace passes 409 tests, and the package plus dependencies build in a clean
temporary colcon workspace. The Fable Phase-end review returned PASS and all three pre-live hardening
items were fixed and re-tested. No Unity project exists under the current workspace, so Unity
deployment inputs are documented for the user handoff rather than assumed.
