# RB5 Four-Source Recording, Storage, and Soak Result - 2026-08-23

## Status

**MCAP + CENTRAL STORAGE PASS / STRICT TWO-HOUR SOAK FAIL.** The selected observation segment was recorded and preserved correctly. The uninterrupted 7,200-second probe failed only the 250 ms maximum-arrival-gap checks after two cross-stream observer stalls and one smaller camera-only stall.

## Safety and Scope

- Processing Agent: `processing-lab1`
- Robot observation: RB5-850E measured joint state from the vendor data port at 30 Hz
- Cameras: Intel RealSense D455 and D405 color at 640x480/30 fps, plus OTOS WGC200 MJPG at 1920x1080/30 fps
- Safety: read-only observation; no robot command interface, vendor command channel, welder, feeder, arc, or motion was used
- Test posture: the robot remained stationary
- Synchronization boundary: source messages share the Lab1 host `CLOCK_REALTIME`; camera timestamps are host capture-return times, not hardware exposure timestamps. This test does not prove hardware-trigger or PTP synchronization.

## Runtime Preparation

- Added the OTOS topic to capture profile `rb5-lab1-observation-v2` and aligned all four `asset_id` values with the Central registry.
- Added OTOS to the reusable Lab1 synchronization-probe service.
- Enabled a persistent Central filesystem Object Store at `runtime/phase3-lab1/objects`, separate from SQLite metadata.
- Preserved existing v1 sessions and backed up both the remote capture/Compose files and the Central database before deployment.
- Focused deployment, profile, and Object Store tests: 28 passed.

## Sixty-Second Preflight

The four-source preflight passed every configured check.

| Stream | Rate | Maximum arrival gap | Timestamp regressions | Camera-to-joint p95 / maximum skew |
| --- | ---: | ---: | ---: | ---: |
| RB5 joints | 30.000 Hz | 34.73 ms | 0 | n/a |
| D455 color | 29.958 Hz | 50.48 ms | 0 | 15.78 / 16.69 ms |
| D405 color | 29.992 Hz | 50.46 ms | 0 | 7.88 / 9.02 ms |
| OTOS welding view | 29.733 Hz | 67.33 ms | 0 | 17.01 / 20.34 ms |

There were no zero timestamps, over-limit gaps, or system-clock steps.

## Ten-Minute MCAP

- Session: `rb5-lab1-observation-v2-20260823T192546Z-site10m`
- State: `complete`
- Record duration: 599.769 seconds
- Total size: 5,394,131,765 bytes
- Total messages: 71,796
- RB5 joints: 17,993
- D455 color: 17,970
- D405 color: 17,989
- OTOS welding view: 17,844
- Transport messages lost: 0
- Header timestamp regressions and zero timestamps: 0 for every topic
- Local SHA-256 verification: all three declared artifacts matched

The concurrent 600-second synchronization probe also passed every threshold:

| Stream | Rate | Maximum arrival gap | Timestamp regressions | Camera-to-joint p95 / maximum skew |
| --- | ---: | ---: | ---: | ---: |
| RB5 joints | 30.000 Hz | 59.93 ms | 0 | n/a |
| D455 color | 29.963 Hz | 57.16 ms | 0 | 15.83 / 16.91 ms |
| D405 color | 29.993 Hz | 76.63 ms | 0 | 15.92 / 16.76 ms |
| OTOS welding view | 29.752 Hz | 68.00 ms | 0 | 17.85 / 20.09 ms |

Probe host CPU was 10.93% at p95 and 13.56% maximum, I/O wait was 0.19% at p95, and there were no cgroup throttles or clock steps.

## Central Object Store

The finalized Edge session was transferred over the Lab1 1 Gb/s Ethernet link at approximately 32 MB/s. Central staging revalidated every source hash before upload.

