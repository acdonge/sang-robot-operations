# PLAN - Unity IITP Scenario 1 Integration

Goal: Register the supplied Unity Scenario 1 as a repeatable SANG Task and connect its derived
safety facts to command-free Safety Shadow evaluation.

Done when:
- A versioned bundle describes the Scenario 1 assets, roles, ordered workflow, policy, and READY
  simulation Task Run.
- Applying the bundle twice creates no duplicate registry objects.
- A loopback Edge endpoint validates `SafetySnapshotV1`, evaluates it with the reference oracle,
  and forwards only `SafetyAssessmentV1` to Central.
- The Unity project automatically emits snapshots without activating any robot, welder, feeder,
  Task Pause, or E-stop command path.
- Focused Python tests and the available workspace checks pass.

Out of scope:
- Physical robot or welding commands.
- Certified safety-stop behavior.
- Final cell/world calibration, real person tracking, and approved production thresholds.
- Installing or licensing Unity Editor on this PC.

## Milestones
- [x] M1: Scenario Task bundle and idempotent registrar - verified with API tests and live replay.
- [x] M2: Loopback Safety Edge evaluator - verified against the reference evaluator and no-command flags.
- [x] M3: Unity snapshot reporter and workflow adapters - verified for contract coherence and static guardrails.
- [x] M4: Register the READY simulation Task in the local Central instance - verified through the API.
- [x] M5: Run focused and workspace regression checks; document rollout limits.

## Decisions log
- 2026-08-27: Use a local Python evaluator rather than duplicating safety policy logic in C#.
- 2026-08-27: Register simulation assets separately from physical Lab1 assets to preserve provenance.
- 2026-08-27: Keep the first Run in READY/synthetic Shadow Mode; no physical control is introduced.
- 2026-08-27: Refuse Shadow reporting whenever an IITP OMY or RB5 Sim-to-Real arm flag is active.

## Current state
M1-M5 are complete. Central holds the READY Run and a synthetic SAFE assessment produced through
the live loopback Edge. Focused, Fleet, workspace regression, and clean package build checks pass.
Unity Editor is not installed on this PC, so Editor compilation and Play Mode remain attended evidence
for the next step rather than part of this command-free implementation iteration.
