# RB5 Lab1 Synchronization Validation Result

## Verdict

**FAIL** for the complete validation sequence. The 60-second baseline and
10-minute attended motion test passed, but the two-hour stationary soak failed
four configured checks.

No welding arc, wire feed, or SANG-issued robot command was used. The operator
moved the RB5 at low speed through the Rainbow Robotics manufacturer UI during
the attended test.

## Identity and data isolation

- Active Agent ID: `processing-lab1`
- Active Lab1 database contains only `processing-lab1` events.
- Previous `workcell-soarm-lab1` state is preserved under archive paths and is
  not used by the active deployment.

## Passing stages

### 60-second stationary baseline

- RB5: 30.00 Hz
- D455: 29.96 Hz
- OTOS: 29.01 Hz
- Timestamp regressions: 0
- Clock steps: 0
- Maximum camera-to-joint skew: 37.62 ms

### 10-minute attended motion test

- Completed: 600.11 seconds, uninterrupted
- Moving joint samples: 4,295
- RB5: 30.00 Hz, maximum gap 62.41 ms
- D455: 29.96 Hz, maximum gap 66.62 ms
- OTOS: 27.37 Hz, maximum gap 149.17 ms
- D455 moving p95 skew: 16.00 ms
- OTOS moving p95 skew: 18.16 ms
- Timestamp regressions and clock steps: 0

Summary: `sync-probe-20260805T212948.373050Z.summary.json`

## Two-hour soak

The soak ran from `2026-08-05T21:40:47Z` to `2026-08-05T23:40:47Z`
(`17:40:47` to `19:40:47` America/New_York) and completed 7,200.29 seconds
without interruption, OOM, or service restart.

| Stream | Samples | Rate | p95 gap | Maximum gap | Regressions |
| --- | ---: | ---: | ---: | ---: | ---: |
| RB5 joint | 215,984 | 30.00 Hz | 34.04 ms | 513.76 ms | 0 |
| D455 color | 215,636 | 29.95 Hz | 34.13 ms | 509.30 ms | 0 |
| OTOS welding | 175,527 | 24.38 Hz | 68.95 ms | 522.98 ms | 0 |

| Camera-to-joint skew | p50 | p95 | Maximum | Result |
| --- | ---: | ---: | ---: | --- |
| D455 | 8.36 ms | 15.87 ms | 46.90 ms | PASS |
| OTOS | 10.39 ms | 28.28 ms | 83.83 ms | PASS |

Failed checks:

- `rb5_joint.max_gap`: 513.76 ms, limit 250 ms
- `d455_color.max_gap`: 509.30 ms, limit 250 ms
- `welding_view.max_gap`: 522.98 ms, limit 250 ms
- `welding_view.rate`: 24.38 Hz, minimum 25 Hz

The three maximum gaps occurred together at `2026-08-05 19:19:41.58 EDT`.
There was no timestamp regression, clock step, container restart, OOM, camera
reconnect, or kernel error at that time. This points to a transient host/probe
scheduling stall rather than three independent sensor failures, but the
available telemetry is not sufficient to assign a definitive root cause.

OTOS also crossed the 25 Hz health threshold repeatedly throughout the soak:
331 `topic_rate_low` and 332 `topic_rate_recovered` events were recorded. Its
current 1920x1080 MJPG configuration has `mjpg_passthrough: false`, so every
frame is decoded and JPEG-encoded again. The OTOS capture-to-arrival p95 was
50.02 ms, compared with 24.78 ms for the D455; re-encoding is therefore the
first optimization to test, not a reason to weaken the acceptance threshold.

Image-quality reduction is explicitly deferred. The next optimization and
validation pass keeps the OTOS 1920x1080 resolution, 30 fps target, and JPEG
quality 90. Lower resolution, frame rate, or JPEG quality may be evaluated
later as a task-specific bandwidth tradeoff, but must not be used to make the
current synchronization gate pass before MJPG passthrough and probe scheduling
are addressed.

The D455 showed no camera-specific fault: it sustained 29.95 Hz, had no
timestamp regression, reconnect, or rate-flapping event, and its p95 skew was
15.87 ms. It is not declared fully clean only because it participated in the
single 509.30 ms gap that affected all three streams simultaneously. Current
evidence does not identify the D455 as the cause of that common stall.

Summary: `sync-probe-20260805T214047.027904Z.summary.json`

## Required revision

1. Validate OTOS with MJPG passthrough and confirm the browser/ROS consumers
   accept the resulting JPEG stream without reducing image quality.
2. Add timestamps for over-limit gap events and lightweight host CPU, run queue,
   I/O wait, and process scheduling evidence to the soak report.
3. Repeat a short stationary benchmark, then rerun the two-hour soak with the
   existing 25 Hz and 250 ms limits.
