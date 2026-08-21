# RB5 Lab1 Synchronization Hardening Result

## Status

**SUPERSEDED / INTERRUPTED.** The 7,200-second stationary final gate started at
`2026-08-06T17:42:30Z` (`13:42:30` America/New_York) in the detached container
`sang-phase37-sync-final`. Expected completion is approximately `15:42:30`
America/New_York. It was intentionally stopped at approximately 84 minutes
when the validation target changed to D455 + D405 + RB5. Raw sensor and host
JSONL evidence is retained; OTOS optimization is deferred.

## Implementation

- OTOS remains 1920x1080 with a 30 fps target and now publishes raw MJPG bytes
  without decode/re-encode.
- Up to 10 consecutive invalid MJPG frames are skipped before fail-closed
  reconnect. Skips are counted and warning logs are rate-limited to 60 seconds.
- Sync report schema 0.2 records bounded UTC gap events and correlates them
  with 1 Hz host telemetry: CPU, I/O wait, run queue, process scheduler wait,
  cgroup throttling, and Linux pressure-stall information.

## Verified Before Final Gate

- Related package tests: 121 passed.
- One-time Phase 3.7 Fable review: `PASS`, no critical or required revisions.
- Live OTOS ROS payload: JPEG markers valid, OpenCV-decodable 1920x1080 frame.
- RB5 and D455 observation containers were not recreated.
- 60.28-second preflight: `PASS`.
- RB5: 30.00 Hz, 37.44 ms maximum gap.
- D455: 29.97 Hz, 50.64 ms maximum gap, 15.86 ms p95 joint skew.
- OTOS: 29.72 Hz, 84.26 ms maximum gap, 2.89 ms p95 joint skew.
- Timestamp regressions and clock steps: 0.
- Host samples: 60, unavailable metrics 0, maximum probe scheduling lateness
  2.26 ms, cgroup CPU throttling 0.

## Final Acceptance

Pending the uninterrupted final report. Every configured rate, 250 ms maximum
gap, timestamp, clock-step, 50 ms p95 skew, 150 ms maximum skew, and
interruption check must pass. A completed container alone is not a pass; the
atomic summary JSON is authoritative.
