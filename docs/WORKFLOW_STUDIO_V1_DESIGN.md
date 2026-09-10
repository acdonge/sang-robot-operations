# Workflow Studio v1 Design

Status: Design accepted and v1 output-disconnected implementation verified; physical execution remains
outside this release

Date: 2026-08-28

Roadmap authority: [`../SANG_MASTER_IMPLEMENTATION_PLAN.md`](../SANG_MASTER_IMPLEMENTATION_PLAN.md).
Implementation evidence: [`WORKFLOW_STUDIO_UNITY_LIVE_RESULT_2026-08-28.md`](WORKFLOW_STUDIO_UNITY_LIVE_RESULT_2026-08-28.md)
and [`SIMULATION_OPERATIONS_V1_EVIDENCE.md`](SIMULATION_OPERATIONS_V1_EVIDENCE.md).

## 1. Decision

SANG will add a manager-facing **Workflow Studio**. A Task remains the operator-facing unit of work, while its executable behavior is a versioned `WorkflowDefinitionV2` directed acyclic graph (DAG).

Published Task and Workflow versions are immutable. A change never edits a published row. It creates a mutable Draft targeting a new version, validates it, then publishes a new immutable Task/Workflow pair.

The Studio also supports two explicit copy operations:

1. **Create new version** keeps the Task and Workflow IDs and targets each ID's next version.
2. **Clone as new Task** copies metadata, roles, and the linked executable Workflow into new IDs beginning at version 1.

This preserves reproducibility for executable Simulation Runs. Existing Task Runs remain
observation/recording metadata and pin a Task ID/version; the immutable publication can be
resolved from that exact Task version but is not copied into the legacy Task Run row.

## 2. Scope and safety boundary

Workflow Studio v1 includes:

- Server-side mutable Drafts with optimistic revisions.
- Ordered role and step editing for `action` and `barrier` steps.
- Explicit dependencies and a graph preview. Parallel graphs may be authored for a
  future runtime, but the Scenario 1 Unity adapter accepts only its fixed sequential DAG.
- Contract validation before publish.
- Atomic Task/Workflow publication and immutable linkage.
- Copy as a new Task and branch as a new version.
- A manager-only Unity Scenario 1 simulation queue action and step-progress view,
  executed by a separate local Worker rather than the HTTP process.

Workflow Studio v1 does not include:

- Physical robot, welder, feeder, policy, or XR commands.
- Raw joint targets, vendor commands, or arbitrary shell/script execution.
- Runtime support for conditional, observation-wait, human-approval, or human-task steps. Existing contracts remain preserved, but the v1 editor labels unsupported kinds as read-only and the local runtime will hold rather than pretend to execute them.
- Cooperative simulation pause/resume/abort. Those controls require a cancellation contract in Unity and the Cell Orchestrator first.
- Multiple assets in one role. Workflow Studio v1 fixes role cardinality to `1`
  because the existing Task assignment model stores one asset per role.

The existing read-only adapters remain unchanged. A browser request can launch only a configured `SIMULATION` adapter. The adapter must verify a fresh local Unity heartbeat, `physical_output_connected=false`, the scenario identity, and the checked-in step/capability allowlist before dispatch.

Authoring request bodies are bounded to 512 KiB with a maximum JSON nesting depth of
12. Parameter keys that imply credentials or deployment details, including password,
secret, token, private key, host, port, endpoint, vendor, topic, or asset binding, are
rejected. Workflow payloads are viewer-readable contracts and never a secret store.

## 3. Domain model

### 3.1 Task Definition

The existing Task Definition remains immutable registry metadata:

- Task ID and version
- Display name and description
- Required roles, asset types, and access modes

It answers **what operational unit is being run and what assets must be assigned**.

### 3.2 Workflow Definition

`WorkflowDefinitionV2` remains the executable contract:

- Workflow ID and version
- Capability-based roles and cardinality
- DAG steps, dependencies, timeout, retry, parameters, and recovery references

It answers **which validated Skills run, in what dependency order, against which role**.

### 3.3 Workflow Draft

A Draft is authoring state, not an executable or historical record. It stores:

- Draft ID, state, and optimistic revision
- Target Task ID/version and Workflow ID/version
- Display name and description
- Authoring roles: role ID, asset type, access, fixed cardinality `1`, required capabilities
- Step JSON preserved losslessly for all `WorkflowDefinitionV2` kinds
- Optional source Task/Workflow provenance
- Creation/update/publication timestamps

