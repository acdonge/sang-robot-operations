# Phase 3.7 - Lab1 Synchronization Hardening

## Goal

Remove OTOS JPEG re-encoding, capture lightweight host scheduling evidence,
and pass the existing Lab1 synchronization gate without reducing image
resolution, frame rate target, or JPEG quality.

## Scope

- Enable raw MJPG passthrough for the OTOS 1920x1080 30 fps stream.
- Verify ROS consumers receive valid JPEG bytes that decode to 1920x1080.
- Add timestamped over-limit sensor gap events to the sync summary.
- Sample CPU use, I/O wait, run queue, process scheduling wait, cgroup CPU
  throttling, and Linux pressure-stall information at a bounded interval.
- Correlate each over-limit gap with the nearest host sample.
- Run focused tests, a 60-second stationary preflight, and a two-hour
  stationary final gate at the existing 25 Hz and 250 ms limits.

## Non-Goals

- No robot motion command, welding arc, wire feed, or vendor control action.
- No camera resolution, target frame rate, or image-quality reduction.
- No hardware-trigger or PTP exposure-time guarantee in this phase.
- No multi-Edge Phase 4 rollout before the Lab1 gate passes.

## Acceptance Criteria

- OTOS publishes marker-valid, OpenCV-decodable 1920x1080 JPEG payloads by
  passthrough and sustains at least 25 Hz in both validation runs.
- The report contains bounded gap events with UTC timestamps and nearest host
  evidence, plus aggregate host telemetry.
- Timestamp regressions and clock steps remain zero.
- Every configured rate, gap, skew, and interruption check passes for 60
  seconds and then for 7,200 uninterrupted seconds.
- RB5 state and D455 observation services are not recreated by the OTOS change.

## Milestones

- [x] M1: Probe telemetry implementation and focused tests (22 passed).
- [x] M2: OTOS passthrough deployment and JPEG compatibility check.
- [x] M3: 60-second stationary preflight.
- [x] M4: One-time phase-end Fable review (`PASS`, no required revisions).
- [ ] M5: Two-hour stationary final gate and result document (running).

## Decisions Log

- 2026-08-06: Keep the existing 1920x1080, 30 fps target, 25 Hz acceptance
  threshold, and 250 ms gap limit. Optimization must remove duplicate encoding
  rather than weaken the data-quality contract.
- 2026-08-06: Treat this as software-level host timestamp validation. Hardware
  exposure/controller synchronization remains explicitly unverified.

## Current State

M1-M3 are complete. The 60-second preflight passed every check: OTOS 29.72 Hz
with 84.3 ms maximum gap and 2.89 ms p95 joint skew, with no timestamp
regression or clock step. Host telemetry recorded 60 samples with 2.26 ms
maximum probe scheduling lateness and no cgroup CPU throttling. Next: the
two-hour gate. The one-time Fable review passed; its optional observability
suggestion was adopted by counting and logging skipped invalid MJPG frames.
The detached final gate `sang-phase37-sync-final` started at
2026-08-06T17:42:30Z with the exact 7,200-second command and no motion sample
requirement. Do not deploy or restart observation services until it exits.
