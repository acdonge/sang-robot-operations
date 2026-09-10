# PLAN - G1 through G6 Orchestration Runtime

## Planner
### Goal
Implement the software path from Central orchestration integrity through output-disconnected motion
readiness, using the frozen G0 contracts and preserving the rule that no physical robot or process
output may be reached before its later commissioning gate.

### Scope for this iteration
This iteration implements G1 through G6 as software, simulation, replay, and disconnected-HIL
capabilities. It adds durable registries, reservations and run state, Safety Evidence v2, a local
simulation Orchestrator, signed delegation and lease validation, Unity/Isaac simulation adapters,
an independent local Arbiter, XR intervention state, and a command-capable Agent skeleton whose
only controller is an output-disconnected mock.

It does not commission a physical robot, connect a vendor command SDK, enable a ROS command topic,
energize motion, or expose welding/wire-feeder outputs. G6 readiness evidence remains explicitly
`UNVERIFIED` wherever lab measurements or safety-owner approval are required. A manifest with
`command_sink_present: true` is rejected in this iteration.

### Tasks
1. G1: persist immutable workflows, policies, manifests, bindings, and Runs; derive provenance;
   enforce capability/cardinality eligibility and compatible resource reservations; migrate replay,
   immutable runtime identity, and extended Run states.
2. G2: evaluate and persist Safety Snapshot v2 using trusted measured-clock context,
   boot/sequence ordering, per human/asset separation rules, required confidence/calibration rules,
   and fail-closed UNKNOWN/UNSAFE holds.
3. G3: add ROS 2 interface definitions and a durable simulation-only Cell Orchestrator with fake
   Agents, idempotent command ledgers, signed grants/subleases, expiry, restart reconciliation, and
   deterministic DAG execution.
4. G4: isolate Unity observer/simulation/fixture roles, add simulation capability adapters for
   Unity and Isaac, and add build scanning plus signed attestation generation. Legacy direct
   sim-to-real senders must not be reachable from accepted builds.
5. G5: add key rotation, local identity checks, a separate demand Arbiter, bounded PolicyDemand,
   XR session/deadman/focus/network latching, role checks, and two-person resume approval.
6. G6: add an output-disconnected Device Agent skeleton, mandatory fail-behavior and timing
   evidence, controller mock/HIL fault injection, and a machine-readable readiness report that
   cannot authorize G7.
7. Run focused and full regressions, scan for command paths, obtain independent Claude reviews,
   and document all remaining lab-owned evidence.

### Acceptance Criteria
- Immutable registry content is idempotent by digest and rejects same-version mutation.
- A Run pins execution mode, workflow, binding, and resource reservations; mode/runtime identity
  cannot change after readiness.
- Shared-read resources can coexist, while exclusive assets, coordinated-group members, zones, and
  process resources conflict atomically.
- Safety input provenance is derived from authenticated binding and cannot be supplied by the
  producer; missing confidence/calibration/clock quality, stale data, or sequence regression yields
  UNKNOWN and HELD.
- Simulation workflow execution is durable and idempotent; lost replies and restart enter
  RECONCILING and query before any redispatch.
- Lease epoch, holder identity/incarnation, expiry, renewal guard, signature, capability, mode, and
  envelope fences are enforced.
- Unity/Isaac accepted simulation builds/adapters contain no physical sender, vendor command SDK,
  process-output path, or direct robot endpoint.
- XR loss disables intervention; UNSAFE/UNKNOWN resume requires both Edge Operator and Manager
  approval and never claims an E-stop.
- G6 accepts only output-disconnected HIL, requires explicit fail behavior and measured timing
  evidence, keeps `command_sink_present: false`, and reports lab-owned items as unverified.
- Existing observation, recording, catalog, OPC UA, and read-only robot behavior remains unchanged.

### Risks / Notes
- The Unity project is not currently a Git repository. Build provenance can be implemented and
  tested, but a production attestation cannot be issued until the user-owned private repository has
  a pinned commit and release key outside the checkout.
- Physical stop time/distance, hardwired safety-chain behavior, canonical cell calibration, and
  named safety approval cannot be established by software. They remain blocking evidence for G7.
