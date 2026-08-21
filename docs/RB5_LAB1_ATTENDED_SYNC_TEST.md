# RB5 Lab1 Attended Sync Test

## Scope

This runbook validates the `processing-lab1` observation stack with the RB5,
OTOS welding camera, and D455 color camera. No welding arc, wire feed, or
automatic robot command is permitted.

## Safety boundary

- The SANG observation Agent and sync probe are read-only.
- The official Rainbow Robotics ROS 2 driver is isolated on ROS domain 52.
- The upstream driver is pinned and patched from a 100% startup speed bar to 1%.
- The operator must keep the physical E-stop within reach and execute motion
  manually through the official UI. SANG does not click Plan or Execute.
- Stop immediately on unexpected motion, loss of camera data, or a controller
  alarm.

## Desktop launchers

- `RB5 Safe Simulation` validates the official MoveIt installation without a
  physical controller.
- `RB5 Hardware Control (Attended)` opens the physical control UI only after a
  safety checklist confirmation.
- `Stop RB5 Control UI` stops either official control container.

## Probe commands

Stationary 60-second preflight:

```bash
cd /home/lab1/sang/v2_ws
SYNC_PROBE_DURATION_S=60 SYNC_PROBE_MIN_MOVING_SAMPLES=0 \
  docker compose --env-file /home/lab1/sang/runtime/config/edge.env \
  -f docker/compose.rb5-lab1-observation.yaml \
  --profile sync-probe run --rm --no-deps sync-probe
```

Attended 10-minute motion capture:

```bash
cd /home/lab1/sang/v2_ws
SYNC_PROBE_DURATION_S=600 SYNC_PROBE_MIN_MOVING_SAMPLES=30 \
  docker compose --env-file /home/lab1/sang/runtime/config/edge.env \
  -f docker/compose.rb5-lab1-observation.yaml \
  --profile sync-probe run --rm --no-deps sync-probe
```

Stationary two-hour soak, only after the attended capture passes:

```bash
cd /home/lab1/sang/v2_ws
SYNC_PROBE_DURATION_S=7200 SYNC_PROBE_MIN_MOVING_SAMPLES=0 \
  docker compose --env-file /home/lab1/sang/runtime/config/edge.env \
  -f docker/compose.rb5-lab1-observation.yaml \
  --profile sync-probe run --rm --no-deps sync-probe
```

Keep the robot stationary and welding equipment disabled for the soak. A soak
passes only when every configured rate, gap, timestamp-regression, clock-step,
and nearest camera-to-joint skew check passes. An interrupted probe is a fail.

Results are written under
`/home/lab1/sang/runtime/processing-lab1/data/sync-probes`. Camera image payloads
are not duplicated; the JSONL contains timestamps and RB5 joint positions.

## Timestamp meaning

RB5 timestamps are assigned on the Lab1 host immediately after each vendor
data-channel read. Camera timestamps are assigned when OpenCV returns a frame,
not at physical exposure. The reported skew therefore measures software-level
alignment on one NTP-synchronized PC. Hardware trigger or PTP is required for a
true exposure-to-controller timing guarantee.

## Local MCAP capture

The MCAP worker is a separate read-only, one-shot Compose profile. It records
the RB5 measured joints and the original OTOS/D455 compressed ROS image bytes.
It does not decode images, publish commands, or write payloads to SQLite.

Run a 30-second preflight capture:

```bash
cd /home/lab1/sang/v2_ws
CAPTURE_DURATION_S=30 \
  docker compose --env-file /home/lab1/sang/runtime/config/edge.env \
  -f docker/compose.rb5-lab1-observation.yaml \
  --profile capture run --rm --no-deps capture
```

Completed sessions are written under
`/home/lab1/sang/runtime/processing-lab1/data/captures/<session_id>`.
`session-manifest.json` contains source-header ranges, per-topic counts, and
SHA-256 checksums. The hashed `recorder.log` must report zero transport message
loss. During capture the directory ends in `.partial`; Ctrl+C is forwarded as
a graceful recorder stop. If validation fails, the partial directory and a
`state=failed` manifest are retained for diagnosis and are never reported as a
complete session.

The current clock quality is deliberately `UNVERIFIED`: RB5 and camera headers
use the Lab1 host clock, but this worker does not yet collect PTP/NTP offset
evidence. Use the sync probe for rate, gap, regression, and software-level
camera-to-joint skew acceptance. A `complete` MCAP session means storage and
timestamp integrity passed; it does not by itself approve the data rate or
training-data quality.
