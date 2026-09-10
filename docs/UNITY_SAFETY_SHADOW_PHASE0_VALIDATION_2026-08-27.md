# Unity Safety Shadow Phase 0 Validation - 2026-08-27

## Verdict

PASS for the command-free Phase 0 software gate. This result does not validate Unity scene geometry,
human tracking, camera calibration, a physical Task Pause, or a safety-rated stop function.

## Implemented scope

- Versioned JSON contract for snapshots, policies, assessments, reasons, freshness, confidence,
  validity, Task Run identity, asset references, and evidence references.
- Deterministic reference evaluator for `SAFE`, `WARNING`, `UNSAFE`, and `UNKNOWN`.
- JSON contract export and replay CLI with no ROS, vendor, or command dependency.
- Additive SQLite schema v12 with one latest row per Agent/Run/evaluator and a bounded 10,000-row
  meaningful-transition history.
- Authenticated Central assessment ingest plus latest/event read APIs.
- ACTIVE Run, assigned asset, reporting Agent, service-token Agent scope, timestamp order, body size,
  idempotency, and out-of-order validation.

The assessment contract pins `shadow_mode=true` and `command_emitted=false`. `task_pause_candidate`
is a recommendation string only. No robot, vendor, welding, feeder, policy-execution, Task Pause, or
E-stop route was added.

## Verification evidence

- Focused safety suite: `13 passed`.
- Full Fleet Control Plane suite: `169 passed`.
- Full workspace suite in a clean container with the external Isaac assets mounted: `409 passed`,
  `14 warnings`, `0 failed` in 69.94 seconds.
- Clean temporary colcon build through `fleet_control_plane`: four packages finished, zero failed.
- `git diff --check`: passed.
- Checked-in `contracts/safety/shadow-v1.schema.json` exactly matches the Pydantic-generated schema.
- Host Isaac vendor validation reports both OMY and RB5 profiles valid with every file hash matching.

The warnings are pre-existing Starlette TestClient and asyncua X.509 deprecations. They do not affect
this phase's behavior.

## Fable final review

Model: `claude-fable-5` via the Claude CLI, read-only final Phase review.

Initial verdict: PASS, with three hardening items required before live Shadow ingest and four lower
priority observations.

Findings and resolutions:

1. Unbounded threshold-flap events: fixed with a newest-10,000 transition retention cap and a prune
   regression test. Volatile metrics remain excluded from transition identity.
2. Far-future assessment time could wedge a latest key: fixed by rejecting timestamps over two
   seconds ahead of Central receive time, with a regression test.
3. Every assessment triggered Fleet WebSocket churn: fixed so `fleet_revision` changes only for a
   meaningful safety transition; replay and stable-update behavior is tested.
4. Agent-to-Run attribution was weak: fixed by requiring a live reporting Agent to be bound to an
   assigned asset or its Processing Node.
5. Superseded non-transition retry semantics: documented as terminal `409`; immediate and retained
   transition retries remain idempotent without storing every high-rate receipt.
6. Test gaps: added conflicting-ID, replay-on-complete, future-time, wrong-Agent, retention, viewer
   GET/operator POST, oversized-body, and revision-churn coverage.

No second Fable call was used; the project policy reserves one Fable review per Phase. All review
fixes were verified by the local suites above.

## Remaining attended evidence

No Unity Hub, Unity Editor executable, desktop entry, or Unity project was found on this PC, so no
Unity runtime was started. Before Phase 1 integration, the operator must choose an Editor/LTS version
and project location, then supply the world-frame transform,
person/object tracking contract, approved zone/threshold values, and camera calibration identities.
The existing live Central process was not restarted for this command-free contract iteration, so its
production database has not yet been migrated to schema v12 and the new endpoints are not active in
that process. Back up the live database and perform the short Central restart at the Phase 1 rollout.
The rollout order remains MCAP replay, live Shadow Mode, attended false-positive/false-negative
review, and only then a separately reviewed orchestration pause. Physical stop remains outside this
software path and under the independent certified safety chain.
