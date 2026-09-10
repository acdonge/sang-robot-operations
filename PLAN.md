# PLAN - GitHub source checkpoint and platform concept

## Planner
### Goal
Publish the SANG source checkpoint and an accurate explanation of the extended
OPC-UA-based robot operations platform discussed with the user.
### Scope for this iteration
Documentation, source publication hygiene, local commit and an authorized GitHub
destination. No hardware, simulator, deployment, new runtime feature or history rewrite.
### Tasks
1. Inspect repository history, source ownership, secrets, generated and external data.
2. Document the product concept, implemented slices, evidence and remaining limits.
3. Check the exact publish tree, create a local commit, upload and verify its remote SHA.
### Acceptance Criteria
- [x] P1: one concept overview links to the canonical roadmap and dated evidence.
- [x] P2: staged source excludes private state, media, installers and unapproved assets.
- [x] P3: documentation links, focused checks and independent review pass.
- [x] P4: the user-selected GitHub destination contains the verified commit.
### Risks / Notes
The user approved the new PRIVATE repository `acdonge/sang-robot-operations`.
The empty remote has been created and its private visibility verified.
Do not treat simulation success as OPC UA certification or physical commissioning.
### Developer Instruction
Keep the existing implementation and history. Use an explicit reviewed source list,
not a blanket add of the workspace. Preserve excluded files locally.
### Current State
2026-09-10: canonical overview and status corrections passed independent review.
Publication hygiene checked 555 historical blobs and pinned external dependencies.
Exact staged-tree checks passed. Fresh checks: 400 simulation and 754 ROS/backend
tests, 13 frontend selection tests, 15 rebuilt-UI tests, frontend typecheck/build
and 162 local documentation links. Source checkpoint `ef50bbc` was uploaded to
`acdonge/sang-robot-operations` on `master`; remote SHA matches local and GitHub
reports PRIVATE visibility. Live services and device execution are unchanged.

### Developer
Published the existing source/history with an accurate concept overview, canonical
status corrections, source-only exclusions and fresh test evidence. Rebuilt the
exported frontend without modifying live UI output. Sensitive/generated local
files remain on disk and are not part of the new source tree.

### Evaluator
PASS for documentation and private source publication. Exact remote source SHA
`ef50bbc29e9222bb0e3df4ff9ba08c150c6c199b` verified after push. ACT-C01, official
projection qualification, external asset provenance and physics/physical-safety
gates remain open. See `docs/GITHUB_CHECKPOINT_2026-09-10.md` for exclusions and
non-critical dependency/build/formatting warnings.

---

# Completed - Isaac Scenario 1 / Unity observer expansion

## Planner
Goal: execute the existing five-stage Scenario 1 using Isaac as the only virtual
device authority and a separate passive Unity multi-asset observer through the
existing Web Task queue. Reuse recorded OMY/RB5 trajectories, source cell layout
and immutable Workflow contracts; do not reproduce the old free-running Unity IK.

Scope: simulation-only carrier delivery, OMY transfer to table, RB5 path with no
arc, OMY return to carrier, carrier return. Carrier/plate attachment is explicitly
a simulation fixture if no drive/grasp physics asset is available. Never represent
a fixture as validated mobile dynamics, grasp force or physical safety.

### Tasks / Acceptance Criteria
- [x] S1: inspect and pin source layout, robot models and recorded motions; check
  names/units/frames/ranges and source hashes before loading.
- [x] S2: compose isolated Isaac cell; run measured-joint trajectory playback and
  object ownership predicates with single-authority, permit/cancel/reset gates.
- [x] S3: passive Unity observer mirrors all assets from ROS-derived snapshots;
  actual Editor build, rejection tests and coordinate/pose correspondence checks.
- [x] S4: existing Workflow UI publishes/executes five stages; fresh end-to-end
  complete/reset/repeat evidence with camera and object/joint telemetry.
- [x] S5: independent review, focused regressions, document exact qualified
  behavior and any remaining physics, transport, camera or safety limitations.

### Risks / Developer Instruction
Preserve original Unity project/vendor USD, old RB5 pilot and every physical
read-only path. Keep domain 98 localhost, a distinct runtime root/session and fixed
allowlists. No arbitrary uploaded scripts, welding I/O, real robot commands or new
frontend theme. Do not mark a timer or commanded pose as measured task success.
Recorded trajectory calibration must match the composed scene or execution blocks.

