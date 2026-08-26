# PLAN - Operator Run Closeout and LeRobot Derivation

Goal: Close validated Task Runs safely and derive immutable, observation-only LeRobot datasets from quality-passed MCAP sessions without inventing robot actions.

Done when:

- an Operator can complete an ACTIVE Task Run only after its latest Capture Job is terminal, uploaded, and quality PASS;
- completion releases every reserved asset and remains visible after refresh;
- the Robot/Camera v1 database and Object Store are backed up and the checked-in release state is reproducible;
- a bounded derivation job converts selected joint and compressed-camera observations from one immutable MCAP session into a versioned LeRobot-compatible dataset;
- derived metadata preserves source session, manifest/report hashes, Task Run, clock quality, calibration references, and selected time bounds;
- the converter emits no `action` feature because the current read-only recordings contain measured state only;
- no robot, vendor, policy, welder, or feeder command route is introduced.

Out of scope: synthetic next-state actions, policy training, hardware-trigger synchronization, automatic retention/deletion, cloud Object Storage, robot control, and welding control.

## Milestones

- [x] M0: Complete the attended RB5 Run through the existing state-transition API and verify four reservations are released.
- [x] M1: Add a server-enforced, quality-gated Operator Run completion flow and focused UI/API tests.
- [x] M2: Back up and verify the Robot/Camera v1 runtime, then create a Git release checkpoint.
- [ ] M3: Freeze the installed LeRobot version and an observation-only derivation contract.
- [ ] M4: Implement bounded MCAP conversion, immutable derived-object storage, provenance catalog, and tests.
- [ ] M5: Expose derivation status in the Catalog and run an end-to-end conversion against the accepted RB5 session.
- [ ] M6: Run regression, browser, backup, and no-actuation gates; record final evidence.

## Decisions Log

- 2026-08-26: Task Run completion stays explicit because one Run may contain multiple capture sessions.
- 2026-08-26: Operator completion uses a dedicated quality-gated endpoint; the generic Admin lifecycle endpoint remains valid for non-recording Tasks.
- 2026-08-26: The first LeRobot derivation is observation-only. Measured future state is not relabeled as a commanded action.
- 2026-08-26: Raw MCAP and its quality report remain authoritative and immutable; LeRobot output is a derived artifact with source hashes.
- 2026-08-26: Run completion uses the newest Capture Job, not merely the newest successful one, so a later failed or cancelled attempt cannot be hidden by an earlier PASS.
- 2026-08-26: Robot/Camera v1 backup is `/home/sa08050/sang-backups/robot-camera-v1-20260826T131714Z`; verification passed for SQLite schema versions 1-10 and 24 immutable objects (50,085,234,199 bytes).

## Current State

M0-M2 are complete. `rb5-ui-capture-acceptance-20260824` is terminal COMPLETE at revision 3 and all four assignments are unreserved. The quality-gated Operator closeout is live, the verified runtime backup is recorded above, and all 436 tests across nine package suites pass. The release checkpoint tag is `robot-camera-operations-v1-20260826`. M3 LeRobot version and derivation contract are next.
