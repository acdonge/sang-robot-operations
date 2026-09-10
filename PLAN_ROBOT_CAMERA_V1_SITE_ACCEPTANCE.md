# Robot/Camera Operations v1 Site Acceptance Gate

Date started: 2026-08-26
Status: Complete - PASS

## Goal

Accept the tagged observation-only Robot/Camera Operations v1 on the real lab topology without
adding or exercising robot, EEF, welding, feeder, or policy actuation.

## Done when

- Central survives a controlled service restart and exposes authenticated Admin/Manager surfaces.
- The real Processing PC reconnects with valid mTLS identity, fresh clock evidence and its approved
  RB5/camera manifest.
- Physical and simulation Agents can be observed concurrently, and stopping one non-authoritative
  Edge does not corrupt the other observations or Central.
- A browser-created Task Run completes one bounded capture, automatic upload, quality evaluation,
  synchronized multi-camera playback and idempotent observation-only LeRobot derivation.
- The live database and catalog-referenced objects pass backup verification and clean-path restore.
- One uninterrupted two-hour observation soak passes the frozen source, clock, reconnect and
  integrity criteria.

## Safety and data boundaries

- This gate is observation-only. It does not start a robot vendor controller, publish a robot
  command, move an axis, switch a robot mode, or energize process I/O.
- Existing databases, objects, captures and validation evidence are never deleted or rewritten.
- Failure injection is limited to a selected Agent/gateway process after confirming that it has no
  actuator command surface.
- New evidence is written below `runtime/site-acceptance-v1/`.

## Milestones

- [x] M1: Central and network preflight
  - Verify release/tag cleanliness, active services, health endpoints, disk capacity and DB
    integrity.
  - Record Processing PC reachability before any distributed test.
- [x] M2: Controlled Central restart and recovery
  - Snapshot counts and service state, restart only the v1 Central services, and prove that the
    same DB/catalog state returns without manual repair.
- [x] M3: Real Processing PC and asset admission
  - Verify mTLS heartbeat, peer identity, clock quality, approved manifest/calibration and selected
    camera preview for RB5, D405, D455 and OTOS.
- [x] M4: Multi-Agent and one-Edge fault isolation
  - Observe the available physical/simulation profiles concurrently and stop one read-only canary
    while all other snapshots continue.
- [x] M5: Browser-to-artifact end-to-end flow
  - Task Run -> bounded capture -> automatic upload -> quality -> playback -> observation-only
    LeRobot derivation, including idempotent retry.
- [x] M6: Backup, restore and operational limits
  - Verify the live backup, restore to empty paths, object hashes, free-space threshold, retention
    decision and security-audit handling.
- [x] M7: Uninterrupted two-hour soak and final verdict
  - Publish a PASS/FAIL report with exact topology, serials, manifest/calibration hashes, clock
    evidence, interruptions and residual risks.

## Current state

- Central Admin and Manager are running on `10.0.0.8`; both `/api/v1/healthz` endpoints returned
  `robot-camera-operations-v1 / ok` during the initial preflight.
- Agent and recording ingress are listening with client certificates required.
- Central controlled restart passed for Admin, Manager, Agent ingress and Recording ingress. Both
  online Agents continued or recovered, and DB/catalog counts were unchanged.
- Browser login, Fleet and Catalog recovered with 6 complete sessions, 18 artifacts, 3 quality
  reports and 1 observation-only LeRobot derivation.
- The read-only OMY Isaac heartbeat Agent was stopped and detected offline while the SO-101 and
  physical Lab1 Agents advanced and the catalog remained intact. OMY recovered with a new boot ID
  after restart. M4 passed.
- The existing 47 GiB backup passed an isolated read-only verification: schema versions 1 through
  11, 29 objects, 50,092,099,490 bytes and matching database SHA-256.
- Central filesystem usage is 87% with 254,773,911,552 bytes free after retaining the clean-path
  restore. The 300 GiB warning is active; the 150 GiB admission stop is not.
- Processing PC `lab1` rejoined as DHCP address `10.0.0.5`. Its mTLS Agent is online, the observed
  peer is `10.0.0.5`, and chrony is locked to Central `10.0.0.8` with a 2.085 ms RMS offset in the
  admission snapshot.
- OTOS, D455 and D405 are fresh at approximately 29.8, 30.03 and 29.99 Hz. All three selected-camera
  streams rendered through the Central authenticated on-demand relay. The D455 USB 2 color endpoint
  was corrected to persistent `video-index2` with UYVY input.
- RB5 retained its static address `10.0.2.7`; it is wired to the shared lab router rather than
  Lab1's disconnected `eth0`. Moving Lab1's `10.0.2.101/24` secondary address to `wlp0s20f3`
  restored the read-only data channel. `/rbpodo/joint_states` measured 30.0 Hz directly, and Central
  reports `READY`, fresh sources, healthy rates and 4/4 healthy inputs. M3 is complete.