### Current State
2026-09-09: previous one-robot pilot passed; original OMY recorded pre/post segments
and RB5 WAAM joint path found. Separate multi-asset observer/layout task delegated;
main owns Isaac composition, measured execution and live verification. Backend
profile/worker integration retains prior schema/digest compatibility. Candidate 1
completed all five stages and Reset (231.026 s including a Hold/Resume check).
Cross-review found and fixed held-state ownership transitions, old-pose abort
targets, attachment incoherence on Reset, and non-finite geometry acceptance.
Final candidate completed two Web-started five-stage Runs in the same Isaac
session: `sim-18613c9242cf49898cbdfbccb96a7d5f` (202.463 s) and
`sim-9ed2fc38441a40ad9273abab89d06315` (224.164 s including Hold/Resume).
Both Reset acknowledgments and gate v3 validations passed. Unity observation
probes passed with no sample failures. Details and declared fixture exclusions are recorded in
`docs/ISAAC_SCENARIO1_EXECUTION_2026-09-09.md`.

### Developer
Implemented the pinned five-stage executor, measured completion/fixture gates,
isolated telemetry, passive multi-asset Unity build, durable worker/profile and
explicit UI adapter selection. Final checks: 400 simulation tests and 267 focused
backend/UI tests PASS; 13 frontend selection tests and typecheck/build PASS;
174 Unity Editor checks, 113-assembly audit and 14 launcher tests PASS.
Two existing dependency deprecation warnings remain. Original Unity/vendor files
and physical read-only deployments are unchanged.

### Evaluator
PASS for the fixed virtual fixture scope. Independent review findings were fixed
before both final Runs and covered by 89 added regression cases. Not a claim of
fully qualified physics or visual fidelity: OMY base visual registration remains
incorrect in the Isaac overview, carrier/grasp are kinematic, and the plate return
offset accumulated from 0.0895 to 0.1789 mm across the two cycles. Preserve these
limits when planning contact dynamics, extended repeats, package reuse and MCAP.
The bounded trial exited normally from READY at 950 seconds; receiver/observer
also exited. Web servers and the idle worker remain available, without changing
physical read-only services.

---

# Completed - Isaac execution and Unity observation pilot

## Current Iteration / Planner
Goal: retain SANG's durable Task orchestration while Isaac Sim replaces one
physical robot and a separate Unity observer mirrors ROS 2 measurements.

Scope: one vendor robot, one RGB camera, fixed bounded offset/return actions;
local SIMULATION only. No physical output, new application, generic script
execution, new motion planner, multi-robot qualification, or real safety approval.
The earlier A1/A2 evidence below remains historical, not overwritten by this pilot.

### Tasks and Acceptance Criteria
- [x] I1: inspect installed runtime/assets; pin the first robot and wire contracts.
- [x] I2: dedicated Isaac executor with fixed actions, wall-time authority expiry,
  idempotent results, cancel/reset semantics, simulation-clock observations.
- [x] I3: separate Unity observer build with no executor/vendor assembly; consume
  ROS-derived named joint measurements and reject stale/out-of-order data.
- [x] I4: existing Workflow UI/queue dispatches the pilot; actual Isaac movement,
  ROS camera/joints and Unity mirror measured end to end. Exercise failure gates.
- [x] I5: independent review, focused regressions, durable evidence and limitations.

### Risks / Developer Instruction
Do not modify hardware or existing read-only observation adapters. Retain old
profile serialization/digests. Separate Isaac command files/domain from the old
Unity executor; Unity never produces joint commands in this topology. Simulation
timestamps do not govern worker leases or disconnect watchdogs. Preserve existing
uncommitted code, original vendor scenes and validation output. Build/runtime
success is not a qualification of physical robot dynamics or safety.

### Current State
2026-09-09: complete for one fixed RB5 + one RGB camera. Actual Web Run
`sim-bdf273ce3bc14bfc8cb02158dee50f2f`: 2/2 actions completed in 6.150 s, Reset
acknowledged, gate PASS. Final 50-second probe: 994/994 Unity mirroring samples,
30.008 Hz average joints/images, simulation/wall ratio 1.00028. Actual worker-loss
probe latched Abort in 1.971 s without automatic recovery. Explicit observer
rebind after Reset was verified. Pilot processes stopped after verification;
Web servers and the dedicated idle worker remain available.

### Developer
Implemented a separate pinned Isaac executor, ROS receiver, observer-only Unity
Player and durable Worker/UI adapter. Existing physical/read-only paths and legacy
Unity profiles remain separate. Final source tests: 441 Python PASS (45 Isaac
included), 44 Unity Editor checks and strict Linux build PASS, 3 launcher tests
PASS, frontend typecheck/build PASS. Two dependency-deprecation warnings remain.
Commands, source digests, live results and launch instructions:
`docs/ISAAC_UNITY_TWIN_PILOT_2026-09-09.md`.

### Evaluator
PASS for the bounded local pilot. Independent review's ten findings and additional
camera timestamp regression were fixed and reverified; reviewer independently
ran 45 Isaac tests. No critical issue remains in that slice. Limits: no physical
outputs, whole Scenario 1, TCP/world-frame qualification, signed package release,
hard real-time guarantee or new MCAP/catalog integration. Unity stays a passive
observer and must be explicitly rebound after session/epoch changes.

