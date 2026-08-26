# PLAN - Task Run Capture Jobs

Goal: Let an authorized Operator start and stop a bounded, read-only Edge MCAP capture from the Task Run UI without SSH, while preserving exact Task Run provenance, restart-visible state, idempotency, automatic Central upload, and zero robot or welding command paths.

Done when:

- Central durably stores idempotent Capture Jobs linked to one ACTIVE Task Run and one fresh, mTLS-authenticated Edge capture worker;
- the Edge pulls only its own jobs and executes one checked-in allowlisted capture profile without SSH, a Docker socket, or arbitrary command input;
- UI refresh and Robot Agent restart do not lose capture state, and capture-worker interruption preserves the partial session and reaches a visible terminal failure instead of duplicating data;
- completed uploads advance the job to `COMPLETE` only after Central verifies every declared artifact;
- the Operator UI shows capture phase, stop control, linked session, MCAP size, artifact completion, and SHA-256 integrity;
- tests and a short Lab1 acceptance pass without any robot, vendor, welder, feeder, policy, ROS publish/service/action, or writable OPC UA command path.

Out of scope: robot movement, policy execution, welding/feeder I/O, arbitrary profile authoring from the UI, resumable rosbag writes after capture-worker failure, retention/deletion, LeRobot conversion, AAS, and hard real-time synchronization.

## Milestones

- [x] M1: Persist the architecture and inspect current Task Run, authentication, Agent mTLS, recorder, uploader, catalog, and UI contracts.
- [x] M2: Add the Capture Worker/Job schema, API, RBAC, Task Run validation, durable events, idempotency, and upload-to-job completion linkage.
- [x] M3: Add the mTLS Agent pull/report protocol and fixed-profile Edge recording worker with stop and restart reconciliation.
- [x] M4: Add Operator capture controls and linked MCAP/integrity presentation without adding actuator controls.
- [x] M5: Pass focused/full tests, render Compose, deploy Central and Lab1, and complete a short live read-only acceptance.
- [x] M6: Record final evidence, review the diff and safety boundary, and update the roadmap.

## Decisions Log

- 2026-08-24: Use an outbound Edge pull protocol on the existing Agent mTLS ingress at port 8443. Central does not connect inbound to the Edge and never sends shell or ROS commands.
- 2026-08-24: Keep artifact transfer on the existing dedicated recording mTLS ingress at port 8444 so job control and multi-gigabyte data transfer remain isolated.
- 2026-08-24: Register fixed-profile workers dynamically over mTLS. A worker advertises its checked-in profile ID, robot ID, and asset IDs; Central creates a job only when every advertised asset belongs to the selected ACTIVE Task Run.
- 2026-08-24: Use a client request ID plus a database uniqueness constraint for replay-safe creation, and allow only one non-terminal job per worker.
- 2026-08-24: The Edge may report progress through `UPLOADING`, but only Central Object Store finalization may set `COMPLETE`.
- 2026-08-24: A Robot Agent restart is isolated from the recording worker. A recording-worker restart does not attempt unsafe rosbag append; it preserves `.partial` evidence and marks the durable job failed.

## Current State

M1-M6 are complete. The Operator UI starts/stops only Task Run Capture Jobs, persists the client request ID across ambiguous retries, restores durable state from Central after refresh, and links verified MCAP objects and manifest SHA-256 into the Catalog. Viewer/Operator RBAC and Agent-scoped service authorization are covered. The Control Plane, Robot Agent, and MCAP Recorder suites pass 277 tests; the only repository-wide collection blocker is an unrelated stale generated XR message in an existing build output.

The 2026-08-24 Lab1 acceptance created one 45-second job for `rb5-ui-capture-acceptance-20260824`. Replaying the same request returned the same job, restarting the observation Agent did not interrupt capture, and Central alone advanced `UPLOADING` to `COMPLETE` after verifying 3/3 objects and the manifest SHA-256. A second 20-second job was started through the real Operator button; a full browser reload restored its `UPLOADING` state and the UI then reached `VERIFIED / SHA-256`. The full evidence is in [`docs/TASK_RUN_UI_CAPTURE_ACCEPTANCE_2026-08-24.md`](docs/TASK_RUN_UI_CAPTURE_ACCEPTANCE_2026-08-24.md).