- The current Unity scene and frame are provisional and are not credited for machine safety.
- Existing RB5/OMY sim-to-real source files are treated as prohibited legacy code. They must be
  excluded from every accepted G1-G6 build; they are not a supported command path.
- The dirty worktree contains user-owned work. No unrelated change may be reverted.

### Developer Instruction
Reuse the strict G0 models and current SQLite/FastAPI/security patterns. Keep the new runtime in
small gate-owned modules and migrations, add tests before advancing each gate, and reject rather
than default any safety-critical value. Never instantiate a physical controller or process-output
sink. Finish with source/build scans and an independent review.

## Milestones
- [x] M0: Baseline, Claude gap audit, and implementation plan.
- [x] M1: G1 Central integrity and reservation/state migrations.
- [x] M2: G2 Safety Evidence v2 evaluator and ingress.
- [x] M3: G3 simulation Orchestrator, ROS contracts, fake Agent, and durable ledger.
- [x] M4: G4 Unity/Isaac adapters, build isolation, scans, and attestations.
- [x] M5: G5 signing/key rotation, Arbiter, policy stream, XR, and RBAC.
- [x] M6: G6 output-disconnected Agent/HIL harness and readiness evidence.
- [x] M7: Full regression, independent review attempt, documentation, and gate evaluation.

## Gate Status

| Gate | Status | Closure rule |
|---|---|---|
| G1 | SOFTWARE COMPLETE | Central DB/API/source tests pass; production observer attestation still needs pinned Unity source. |
| G2 | SOFTWARE COMPLETE | Fail-closed evaluator, trusted ingress, ordering, and HELD transitions pass. |
| G3 | SOFTWARE COMPLETE | Simulation DAG, signed grant/sublease, renewal, expiry, restart, and reconciliation pass. |
| G4 | SOFTWARE COMPLETE | Isolated observer package, backend isolation, source/Player scanners, and fixture attestations pass; production attestation awaits pinned Unity source and real role Player artifacts. |
| G5 | SOFTWARE COMPLETE | Signing/key rotation, split-brain, staleness, XR-loss, Arbiter, and RBAC tests pass. |
| G6a | SOFTWARE COMPLETE | Disconnected-HIL software/fault tests pass; report remains non-authorizing. |
| G6b | LAB EVIDENCE REQUIRED | Dated measurements, physical isolation, safety-chain evidence, and named approvals. |
| G7 | OUT OF SCOPE | Requires lab measurements, named approvals, and attended motion. |
| G9 | OUT OF SCOPE | Welding and wire-feeder outputs remain disconnected. |

## Independent Gap Audit

Claude Opus completed a read-only audit before implementation. It confirmed the G0 contracts are
sound and identified three conditions that are now explicit in this plan:

1. Missing required confidence and producer-declared clock quality in the deployed v1 Shadow path
   can currently produce a false `SAFE/1.0`; confidence/clock honesty is therefore fixed before the
   broader G2 evaluator.
2. The Unity observer split needs an interface seam because scenario and WAAM controllers directly
   reference the two UDP sender classes. Excluding only the sender files would not compile.
3. G6 is split into software G6a and lab-owned G6b. G6a never authorizes G7.

The audit also confirmed that the existing OPC UA, Robot Agent, and read-only adapters have no
command method/write path. It flagged the legacy Unity UDP senders and XR demand topics as latent or
live command-shaped paths; accepted G1-G6 builds must exclude or arbitrate them.

## Current State

G1 through G6a are implemented. Central registry/reservations, Safety Evidence v2, the local
simulation Orchestrator and durable Agent ledger, Unity/Isaac simulation adapters, build scanning,
key rotation, Demand Arbiter, XR latches, dual approval, and the disconnected Device Agent all have
focused passing tests. ROS 2 Jazzy generated both interface packages, the non-XR regression passed
481 tests, and the three XR suites passed 58 tests. Claude Opus completed the pre-implementation gap
audit; the requested final Fable review was blocked by exhausted credits and the Opus fallback by
its session limit, so that post-implementation review remains pending rather than being claimed.
Production Unity attestation and all G6b physical evidence remain intentionally blocked.
