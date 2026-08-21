# Phase 3.5 - Local MCAP Storage MVP

## Goal

Record the read-only RB5 Lab1 observation streams as an immutable local MCAP
session that can later be uploaded to object storage or converted to LeRobot.

## Scope

- Record only these allowlisted ROS 2 topics:
  - RB5 measured joints
  - OTOS welding camera compressed images
  - D455 color compressed images
- Run as a separate `sang_mcap_recorder` worker on `processing-lab1`.
- Write into a `.partial` staging directory while capture is active.
- Finalize only after ROS bag metadata, topic types, and non-zero message counts
  have been validated.
- Write an atomic `session-manifest.json` with provenance, source time range,
  per-topic counts, file sizes, and SHA-256 checksums.
- Preserve an explicit failure manifest in the partial directory when capture
  does not pass validation.

## Non-Goals

- No robot commands, ROS actions, services, torque changes, or vendor command
  channel access.
- No central object-storage upload or catalog service.
- No LeRobot conversion, video sidecar, image decode, or image re-encode.
- No live preview recording and no high-rate payloads in SQLite.
- No claim of hardware-synchronized exposure time. Current adapter timestamp
  provenance remains explicit in the session manifest.

## Artifact Layout

```text
/var/lib/sang/captures/
  <session_id>.partial/        active or failed capture
    metadata.yaml
    *.mcap
    session-manifest.json      state=failed when present
  <session_id>/                finalized immutable capture
    metadata.yaml
    *.mcap
    session-manifest.json      state=complete
```

## Safety And Integrity Rules

1. The profile is a checked-in allowlist; `--all` and regular-expression topic
   selection are not exposed.
2. Session identifiers and output paths are validated against path traversal.
3. SIGINT and SIGTERM are forwarded to `ros2 bag record`, then bounded by a
   forced-stop timeout.
4. A final directory is never overwritten.
5. Every required topic must have the configured ROS type and at least one
   recorded message.
6. ROS bag output is retained as a hashed `recorder.log`; any reported
   transport message loss prevents completion.
7. The manifest is written atomically and the staging directory is renamed
   atomically only after all checks pass.
8. Camera payloads remain the original `sensor_msgs/msg/CompressedImage` bytes.

## Acceptance Criteria

- Unit tests cover profile validation, path safety, metadata validation,
  checksums, complete finalization, and failed partial retention.
- A fake ROS integration records JointState and two CompressedImage topics,
  then `ros2 bag info` reports MCAP with all three non-empty topics.
- Docker build and focused/full repository tests pass.
- A short read-only capture on `processing-lab1` records the live RB5, OTOS,
  and D455 topics and verifies every checksum.
- Stopping or failing the recorder does not stop the Agent, adapters, or camera
  publishers.

## Deferred Follow-Up

- Phase 4: local session inventory in Agent heartbeat and multi-edge status.
- Phase 5: object storage, central artifact catalog, retention policy, and
  provenance-preserving LeRobot conversion.
- Time-quality follow-up: PTP/NTP evidence, sensor hardware timestamps where
  supported, and tighter timestamp/skew validation.

## Validation Result

**COMPLETE (2026-08-06).** Unit tests, fake ROS integration, the full repository
suite, and a live RB5 Lab1 read-only capture passed. The accepted session had
678 messages, zero ROS bag transport loss, zero invalid/regressing source
headers, and independently matching SHA-256 checksums. The detailed evidence
and the separately tracked OTOS rate debt are in
`docs/RB5_LAB1_MCAP_RESULT_2026-08-06.md`.

The one-time Claude Fable final review returned PASS with no critical issues.
Its concrete hardening findings were applied to symlink rejection, fsync
durability, failure-manifest independence, literal requirements wording, and
subprocess/loss regression coverage.
