# PLAN - Multi-Robot Orchestrator and XR Control Design

Goal: Define an implementation-ready, safety-bounded orchestration platform for heterogeneous real and
simulated robots, using Scenario 1 and Scenario 2 only as the first acceptance fixtures.

Done when:
- The current Unity, Python bridge, ROS 2, Safety Shadow, Task Run, and XR boundaries are documented.
- A generic, versioned workflow graph and capability-based adapter contract can express Scenario 1,
  Scenario 2, and future multi-robot tasks without robot- or scene-specific Orchestrator code.
- Command ownership, leases, idempotency, recovery, safety gates, and human intervention are specified.
- Real-to-sim and sim-to-real use separate typed paths and cannot be enabled accidentally together.
- Manager XR and Edge XR roles, permissions, confirmation rules, and stale-input behavior are defined.
- Hardware and simulator instances are interchangeable runtime bindings only when their capability,
  units, frames, safety envelope, and completion contract are compatible and explicitly approved.
- A phased implementation sequence has acceptance gates before any physical motion is introduced.
- An independent Claude review is incorporated and remaining decisions requiring lab approval are listed.

Out of scope:
- Adding a physical robot, welder, feeder, mobile robot, Task Pause, or E-stop command path.
- Selecting final safety distances, speed limits, coordinate transforms, or electrical I/O channels.
- Hardcoding Scenario 1/2, OMY, RB5, Unity scene names, or current lab network addresses into the
  generic Orchestrator runtime.
- Claiming safety certification or replacing the independent hardwired safety chain.
- Installing Unity or running an attended physical commissioning test.

## Milestones
- [x] M1: Inventory the supplied Unity scenarios and current SANG control/safety/XR boundaries.
- [x] M2: Obtain an independent Claude architecture and hazard critique.
- [x] M3: Write the implementation specification and versioned workflow examples.
- [x] M4: Run an adversarial Claude review and revise all critical findings.
- [x] M5: Verify document consistency, links, safety invariants, and implementation traceability.

## Decisions log
- 2026-08-28: Central Manager owns intent, audit, and durable Task state; a Processing-PC Orchestrator
  owns execution sequencing; robot Agents own local motion and vendor adapters.
- 2026-08-28: Orchestrator commands are high-level typed steps, never raw joint or vendor commands.
- 2026-08-28: Unity is an adapter/digital-twin participant, not the authoritative production workflow
  state machine after migration.
- 2026-08-28: XR is treated as an authenticated human interface. It is not a safety-rated sensor,
  network E-stop, or direct actuator endpoint.
- 2026-08-28: Scenario 1/2 are conformance tests for the platform. New robot types and workflows must
  be added through manifests, capability contracts, workflow definitions, and adapters.

## Current state

M1 is complete. Local inspection found a C# single-scene Scenario 1 state machine, a JSON-driven
Scenario 2 human-proximity stop/resume demonstration, separate Python bridges for physical OMY/RB5,
command-free Safety Shadow, and input/preview-only Galaxy XR packages. Claude review and the detailed
implementation specification are next.

M2 is complete. The independent Claude Opus review confirmed the responsibility split and found
three release-blocking contract defects: producer-declared safety provenance can bypass live-only
authorization, Task bundle registration drops the workflow and safety policy, and run execution mode
is absent while an asset runtime kind can be changed in place. It also confirmed that Scenario 2's
current `AUTO STOP` only pauses Unity video and must remain evidence-only. M3 now incorporates these
findings into the generic implementation contract.

M3 is complete in `docs/MULTI_ROBOT_ORCHESTRATOR_IMPLEMENTATION_DESIGN.md`. The draft separates
runtime identity, Run execution authority, and twin data flow; defines generic capability/workflow,
Action, lease, recovery, safety, XR, provenance, and persistence contracts; and treats Scenario 1/2
only as conformance fixtures. M4 is the adversarial review of that concrete draft.

M4 is complete after two Claude Opus passes. The first adversarial pass returned FAIL and identified
undefined long-action lease renewal, Central/local-autonomy contradiction, missing durable Agent
fencing, Orchestrator incarnation ambiguity, teleop epoch collision, physical retry ambiguity, and
camera cardinality/reservation gaps. Version 1.1 closes those findings with delegated grants, local
sub-leases, durable ledgers/epochs, boot identities, ordered mode drain, capability-only multi-asset
binding, VLA demand arbitration, Unity build attestations, and explicit acceptance tests. The final
Claude gate returned PASS with no blockers. M5 performs repository-facing consistency checks and
publishes the document entry points.

M5 is complete. Markdown fences and all local links resolve; repository diff whitespace checks pass;
28 architecture/safety/recovery/extensibility traceability assertions pass; legacy ambiguous fields
and privileged camera step kinds are absent. README and `docs/requirements.md` now link this contract
while explicitly preserving zero physical command sinks until G7.
