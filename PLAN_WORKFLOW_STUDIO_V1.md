# PLAN - Workflow Studio v1

Goal: Add a manager-facing Workflow Studio that can author executable Task workflows, clone an existing Task into a new Task or a new version, publish immutable versions, and launch only an output-disconnected Unity simulation while showing step progress.

Done when:
- Drafts are revisioned and editable while published Task/Workflow versions remain immutable.
- An existing Task can be cloned as either a new Task or the next version, preserving its executable Workflow when one is linked.
- Publishing validates the Task and `WorkflowDefinitionV2` contracts and atomically records both plus their immutable link.
- The Ant Design manager UI can edit roles and action/barrier steps, validate, publish, clone, and view simulation progress.
- Browser-triggered execution is restricted to an explicitly configured Unity `SIMULATION` adapter with physical output disconnected.
- Focused backend tests, frontend type-check/build, and safety-boundary tests pass.

Out of scope:
- Physical robot, welding, feeder, policy, or XR command paths.
- Raw joint trajectory editing.
- Runtime execution of conditional, observation-wait, or human-approval steps.
- A free-form visual graph canvas; v1 uses an ordered editor plus dependency graph preview.
- Cooperative pause, resume, or abort of a Unity scene.

## Milestones
- [x] M1: Architecture document reviewed by a second Codex and revised - verify by: review findings are dispositioned in the design document.
- [x] M2: Revisioned authoring store and immutable publish/clone API - verify by: focused API/store tests.
- [x] M3: Output-disconnected Unity simulation execution service - verify by: service tests covering allowlist, idempotency, progress, and failure.
- [x] M4: Ant Design Workflow Studio - verify by: TypeScript check, Vite build, and static UI contract tests.
- [x] M5: Integrated evaluation - verify by: focused pytest suite, built static pages, `git diff --check`, and second-agent code review.

## Decisions log
- 2026-08-28: Published Task and Workflow versions are immutable; edits always begin from a mutable server-side draft targeting a new version.
- 2026-08-28: `Clone as new Task` starts target Task and Workflow histories at version 1. `Create new version` keeps their IDs and targets the next available versions.
- 2026-08-28: Task metadata and executable Workflow are published atomically and linked by an immutable mapping row.
- 2026-08-28: Motion payloads remain referenced Skill/Motion Artifacts; the browser does not expose raw actuator commands.
- 2026-08-28: The first execution adapter is Unity Scenario 1 only, `SIMULATION` only, and must pass the checked-in step/capability allowlist plus runtime output-isolation checks.
- 2026-08-28: A second Codex returned FAIL on the first draft. Accepted revisions include cardinality `1`, principal-scoped idempotency, one-transaction publish, a fixed adapter-profile digest, and a durable local Worker rather than an HTTP-owned thread.
- 2026-08-28: Workflow contracts are viewer-readable; authoring bodies are bounded and reject credential/deployment detail keys rather than becoming a secret store.
- 2026-08-28: The first final code review returned FAIL and reproduced late-lease revival and false COMPLETE outcomes. M5 remains open until those paths, v17 migration, persisted browser idempotency, and actual Manager compose wiring pass re-review.
- 2026-08-28: A second implementation re-review found overlong browser request IDs, camel-case credential and bare IPv6 scanning gaps, and a spoofable request-body size check. All were corrected and covered by focused tests before integrated evaluation.
- 2026-08-28: Real-browser copy, save, immutable publish, and Unity queue flow passed against an isolated temporary database; the full backend suite reports 270 passed, and frontend type-check/build plus `git diff --check` pass.
- 2026-08-28: A third strict pass reproduced plural credential, punctuated IPv6, delimiter-prefixed path, mixed-URI, and HTTP 500 description-validation gaps. The scanner and API regression tests now reject each case with HTTP 422 and prove no Draft mutation.
- 2026-08-28: Later adversarial passes expanded coverage to acronym/punctuation-separated keys, quoted and UNC paths, and slash-only root aliases. The normalized scanner closes the complete reproduced set without rejecting benign workflow values.
- 2026-08-28: The final independent Codex verdict is PASS with no critical or non-critical correctness finding.

## Current state
M1-M5 are complete. Workflow Studio v1 is implemented and independently approved.
