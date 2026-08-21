# RB5 Dual RealSense Synchronization Result

## Status

**STOPPED BY REQUEST / STRICT GATE FAILED.** The final gate ran for 6,953.5
seconds (1 hour 55 minutes 53.5 seconds), 246.5 seconds short of the requested
7,200 seconds. The probe handled the requested stop correctly, wrote an atomic
summary with `interrupted: true`, and exited with status 1. Raw and summarized
evidence remain on Lab1.

## Scope

- RB5-850E measured joint state from the read-only vendor data adapter.
- Intel RealSense D455 color and D405 color at 640x480 and 30 fps target.
- ROS 2 JPEG transport using host capture timestamps.
- OTOS, depth/infrared, robot commands, welding, hardware trigger, and PTP are
  excluded.

This gate measures software-level timestamp alignment. It does not prove that
the two cameras exposed their sensors at the same physical instant.

## Deployment Evidence

- D405 serial: `351623070728`.
- D405 stable color device: RealSense D405 by-id `video-index4`.
- Existing D455 container remained `46ceef8ce8bd`.
- Existing RB5 state container remained `74e35a4324e1`.
- D405 container: `a7d1b0571a2e`.
- Central heartbeat reported `READY` with all three required sources fresh.
- D405 live payload: marker-valid, OpenCV-decodable 640x480 JPEG at 31.13 Hz.
- D455 live payload: marker-valid, OpenCV-decodable 640x480 JPEG.

## Verification Before Final Gate

- Related local package tests: 141 passed.
- Focused Lab1 deployment/probe tests: 7 passed.
- Controlled interruption after 4.56 seconds wrote an atomic summary with
  `interrupted: true`, retained all stream samples, and exited with status 1.
- One-time Phase 3.8 Fable review: `PASS`, no critical or required revisions.

The first 60-second preflight failed on one startup-adjacent event at about
2.2 seconds: RB5 had a 360.71 ms arrival gap and D405 had a 362.34 ms arrival
gap. It had no timestamp regression or clock step, and the remaining stream
statistics were stable. The gate thresholds were not changed.

The fresh repeated 60-second preflight passed every configured check:

| Stream | Rate | Maximum gap | Timestamp regressions | Camera-to-joint p95 | Camera-to-joint max |
| --- | ---: | ---: | ---: | ---: | ---: |
| RB5 joints | 30.001 Hz | 35.68 ms | 0 | n/a | n/a |
| D455 color | 29.965 Hz | 51.73 ms | 0 | 15.84 ms | 16.81 ms |
| D405 color | 30.017 Hz | 51.79 ms | 0 | 6.68 ms | 8.49 ms |

System clock steps were zero, host CPU p95 was 37.27%, and there were no
failed checks. Early raw samples from the final gate also showed all streams at
approximately 30 Hz, maximum gaps below 53 ms, and zero timestamp regressions.

## Partial Final-Gate Result

The partial gate retained approximately 208,000 messages per stream:

| Stream | Rate | p95 gap | Maximum gap | Gaps over 250 ms | Timestamp regressions |
| --- | ---: | ---: | ---: | ---: | ---: |
| RB5 joints | 29.998 Hz | 34.46 ms | 342.08 ms | 3 | 0 |
| D455 color | 29.959 Hz | 35.20 ms | 322.07 ms | 3 | 0 |
| D405 color | 29.988 Hz | 34.72 ms | 349.52 ms | 3 | 0 |

The normal timing distribution remained tight. At p99.9, arrival gaps were
37.48 ms for RB5, 52.27 ms for D455, and 51.60 ms for D405. All three streams
exceeded 250 ms at the same three UTC times: approximately `20:13:27`,
`20:29:19`, and `20:49:29`.

Camera-to-joint software timestamp alignment passed its configured limits:

| Scope | D455 p95 / max | D405 p95 / max |
| --- | ---: | ---: |
| All matched samples | 15.85 / 46.64 ms | 15.88 / 34.01 ms |
| RB5 moving samples | 15.70 / 16.92 ms | 16.16 / 16.85 ms |

The adapter observed 2,150 moving joint samples over a 120.3-second window.
Within that window the nearest D405-to-D455 timestamp difference was 15.95 ms
at p95 and 16.98 ms maximum. No robot command originated from this system.

## Root-Cause Assessment

The three failures are consistent with host-wide scheduling stalls rather than
an individual USB camera fault:

- Each event affected RB5, D455, and D405 together; their event arrival times
  were within 13 ms of one another.
- System CPU usage reached 83.39% with 73.20% p95. On the 16-core host, runnable
  tasks reached 47 with p95 29, CPU pressure p95 was 21.55%, and process
  scheduler wait p95 was 32.80 ms.
- There was no probe-container CPU throttling, timestamp regression, system
  clock step, out-of-memory event, or USB kernel warning.
- During nearly the entire gate, the OTOS-related `wgc200-win11` QEMU virtual
  machine used about two CPU cores and 8 GiB RAM. The Rainbow `RB_Linux` UI
  used about 1.6 cores, while the two camera encoders together used about 2.25
  cores. These are current/process-lifetime observations rather than exact
  per-event attribution, but they agree with the host-pressure evidence.

The evidence therefore supports **good average and moving software alignment,
but not a hard 250 ms no-gap guarantee under the current mixed workload**.

## Recommended Follow-up

1. Run an isolated 30-minute confirmation with the OTOS Windows VM stopped and
   the Rainbow UI closed when it is not needed. Keep the read-only RB5 adapter,
   both RealSense camera nodes, Agent, and probe only.
2. If the isolated run passes, reserve CPU cores for camera, RB5, Agent, and
   probe containers; pin the Windows VM and operator applications to other
   cores before repeating a two-hour gate.
3. Keep gap events as dataset quality metadata so recording episodes that
   include a host stall can be rejected or segmented before VLA training.
4. Treat hardware trigger/PTP and real-time control as separate work. This
   result validates host timestamps, not simultaneous sensor exposure or a
   hard real-time robot control loop.

## Final Acceptance

`FAIL` against the original strict contract. The run was intentionally
interrupted and all three streams also violated the 250 ms maximum-gap limit.
Rate, timestamp integrity, clock stability, and camera-to-joint skew checks
passed.

## User Disposition

On 2026-08-06 the user accepted the CPU-contention finding for Lab1 because
that laptop will not be the production Processing PC, and authorized Phase 4
work to continue. The isolated performance rerun is therefore deferred until
production Processing hardware is selected. This disposition does not change
the strict result above from `FAIL` to `PASS`.