Draft roles may temporarily omit capabilities and Drafts may have no steps. Publish performs the strict contract validation. This allows cloning a legacy Task that has no linked executable Workflow without inventing behavior.

### 3.4 Immutable Task-Workflow publication

An immutable publication row links:

- Task ID/version
- Workflow ID/version
- Workflow SHA-256
- Source Draft ID
- Published timestamp

Database triggers reject update and delete. A Workflow Studio Simulation Run pins this
publication instead of resolving "latest" at execution time. A future physical
Orchestration Run must do the same before G7 commissioning.

### 3.5 Authoring persistence migration

Fleet schema migration v17 installs the idempotent `WORKFLOW_AUTHORING_MIGRATION`.
Recording it in the global migration ledger prevents an older v16 binary from opening
the database and bypassing Workflow Studio invariants. It adds these tables and constraints:

- `workflow_drafts`: primary key `draft_id`; target Task/Workflow identities and
  versions; `state IN ('DRAFT','PUBLISHED')`; revision `>=1`; role/step JSON;
  source provenance; timestamps.
- `task_workflow_publications`: primary key `(task_definition_id, task_version)`;
  unique `(workflow_id, workflow_version)`; 64-character Workflow digest; source
  Draft; timestamp; foreign key to the Task Definition. Update/delete triggers reject
  mutation.
- `workflow_authoring_requests`: primary key `(principal_id, request_id)`; operation,
  canonical request digest, result kind/identity, and response JSON. A request ID reused
  with different content is rejected.
- `workflow_simulation_runs`: primary key `run_id`; unique request identity; pinned
  Task/Workflow identities and digest; adapter/profile digest; state
  `QUEUED|RUNNING|COMPLETE|FAILED|INTERRUPTED`; Worker lease fields; timestamps and
  bounded detail.
- `workflow_simulation_run_events`: append-only Run state/progress evidence.
- `workflow_simulation_workers`: Worker/adapter heartbeat, readiness, fixed profile
  digest, scenario, lease timestamp, and `physical_output_connected=0` check.

A partial unique index permits at most one `QUEUED` or `RUNNING` Run per adapter.
Triggers also reject update/delete of `task_definitions`; published Task metadata is
therefore immutable at the database boundary, not merely by API convention.

## 4. Version and clone semantics

### 4.1 Blank Task

Creating a blank Draft computes the next Task and Workflow versions at creation time. Publish rechecks both under one database write lock. Concurrent Drafts may target the same version, but only one can publish; the loser receives a version conflict and must branch again. The request includes an idempotency ID scoped to the authenticated principal.

### 4.2 Create new version

The source must be an existing Task version. The target Task ID is unchanged. If an immutable Task-Workflow publication exists, the Workflow ID is unchanged and its full roles/steps are copied. The target versions are the next available versions for their respective IDs.

### 4.3 Clone as new Task

The source must be an existing Task version. The caller supplies a new Task ID and, by default, the same new Workflow ID. Both target histories begin at version 1. Display name and description are copied but remain editable in the Draft.

If the source Task has a linked Workflow, roles and steps are copied. If it does not, Task roles are copied with empty capability requirements and no steps. The Studio explains that the Draft must be completed before publication.

Clone copies no Runtime Binding, assigned asset, credential, approval, reservation,
Run history, or safety evidence. Those are deployment/run-specific records.

### 4.4 Published objects

Published versions have no update or delete API. The UI offers only:

- View
- Create new version
- Clone as new Task
- Queue compatible simulation

## 5. Publication transaction

Publishing takes the Draft ID, expected revision, and idempotency request ID. A dedicated
`WorkflowAuthoringStore.publish()` performs the following using one SQLite connection
and one `BEGIN IMMEDIATE` transaction. It does not compose the existing Store methods,
because those methods open independent transactions:

1. Verify the Draft is still editable and its revision matches.
2. Compile authoring roles into `TaskDefinitionCreate` and `WorkflowRoleV2`.
3. Validate the strict `TaskDefinitionCreate` and `WorkflowDefinitionV2` models.
4. Recheck that both target versions are the next legal versions.
5. Insert the immutable Task Definition.
6. Insert the immutable orchestration Workflow with canonical JSON and SHA-256.
7. Insert the immutable Task-Workflow publication mapping.
8. Mark the Draft `PUBLISHED`, increment its revision, and append configuration audit evidence.
9. Store the canonical idempotent response in the same transaction.