---

# Completed Iteration - Unity asset binding A1/A2

## Planner
### Goal
Close the documented deployment debt and integrate the current Unity Scenario 1
with stable asset inventory and Task-driven, output-disconnected action dispatch.

### Scope for this iteration
Reuse Workflow Studio, published workflows, simulation worker, and existing robot
actions. No physical command sinks, arbitrary uploaded script execution, AAS user
content, second-scene package extraction, or new frontend design.

### Tasks and Acceptance Criteria
- [x] M0: Check old SO-101 OPC UA identity deployment; correct only the read-only
  deployment if possible, otherwise record the exact remaining constraint.
- [x] M1 (A1): Integrate stable OMY/RB5/carrier/camera bindings in the actual Unity
  project; export and validate inventory against the scene contract; compile.
- [x] M2 (A2 software): Resolve simulation roles/actions through immutable binding/package
  contracts, reject mismatches, preserve idempotency and output isolation; focused
  tests cover valid remapping and invalid identities/operations/inventory.
- [x] M3: Run the actual output-disconnected Unity workflow through the existing
  UI/worker path; record per-step results and any environmental blockers.
- [x] M4: Independent review, focused regressions, and honest master-plan/evidence
  closeout. Do not claim all Wave 4 or physical commissioning complete.

### Risks / Notes
- The repository contains extensive previous uncommitted work; preserve it.
- Scene integration must not re-enable legacy autonomous motion or physical I/O.
- Asset/source/build changes invalidate old simulation attestations.
- Existing observation services must not be stopped indiscriminately.

### Developer Instruction
Read existing contracts and runtime before edits; split independent Unity and
deployment work from backend changes. Verify actual artifacts and runtime output.

## Decisions Log
- 2026-09-09: Keep the current Ant Design UI and accepted action catalog flow;
  replace scenario-name coupling behind it, not the UI itself.

## Current State
Backend resolves arbitrary role/step names against a pinned schema-2 adapter
contract (final filename revision v3). Legacy v1 behavior/digest is retained.
Actual Unity 6000.5.6f1 build and nine-asset inventory pass. Full Fleet regression:
360 passed after the profile-rollover follow-up; final focused suite 15 passed
(includes one test added after full-suite collection). One existing Starlette warning.

Web Task copy/publish/validation completed as `unity-assets-a2-20260909@1`, Run
`sim-95bf4a8aef1a433e8be1e865f0797da5`: 5/5 steps SUCCEEDED in 162.034 seconds,
RESET ACKNOWLEDGED, validation PASS and READY inventory after Reset. Repeated OMY
motion after Reset remains a separate unqualified IK-limit issue, with root cause
not isolated. No threshold or trajectory was changed to suppress it.

M0 diagnosis completed but live repair NOT completed: configured 10.0.0.8 is no
longer assigned, CRLs expired 2026-09-04, and physical sources are stale. SAN,
ApplicationUri, pins and stored key pair agree only for the old host. Do not
migrate shared PKI or select a new remote endpoint implicitly. Evidence:
docs/SO101_OPCUA_IDENTITY_2026-09-09.md (74 isolated tests; no live secure Read).

Review environment: Claude Opus read-only review was attempted on 2026-09-09 but
OAuth expired and refresh failed before analysis. Do not record this as a review
or spend repeated Fable attempts against the same authentication blocker.

## Developer
Stable Unity inventory and typed action bindings are installed in the real
project. Backend resolves immutable scene/package/action contracts and fences
command/recovery emission against mismatched identities and lease loss. Admin,
Manager and the simulation worker were redeployed with the exact final build
profile. Existing read-only observation and recording services were left alone.

Changes and commands are detailed in docs/UNITY_DYNAMIC_BINDING_A1_A2_2026-09-09.md
and docs/A1_UNITY_INVENTORY_2026-09-09.md. Master plan, README, design and authority
index now distinguish this bounded runtime from signed package registration,
second-scene reuse, ROS 2/MCAP parity and physical commissioning.

## Evaluator
PASS for the explicitly bounded A1/A2 implementation and fresh-run/reset checks.
Independent Codex review closed both P1s (injected asset metadata and lease-loss
fencing) and P2 (profile rollover). No outstanding P1/P2 findings remain in that
reviewed slice. The final follow-up rejects queued/reconciled Runs when the
deployed digest differs and reports the block without crashing. Reviewer tests:
15 focused and 67 combined affected tests passed.

Limits: M0 live SO-101 repair is blocked; OMY repeat-after-reset motion is not
qualified; source archive/packages are local and unsigned, not Central Object
Store releases. A3 second-scene reuse and A4 ROS 2/MCAP parity remain open. Claude
review was unavailable due expired authentication. No physical I/O is authorized.
