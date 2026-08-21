# Phase 4 OMY Isaac Live Observation Result

Date: 2026-08-07  
Scope: read-only OMY Isaac Sim observations into the secure heartbeat-v3 canary  
Result: PASS for the bounded live observation slice

## Runtime Contract

- Scene: ROBOTIS OMY composed USD with the recorded SHA-256 digest.
- ROS 2 domain: 99 with `LOCALHOST` automatic discovery.
- Topics: `/joint_states`, wrist compressed RGB and top compressed RGB.
- Cameras: 848x480 JPEG at a 30 Hz target.
- Agent: `sim-isaac-omy-01`, heartbeat schema v3,
  `runtime_kind=simulation`.
- Control: no exercise, command subscriber, service client, action client or
  hardware command path.

`scripts/manage_isaac_observation.sh` now owns bounded publisher lifecycle
through user systemd. It provides idempotent `start`, `status`, `restart` and `stop`,
rejects an unmanaged duplicate profile and records per-run log/report paths.
The checked-in environment selects a two-hour maximum duration; this is a
bounded runtime, not an automatically restarting daemon. A systemd
`RuntimeMaxSec` five minutes beyond the requested observation duration is an
independent ceiling for a stalled shutdown.

## Completed Evidence

The direct 120-second publisher run completed successfully and wrote:

`output/validation/isaac-omy-v3-canary-20260807.json`

- JointState: 3,600 samples at 30.0003 Hz.
- Top camera: 3,600 frames at 30.0062 Hz.
- Wrist camera: 3,600 frames at 30.0057 Hz.
- Both images were 848x480 with non-empty JPEG payloads and valid pixel
  statistics.
- `subscription_count=0`, `hardware_command_count=0`, `exercise=null` and
  `read_only=true`.

The v3 canary reached `READY` with all required sources fresh. A separate
25-sample Central observation window remained healthy:

- minimum JointState rate: 29.9911 Hz;
- minimum wrist camera rate: 29.9873 Hz;
- minimum top camera rate: 29.9736 Hz;
- maximum observed source age: 34.688 ms;
- sequence increase: 750 or 751 per source.

The managed lifecycle was also exercised. `stop` changed only OMY-sim to
`DEGRADED`; `start` restored it to `READY`. Calling `start` again retained the
same systemd PID. ROS graph inspection showed three application publishers,
the standard parameter-event publisher and no subscribers or action clients.
The simulation loop does hold the declared ready joint targets; this is
sim-side state maintenance and is not a hardware command path.

Unit tests passed: 11 tests for the vendor profile, asset digest, manifest
coverage, bounded exercise contract and ROS isolation. The USD directory must
be mounted at `/issacsim_setting_usd` when those tests run in Docker.

After the lifecycle hardening revision, another 25-sample Central window was
25/25 `READY`: all three minimum rates were 29.9929 Hz, maximum source age was
14.22 ms and heartbeat sequence regressions were zero. The ready marker is now
written only after the first successful observation publish. A focused Claude
Opus read-only review returned `PASS` with no critical findings.

## Current Run

At 2026-08-07 10:04 EDT, systemd unit
`sang-isaac-observation-omy.service` started a 7,200-second run with PID
2440972. It is intentionally left running so the Admin UI remains `READY` and
is expected to finish near 12:04 EDT. The independent systemd ceiling is
12:09 EDT. Its paths are:

- log: `runtime/isaac_vendor/omy/publisher-20260807T140400Z.log`;
- report after normal duration completion:
  `runtime/isaac_vendor/omy/report-20260807T140400Z.json`.

After either limit expires, the simulation Agent becoming `DEGRADED` is the
expected stale-topic result rather than an Agent failure. An interrupted or
externally capped run may have only its log and does not promise a final JSON
report.

Isaac emits non-fatal startup diagnostics about render settings, camera
aperture correction and occasional synthetic-data frame discard. They did not
violate the measured freshness/rate gate. The host also reports a CPU
`powersave` governor; this is a performance risk to revisit before the
five-runtime final gate, not a failure in this single-runtime run.

## Next Slice

Mapping Schema v2 is now implemented for the first safe target, SO-101. The
next slice is per-Agent OPC UA Gateway enrollment/runtime collision validation,
followed by OMY and RB5 mappings only after vendor-confirmed limits are recorded.