No partially published pair is externally visible. A lost HTTP response can be retried
with the same request ID and payload to receive the original result.

The legacy `/api/v1/config/task-definitions` route remains for observation-only Tasks.
Once a Task ID has a Workflow Studio publication, that route rejects later versions for
the ID. The low-level orchestration Workflow route remains a contract-import route and
does not create a Task publication by itself.

## 6. API surface

All write routes require the existing `admin` role. Read routes require `viewer`.

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/workflow-authoring/drafts` | List Drafts |
| `POST` | `/api/v1/workflow-authoring/drafts` | Create blank Draft |
| `GET` | `/api/v1/workflow-authoring/drafts/{draft_id}` | Read Draft |
| `PUT` | `/api/v1/workflow-authoring/drafts/{draft_id}` | Save with expected revision |
| `POST` | `/api/v1/workflow-authoring/drafts/clone` | Clone as new Task or new version |
| `POST` | `/api/v1/workflow-authoring/drafts/{draft_id}/validate` | Return strict validation result |
| `POST` | `/api/v1/workflow-authoring/drafts/{draft_id}/publish` | Atomically publish |
| `GET` | `/api/v1/workflow-authoring/publications` | List immutable Task/Workflow pairs |
| `POST` | `/api/v1/workflow-authoring/simulation-runs` | Queue an allowlisted Unity simulation |
| `GET` | `/api/v1/workflow-authoring/simulation-runs` | List simulation Runs |
| `GET` | `/api/v1/workflow-authoring/simulation-runs/{run_id}` | Read Run and step progress |

Blank create, clone, publish, and Simulation queue use client-generated idempotency
request IDs. The browser retains each pending ID in session storage, keyed by a SHA-256
of the operation and form payload, until it receives a successful response. A replay by
the same principal with the same canonical payload returns the original response.
Reusing the ID with a different payload returns HTTP 409.

## 7. Manager UI

`Workflow Studio` is a new Ant Design manager page, separate from observation-only Operator surfaces.

### 7.1 Draft workspace

- Task/Workflow identity and target versions
- Display name and description
- Role editor: role ID, asset type, access, fixed cardinality `1`, capability tags
- Ordered step editor: step ID, kind, role, capability, dependencies, and JSON parameters
- Add Action, add Barrier, duplicate, move, and remove controls. Moving affects display
  order only; `depends_on` is the sole execution-order contract.
- A quiet dependency preview showing upstream relationships and parallel branches
- Save Draft, Validate, and Publish actions

Unsupported step kinds are preserved and shown read-only rather than deleted or silently simplified.

Retry remains fixed at one attempt, and compensation/lifetime/fan-out controls are not
shown in v1. Existing values from an imported Workflow are preserved read-only. Step
timeouts remain part of the immutable contract but are not exposed as an editable v1
control until every adapter enforces them consistently.

### 7.2 Published library

- Exact Task/Workflow versions and digest
- Create new version
- Clone as new Task
- Queue Unity simulation when compatibility checks pass

### 7.3 Run monitor

- Run state and pinned Workflow digest
- Per-step `PENDING`, `DISPATCHED`, `SUCCEEDED`, `HELD`, or `FAILED` state
- Current Unity phase and failure detail when available
- Automatic in-place polling without remounting or flashing the page

## 8. Unity execution boundary

The first adapter is deliberately scenario-specific and uses a durable local Worker:

- Central records a durable `QUEUED` Run; the HTTP process never executes Unity or
  starts an arbitrary subprocess.
- A separately launched local Worker advertises a fresh readiness heartbeat, claims one
  Run with a bounded database lease, and owns the Unity bridge.
- It reads only the configured Unity Scenario 1 adapter profile. The fixed profile's
  canonical SHA-256, not the selected Workflow, is the execution allowlist.
- It resolves the active local bridge root and requires a fresh heartbeat.
- Central compatibility validation, the Worker, and the Unity C# agent independently
  enforce scenario ID, exact roles, exact action step/capability sequence, fixed
  sequential dependencies, profile digest, and output isolation.
- The Cell Orchestrator still issues signed, expiring, idempotent SIMULATION Skill requests.
- The service stores Run and lease metadata in Central SQLite; the Cell Orchestrator
  keeps its own restart evidence in the Run runtime directory.
- `physical_output_connected` must remain false in command, heartbeat, result, and final summary.

An edited Workflow that is valid but incompatible with the current Unity scene can still be published. The Studio must report it as not runnable on that adapter instead of weakening the Workflow contract.

The Scenario 1 runnable subset is the profile's exact five `action` steps, fixed action
sequence, cardinality `1`, one attempt, and no compensation, lifetime, fan-out,
barrier, conditional, observation-wait, or human step. A `barrier` remains authorable
and publishable for future adapters, but this adapter reports it as incompatible. A
Worker crash or expired lease changes
the Run to `INTERRUPTED`; it is never automatically re-dispatched. An attended operator
must create a new Run after inspecting evidence.

## 9. Failure behavior

- Stale Draft revision: HTTP 409; UI reloads before overwriting.
- Duplicate target version: HTTP 409; source remains unchanged.
- Invalid role/step graph: HTTP 422 with structured validation issues; nothing is published.
- Unity Worker unavailable, stale, busy, or not output-isolated: HTTP 409/503; no Run is queued.
- Repeated simulation request ID: returns the original Run.
- Worker lease expiry changes `RUNNING` to `INTERRUPTED`; Cell Orchestrator evidence
  remains for attended reconciliation. Central HTTP restarts do not own or duplicate the
  Worker execution.
- Unsupported or non-complete runtime outcome: the Worker records the Simulation Run
  as `FAILED` with the Cell Orchestrator state and reason.

## 10. Verification

Backend tests must prove:

- Blank Draft create/update revision checks.
- New-version and new-Task clone semantics.
- Legacy Task clone preserves metadata without inventing steps.
- Strict validation catches missing capabilities, missing roles, missing dependencies, and cycles.
- Oversized, deeply nested, secret-bearing, or deployment-specific parameters are rejected.
- Publish is atomic and immutable.
- Concurrent target-version conflict is deterministic.
- Idempotent blank/clone/publish retries return the original result and reject changed payloads.
- Simulation queueing is idempotent and rejects non-published, incompatible,
  stale-Worker, busy, profile-mismatched, or output-connected inputs.
- Progress reads committed Cell Orchestrator step state.

Frontend checks must prove:

- English-only first-party copy.
- Workflow Studio is a built and served Ant Design entry.
- Published rows expose no edit action.
- Clone modes are distinct.
- Legacy Task Run activation is labeled `Mark active (metadata only)`, never `Start`.
- Build and TypeScript checks pass.

## 11. Deferred work

- Capability-specific parameter schemas and a Skill catalog form generator.
- Versioned Motion Artifact registry and trajectory visualization.
- Conditional, observation, human-approval, compensation, and long-running lifetime editors.
- Cooperative pause/resume/abort and Unity reset contracts.
- Background Processing-PC Orchestrator service with mTLS delegation from Central.
- Physical commissioning after G7, including local Device Agents, command mux, safety gate, deadman/TTL, and attended evidence.
- XR authoring and approval views.

## 12. Review record

Strict review model: second Codex agent, 2026-08-28

Initial verdict: **FAIL**

Material findings and dispositions:

- Independent Store transactions could partially publish: accepted; publication now
  uses one connection and one transaction.
- Existing Task Run did not pin a publication: accepted; v1 makes only Simulation Runs
  pin it and documents legacy Task Runs as observation/recording metadata.
- Legacy APIs bypassed pair creation: accepted; their scope is explicit and published
  Task IDs cannot receive later legacy versions.
- Cardinality mismatch: accepted; v1 is fixed to one asset per role.
- Retry/parallel/compensation UI exceeded runtime behavior: accepted; controls removed
  and Scenario 1 compatibility narrowed to the actual runnable subset.
- HTTP-owned synchronous execution was not restart-safe: accepted; replaced by durable
  queue, Worker heartbeat, claim lease, and non-replayed `INTERRUPTED` outcome.
- Selected Workflow could become its own allowlist: accepted; a checked-in adapter
  profile digest is authoritative.
- Browser simulation was an undocumented safety exception: accepted; requirements and
  repository safety contract must explicitly allow only the output-disconnected queue.
- Create/clone/publish retries were not idempotent: accepted; principal-scoped request
  records are required.
- Step display name was not in `WorkflowDefinitionV2`: accepted; v1 displays `step_id`.
- Existing `Start run` label was misleading: accepted; it becomes metadata-only wording.

Revised design verdict by the primary evaluator: **PASS FOR IMPLEMENTATION**, subject
to the verification in section 10.

Final implementation review by the same second Codex initially returned **FAIL**. Its
runtime reproductions and dispositions were:

- A Cell Orchestrator `HELD` result was recorded as `COMPLETE`: accepted; only an
  orchestrator `COMPLETE` result now completes the Simulation Run, and all other
  terminal results are `FAILED` with state/reason evidence.
- A Worker could renew after lease expiry when no UI poll had reconciled it: accepted;
  update and finish now expire and commit the lease atomically before rejecting the
  late Worker.
- Drafts accepted deployment addresses until publish: accepted; secret keys, network
  addresses, external URIs, and absolute paths are rejected before Draft persistence.
- Browser retries generated a new request ID: accepted; pending IDs survive page reload
  in session storage and are cleared only after a successful response.
- Lab overlay and HTTPS Manager dropped the adapter profile: accepted; both commands
  include the fixed profile, and an optional output-isolated Worker compose profile is
  provided.
- The schema was installed outside the migration ledger: accepted; the migration is
  global schema v17, making downgrade opening fail closed.
- Read-only unsupported steps, API error details, mapping digest validation, progress
  evidence, and Worker failure coverage were incomplete: accepted and covered by UI,
  trigger, event, and integration-test changes.

The second implementation re-review also returned **FAIL** before release. Its
findings and dispositions were:

- Save and publish request IDs exceeded the server's 64-character contract: accepted;
  browser operation IDs are now compact UUID request IDs while the pending operation
  remains stable in session storage until success.
- Camel-case credential keys and bare IPv6 literals bypassed Draft scanning: accepted;
  key normalization now detects credential/password/secret/token components, and IP
  literals are validated with the standard IP parser.
- A false small `Content-Length` header could bypass the authoring-body limit: accepted;
  middleware now reads and verifies the actual body size and rejects a mismatched
  declared length.
- Structured API error arrays rendered poorly, and the optional Worker inherited host
  networking unnecessarily: accepted; UI errors are normalized and the Worker remains
  on the isolated Compose network.

A third strict implementation pass returned **FAIL** after reproducing additional
embedded-text bypasses. Its findings and dispositions were:

- Plural compound keys such as `clientCredentials` were not rejected: accepted;
  normalized key components now reject singular and plural credential/deployment terms,
  including terms embedded in longer compound keys.
- Punctuated IPv6, delimiter-prefixed absolute paths, and an external URI following an
  allowed registry URI could persist in descriptions: accepted; every URI scheme is
  inspected independently, IP candidates are punctuation-normalized, allowed references
  are removed before absolute-path inspection, and delimiter-prefixed Unix/Windows paths
  are rejected.
- Description scanner failures surfaced as HTTP 500: accepted; scanner violations now
  use the authoring validation exception and are returned as HTTP 422.
- API regression coverage now proves every reproduced payload leaves the Draft revision,
  description, and steps unchanged, while a standalone `artifact://` reference remains
  accepted.

