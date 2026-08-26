# RB5 Dual RealSense Recording Result - 2026-08-23

## Status

**MCAP PASS / STRICT SYNC FAIL.** A five-minute read-only recording completed
with valid checksums and zero transport loss. The concurrent synchronization
probe failed only the D455 maximum-gap check: 286.77 ms against the 250 ms
limit. The two-hour soak was not started.

## Scope

- Processing Agent: `processing-lab1`
- Robot state: RB5-850E vendor data channel at 30 Hz
- Cameras: D455 color and D405 color at 640x480, 30 fps
- Excluded: OTOS, depth/infrared, robot commands, welding, and external display
- Safety: observation only; no vendor command channel was opened

The unused OTOS `v4l2loopback` module was unloaded for this runtime, removing
`/dev/video100` and `/dev/video101`. Its boot configuration was retained so the
OTOS work can be resumed later.

## Recovery Before Test

The Dell Thunderbolt dock disconnected and reconnected during the first
preflight. NetworkManager removed the secondary RB5 address
`10.0.2.101/24` from `eth0`, which made the controller data route fall back to
Wi-Fi and stopped joint-state updates. The `rb5-net` Compose service now checks
the address every second and reapplies it after an interface reconnect. The
read-only state process was restarted once after restoring the route.

The first two preflights exposed D455-only gaps. Recreating only the D455
container refreshed its USB device binding. The following 60-second preflight
passed:

| Stream | Rate | Maximum gap | Timestamp regressions | Joint skew p95 / max |
| --- | ---: | ---: | ---: | ---: |
| RB5 joints | 30.000 Hz | 35.09 ms | 0 | n/a |
| D455 color | 29.958 Hz | 50.97 ms | 0 | 15.73 / 16.94 ms |
| D405 color | 29.992 Hz | 48.75 ms | 0 | 12.73 / 13.37 ms |

## Five-Minute Recording

- Session: `rb5-lab1-observation-v1-20260823T175523Z-71203435`
- State: `complete`
- Duration: 299.619 seconds
- Size: 1,137,819,030 bytes (1.1 GiB)
- Messages: 26,927 across three topics
- RB5 joints: 8,985
- D455 color: 8,963
- D405 color: 8,979
- Transport messages lost: 0
- Header timestamp regressions: 0 on every topic
- SHA-256: all recorded-file hashes match `session-manifest.json`

The Central Agent inventory discovered the session as `complete`, with no
partial or invalid session left by this run.

## Concurrent Synchronization Probe

The probe ran uninterrupted for 300.10 seconds while MCAP was recording.

| Stream | Rate | Maximum gap | Timestamp regressions | Joint skew p95 / max |
| --- | ---: | ---: | ---: | ---: |
| RB5 joints | 29.993 Hz | 149.77 ms | 0 | n/a |
| D455 color | 29.918 Hz | **286.77 ms** | 0 | 15.84 / 130.02 ms |
| D405 color | 29.965 Hz | 150.23 ms | 0 | 15.42 / 52.58 ms |

All configured checks passed except `d455_color.max_gap`. Host CPU was not
saturated (21.61% p95, 30.34% maximum), and there were no clock steps. The
result therefore validates recording integrity and normal-case software
alignment, but it does not satisfy the strict no-gap timing contract.

## Decision

- Keep the recording as valid captured evidence with a timing-quality warning.
- Do not claim a strict synchronization pass or start the two-hour soak.
- Before the next long gate, inspect the D455 cable/USB path and repeat an
  isolated recording-load preflight. Do not weaken the 250 ms threshold.
