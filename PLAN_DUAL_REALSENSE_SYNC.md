# Phase 3.8 - RB5 Dual RealSense Synchronization

## Goal

Validate software-level synchronization of RB5 measured joints with D455 and
D405 color streams on `processing-lab1`, deferring OTOS optimization.

## Scope

- D455 color and D405 color at 640x480, 30 fps target, JPEG ROS output.
- RB5 measured joint state at 30 Hz.
- Existing 25 Hz camera, 20 Hz joint, 250 ms gap, 50 ms p95 skew, and 150 ms
  maximum skew gates.
- Preserve read-only robot observation and collect host scheduling evidence.

## Non-Goals

- OTOS, RealSense depth/infrared, hardware trigger, PTP, robot commands, and
  welding equipment are excluded.

## Acceptance Criteria

- D455 and D405 publish decodable 640x480 JPEG at at least 25 Hz.
- RB5, D455, and D405 have zero timestamp regressions and clock steps.
- A 60-second preflight and uninterrupted 7,200-second stationary gate pass.
- RB5 and existing D455 containers are not recreated while D405 is added.

## Current State

D405 serial `351623070728` is present on USB 3. Its color endpoint is the
stable by-id `video-index4` node and advertises YUYV 640x480 at 30 fps.

## Milestones

- [x] M1: Add D405 to the camera, manifest, capture, QoS, and compose
  contracts; keep OTOS outside the active observation contract.
- [x] M2: Deploy to Lab1 without recreating the existing D455 or RB5 state
  containers and verify both camera payloads decode to 640x480 JPEG.
- [x] M3: Verify an interrupted probe writes an atomic summary with
  `interrupted: true` and exits non-zero.
- [x] M4: Pass the 60-second stationary preflight.
- [x] M5: Complete the one-time Phase 3.8 Fable review (`PASS`, no required
  revisions).
- [x] M6: Stop the final gate at the user's request after 6,953.5 seconds and
  evaluate the retained partial-run evidence.

## Decisions Log

- 2026-08-06: Validate only D455 color, D405 color, and measured RB5 joints.
  OTOS and RealSense depth/infrared streams remain configured for later work
  but are excluded from this gate.
- 2026-08-06: Keep the existing 25 Hz camera, 20 Hz joint, 250 ms maximum gap,
  50 ms p95 skew, and 150 ms maximum skew limits. No threshold was weakened
  after the first preflight exposed one startup-adjacent gap.
- 2026-08-06: Treat this as stationary software-timestamp alignment. Hardware
  exposure synchronization and motion-conditioned alignment remain separate
  future gates.
- 2026-08-06: The user ended the final gate 246.5 seconds early and requested
  analysis. Preserve the interrupted result without representing it as a
  7,200-second pass.

## Validation State

The repeated 60-second preflight passed: D405 30.017 Hz with 51.79 ms maximum
gap and 6.68 ms p95 joint skew; D455 29.965 Hz with 51.73 ms maximum gap and
15.84 ms p95 joint skew; RB5 30.001 Hz with 35.68 ms maximum gap. Timestamp
regressions and clock steps were zero. The detached final gate
`sang-phase38-dual-realsense-final` ran for 6,953.5 seconds before the requested
stop. It retained approximately 208,000 samples per stream and exposed three
simultaneous host-wide gaps above 250 ms. The strict gate therefore failed even
apart from the intentional interruption; detailed evidence and follow-up are
recorded in `docs/RB5_DUAL_REALSENSE_SYNC_RESULT_2026-08-06.md`.