Subsequent adversarial passes intentionally expanded those cases before approval. They
found plural multi-word keys, acronym and punctuation-separated keys, quoted/bracketed
Unix and Windows paths, and slash-only root aliases. The final scanner therefore:

- Performs acronym-aware camel-case splitting, maps every non-alphanumeric separator to
  the same key boundary, singularizes the complete compound name, rejects non-ASCII key
  homographs, and compares both tokenized and compact sensitive phrases.
- Inspects each URI scheme independently; permits only checked-in registry-reference
  schemes; rejects embedded IPv4/IPv6, POSIX, drive-letter, UNC, and slash-only root
  paths; and preserves benign values such as `APIVersion`, `1/2`, and `A/B`.
- Proves every rejected API request leaves Draft revision, description, roles, and steps
  unchanged.

Final strict second-Codex verdict on 2026-08-28: **PASS**. No critical or non-critical
correctness issue remained, and no physical robot, welder, policy, XR, vendor, or raw
joint command path was introduced.

Integrated verification on 2026-08-28:

- Real browser flow passed against a temporary SQLite database: copy published Task,
  save Draft revisions, publish the immutable Task/Workflow pair, and queue the pinned
  Unity simulation. The resulting counts were zero editable Drafts, two publications,
  and one `QUEUED` Simulation Run.
- Backend regression suite: `270 passed`.
- Frontend TypeScript check and production build passed. The build retains the existing
  non-blocking warning for a shared Ant Design chunk above 500 kB.
- `git diff --check` passed.