- Catalog state: `COMPLETE`
- Completed artifacts: 3 of 3
- SQLite metadata size: 5,394,131,765 bytes
- Object payloads: `metadata.yaml`, MCAP, and `recorder.log`
- Canonical Central manifest: `session-manifest.json`
- Object Store SHA-256: all payload hashes match SQLite expectations
- Canonical manifest SHA-256: `c29167c5f3b89b189596ec1a52dd799a7fedac74a2095fadd7d77f6e6e4159be`, matching the catalog

The live Admin API remained bound to loopback. For this site acceptance, Central pulled the completed session over SSH and ran the existing uploader locally; remote HTTPS uploader identity was not exercised.

Follow-up on `2026-08-24`: the same session was discovered from Lab1 by the new dedicated Agent-mTLS automatic uploader on port `8444` and durably checkpointed as `COMPLETE` without re-hashing or retransmitting already-complete objects. See `AUTOMATIC_EDGE_RECORDING_UPLOAD_RESULT_2026-08-24.md`.

## Two-Hour Soak

The same probe process ran without interruption from `2026-08-23T19:41:57Z` to `21:41:57Z` for 7,200.110 seconds. The container was not OOM-killed, and all six Lab1 observation services retained restart count zero.

| Stream | Samples | Rate | p95 / maximum arrival gap | Maximum header gap | Timestamp regressions | Camera-to-joint p95 / maximum skew |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| RB5 joints | 215,974 | 29.998 Hz | 33.63 / **365.44 ms** | 200.20 ms | 0 | n/a |
| D455 color | 215,708 | 29.962 Hz | 33.61 / **378.68 ms** | 233.72 ms | 0 | 15.85 / 39.03 ms |
| D405 color | 215,917 | 29.991 Hz | 33.65 / **380.64 ms** | 233.46 ms | 0 | 15.79 / 29.10 ms |
| OTOS welding view | 214,084 | 29.736 Hz | 49.95 / **371.34 ms** | 231.79 ms | 0 | 16.59 / 48.91 ms |

All rate, zero-timestamp, timestamp-regression, clock-step, p95-skew, and maximum-skew checks passed. The failed checks were only the four stream maximum-arrival-gap checks against the 250 ms limit. There were no clock steps.

Three over-limit event clusters were observed:

- `20:49:40Z`: D455 260.32 ms and OTOS 253.66 ms; RB5 and D405 remained below the limit.
- `21:08:07Z`: all four callbacks paused together for 323.98-341.18 ms. The nearest host sample recorded 46.36 ms scheduler lateness and 392.88 ms of probe-process runtime.
- `21:30:53Z`: all four callbacks paused together for 365.44-380.64 ms. The nearest host interval recorded 410.00 ms of probe-process runtime.

No camera, RB5 adapter, Agent, or kernel log reported a reconnect, USB failure, network error, OOM, or service restart in any event window. Host CPU was 10.28% at p95 and 30.76% maximum, I/O wait was 0.13% at p95 and 0.44% maximum, and cgroup throttling remained zero.

The common callback pauses are not mirrored by equally large source-header gaps: every maximum header gap remained below 234 ms. The current probe writes one line-buffered JSON record from its single ROS callback thread for every message and retains roughly 862,000 `Sample` objects for end-of-run calculations. Combined with the common timing and 393-410 ms process-runtime spikes, this is evidence that the observer itself likely stalled. It does not prove that the sensors stopped, but the strict arrival-gap contract must remain failed until a lower-overhead probe repeats the test.

Summary evidence: `runtime/phase3-lab1/evidence/sync-probe-20260823T194157.377457Z.summary.json`. The full raw sample and host telemetry files remain on Lab1 under `/home/lab1/sang/runtime/processing-lab1/data/sync-probes/`.

## Decision

The selected-segment recording and Central persistence path is accepted for this Lab1 topology. The strict two-hour arrival-continuity gate is not accepted.

Before repeating the long gate, decouple probe evidence writing from ROS callbacks, use bounded/compact online statistics instead of retaining every Python object, and rerun first on the final Processing PC. Do not weaken the 250 ms threshold. A later moving-robot test and hardware exposure/PTP design are still required before claiming physical multi-device synchronization.
