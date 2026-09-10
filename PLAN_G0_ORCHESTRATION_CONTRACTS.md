# PLAN - G0 Generic Orchestration Contracts

## Planner
### Goal
Implement the first executable contract layer for the generic multi-robot architecture defined in
`docs/MULTI_ROBOT_ORCHESTRATOR_IMPLEMENTATION_DESIGN.md`.

### Scope for this iteration
G0 is contract-only. It defines and validates versioned payloads for heterogeneous robot/camera
manifests, capability-based workflows and runtime bindings, delegated control authority, Unity build
provenance, and Safety Snapshot v2. It does not persist these contracts, expose new APIs, publish ROS
2 commands, connect to vendor SDKs, or move physical equipment.

### Tasks
1. Add strict Pydantic models for capability profiles, device manifests, zones/calibrations,
   workflows, runtime bindings, coordinated groups, execution authority, Unity attestations, and
   Safety Snapshot v2.
2. Add cross-contract validators for workflow graph integrity, capability/cardinality binding,
   execution-mode/runtime compatibility, and delegated sublease fencing.
3. Export deterministic JSON Schema files and provide simulation-only reference examples.
4. Test valid and invalid contracts, including one unchanged workflow bound to both three and twelve
   cameras.
5. Run focused tests plus the existing safety/task contract regression tests, then review the diff
   for accidental command paths.

### Acceptance Criteria
- All contract models reject unknown fields and malformed identifiers, references, times, and graph
  relationships.
- Workflow definitions contain no robot vendor, simulator scene, IP address, or fixed camera-count
  fields.
- The same workflow object validates against three-camera and twelve-camera bindings without edits.
- A sublease is rejected when its grant, epoch, boot identity, capability, envelope, time window, or
  TTL does not match.
- Physical execution bindings are rejected unless their bound command-capable assets are explicitly
  commissioned; supplied examples remain simulation-only and command-free.
- JSON Schemas regenerate byte-for-byte and all focused tests pass.
- No ROS publisher, vendor SDK call, physical command endpoint, database migration, or UI control is
  added in G0.

### Risks / Notes
- Safety Snapshot v2 is a new contract and does not replace the deployed v1 Shadow evaluator in this
  iteration.
- Runtime registry persistence and API integration belong to G1; ROS 2 action/adapters belong to G3
  and later safety-gated phases.
- Existing dirty-worktree changes are user-owned and must remain untouched.

### Developer Instruction
Implement only the G0 models, deterministic schema exporter, simulation fixtures, and focused tests.
Reuse the existing `FleetModel` and `SafeId` conventions, keep physical command sinks at zero, and do
not modify deployed runtime behavior.

## Milestones
- [x] M1: Add strict versioned contract models.
- [x] M2: Add graph, binding, readiness, and lease validators.
- [x] M3: Export schemas and add simulation-only examples/documentation.
- [x] M4: Add focused positive and adversarial tests.
- [x] M5: Run regressions and evaluate G0 against its acceptance criteria.

## Evaluation Record

- Focused G0 contract tests: 33 passed.
- Full `fleet_control_plane` regression: 209 passed; one pre-existing FastAPI TestClient deprecation
  warning.
- Nine generated schemas pass Draft 2020-12 meta-schema validation and deterministic drift check.
- All committed orchestration JSON examples parse; normative design JSON blocks validate against the
  frozen models.
- Independent Claude Opus implementation review: first pass FAIL identified three blockers and five
  high issues; revisions closed all of them; second pass PASS authorized G0 closure and G1 start.
- Fable 5 final review was attempted first but the CLI reported exhausted usage credits, so the
  configured Opus fallback performed the independent gate.
- No runtime command transport, ROS publisher, vendor SDK call, DB/API migration, or physical device
  operation was introduced.