- With all three Agents online, stopping the read-only OMY canary marked only that Agent offline.
  Lab1 camera rates and the SO-101 heartbeat continued; OMY recovered with a new boot epoch and the
  Central catalog counts remained unchanged. M4 is complete.
- Existing physical acceptance evidence completes M5 without repeating the same long capture. The
  browser-created Task Run restored through reload/restart, duplicate requests were idempotent, the
  final RB5 session uploaded with SHA-256 integrity and quality `PASS` / clock `LOCKED`, synchronized
  three-camera web playback passed, and its observation-only LeRobot v3 derivation completed with an
  idempotent retry.
- The verified backup was restored into the previously empty
  `runtime/site-acceptance-v1/restore-20260826T215236Z` path in a network-disabled container. An
  independent second pass found 29/29 objects, 50,092,099,490 object bytes, no missing, unexpected
  or hash-mismatched files, DB integrity `ok`, zero foreign-key violations and exact catalog
  references. M6 is complete.
- Site storage policy is frozen at a 300 GiB warning threshold and a 150 GiB admission stop for new
  capture/derivation work. Source MCAP, quality, derivation and audit records are never deleted
  automatically; archival requires a verified copy and explicit administrator approval. The
  retained restore leaves 254,773,911,552 bytes free, so warning is active but admission is open.
- Backup and restored DB files are mode `0600` under mode-`0700` roots, no private key/certificate/
  token file is present in the backup tree, and the point-in-time restore retained seven security
  plus twenty configuration audit rows. Anonymous remote registry access returned HTTP `401`; the
  unauthenticated Admin listener remains loopback-only.
- The strict M7 soak passed for 7,200.016 seconds and 7,144 samples: zero unhealthy samples, fetch
  errors, stale samples or low-rate samples. Minimum measured rates were RB5 26.059 Hz, OTOS 29.201
  Hz, D455 30.019 Hz and D405 29.982 Hz. Central and Lab1 service restart/OOM counters stayed zero.
- A concurrent 7,200.006-second clock/continuity monitor passed 1,439 samples with one unchanged boot
  ID, zero offline/unhealthy/unlocked samples, zero heartbeat/source sequence regressions and zero
  clock steps over 20 ms. Chrony stayed on Central `10.0.0.8`; maximum RMS offset was 1.126166 ms.
- Post-soak live DB integrity and all 29 catalog object hashes passed again. No health event was
  recorded for `processing-lab1` during the soak. M7 and the complete site Gate passed.

## Decisions log

- 2026-08-26: Keep the tagged v1 observation-only boundary for site acceptance. Unity safety,
  Control Mode Manager, AAS, welding assets and robot actuation remain outside this gate.
- 2026-08-26: Run non-destructive Central checks while Lab1 is offline; do not substitute stale
  heartbeat/catalog data for live physical evidence.
- 2026-08-26: Keep strict preview peer pinning. Rotate the Lab1 preview server certificate to include
  its current `10.0.0.5` DHCP address rather than weakening the relay's peer-address check.
- 2026-08-26: Admit the D455's observed USB 2 color contract as UYVY 640x480 at 30 fps on
  `video-index2`; do not treat the depth endpoint at `video-index0` as a color camera.
- 2026-08-26: Match RB5 routing to the physical topology. Keep the controller at `10.0.2.7` and put
  Lab1's `10.0.2.101/24` secondary address on the Wi-Fi interface connected to the shared router;
  do not infer an RB5 address change from the disconnected `eth0` path.
- 2026-08-26: Reuse the already-passed, immutable M5 evidence instead of repeating the same capture,
  upload, playback and derivation workflow. Treat a new short capture after today's D455/routing
  changes as optional regression evidence, not as an unimplemented feature.
- 2026-08-26: Use 300 GiB free as the site warning threshold and 150 GiB free as the stop-admission
  threshold. Do not auto-delete immutable source MCAP, quality, LeRobot derivation or audit records;
  archive only after hash verification and explicit administrator approval.
- 2026-08-26: Keep security/configuration audit rows indefinitely in v1, with a minimum operational
  retention target of 365 days. Until durable login throttling exists in Central, expose Manager
  only on the lab management subnet and preserve denied authentication attempts in the audit log.
- 2026-08-26: SQLite backup validation must use an immutable read-only connection so verification
  cannot leave WAL/SHM sidecars in backup or clean-restore paths.
- 2026-08-27: Accept Robot/Camera Operations v1 for this observation-only topology after M1 through
  M7 passed. This verdict does not authorize robot/process actuation or claim hardware-triggered
  camera exposure synchronization.
