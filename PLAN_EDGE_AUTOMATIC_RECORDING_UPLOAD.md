# PLAN - Automatic Edge Recording Upload

Goal: Automatically transfer finalized Processing PC recording sessions into the Central immutable Object Store and SQLite catalog over Agent-authenticated mTLS.

Done when: Central accepts only an enrolled Agent's own sessions on a dedicated upload ingress, the Edge retries incomplete transfers without re-uploading completed artifacts, deployment configuration is wired for Central and Lab1, and tests plus a live idempotent Lab1 session check pass.

Out of scope: recording deletion or retention, remote robot commands, automatic Task Run lifecycle control, LeRobot conversion, and cloud Object Store backends.

## Milestones

- [x] M1: Review the existing identity, catalog, Object Store, and deployment contracts - verify by: identify reusable APIs and isolate large uploads from heartbeat traffic.
- [x] M2: Add a dedicated mTLS recording ingress with certificate-to-Agent ownership enforcement - verify by: integration tests for success, cross-Agent rejection, revoked credentials, resumability, and bounded streaming.
- [x] M3: Refactor the one-shot uploader and add a polling auto-uploader with durable checkpoints - verify by: tests for completed-session discovery, profile filtering, retry/backoff, restart, and idempotent completion.
- [x] M4: Wire Central and Processing PC compose services and document operations - verify by: compose config rendering and CLI/deployment contract tests.
- [x] M5: Run focused/full tests and a live Lab1-to-Central idempotent upload - verify by: COMPLETE catalog state, matching hashes, healthy heartbeat during upload, and persisted Edge checkpoint.
- [x] M6: Run the operator-approved one-hour first-time recording acceptance - verify by: a new four-source session finalizes after 3,600 seconds, the Agent remains healthy during capture and upload, Central reaches `COMPLETE`, and all Edge/Central artifact sizes and SHA-256 hashes match.

## Decisions Log

- 2026-08-24: Reuse the existing HTTPS Agent certificate, registry allowlist, and CRL rather than introducing a second credential system.
- 2026-08-24: Use a dedicated recording data ingress on port 8444 rather than adding multi-gigabyte PUT requests to the heartbeat process on port 8443; this preserves heartbeat availability and keeps the Admin API private.
- 2026-08-24: Reuse `RecordingCatalog` and `FilesystemObjectStore`; no new database or storage format is introduced.
- 2026-08-24: Keep the uploader read-only with respect to capture directories and persist transfer checkpoints under Edge runtime data.
- 2026-08-24: The operator approved a new one-hour immutable recording. Record RB5 joints plus D455, D405, and OTOS read-only; keep the low-overhead Central monitor running for 4,800 seconds so it covers the expected post-capture upload interval.

## Current State

All milestones are complete. M6 recorded session `rb5-lab1-observation-v2-20260824T160243Z-135ccda2` for 3,599.516 seconds and finalized 32,478,996,343 bytes with zero reported transport loss and zero timestamp regressions. The 4,800-second Central monitor passed 4,767 samples with no fetch errors, unhealthy samples, stale samples, or low-rate samples. Automatic mTLS upload completed on the first attempt; Central reports `COMPLETE` with 3/3 artifacts, and independent Edge/Central size and SHA-256 checks match. The canonical manifest digest is `3e2a658b831a09b2d762b4212686223084933998268021634037703bef89fba2` on both sides. This acceptance does not establish PTP/NTP offset or hardware-exposure-to-joint skew because the recorder clock quality remains `UNVERIFIED`. No robot, welder, feeder, or motion command surface was involved.
