# Recording Quality, Time Status, and Remote Manager Acceptance

Date: 2026-08-24  
Verdict: **PASS**  
Scope: quality gates, clock evidence, one attended ten-minute Lab1 validation, and authenticated remote Manager access. The requested one-hour soak was not run.

## Implemented Boundary

- Storage integrity and observation quality are independent states. A session can remain `COMPLETE` while its quality is `PASS`, `WARNING`, or `FAIL`.
- Completed MCAP sessions are scanned once with bounded message and record-size limits. SQLite stores searchable quality summaries; the Object Store keeps one immutable JSON report addressed by SHA-256.
- Quality evidence includes configured and measured dimensions, source rate, p95/max source gap, timestamp regressions, JPEG byte distribution, and nearest camera-joint p95/max skew.
- Central and Processing clock state is visible through `/api/v1/time-status` and frozen into each recording manifest before capture.
- The Lab1 Processing PC runs a non-adjusting Chrony observer (`chronyd -x`) without `SYS_TIME`; the host remains synchronized by its existing time service.
- Remote Manager is a separate authenticated HTTPS listener. Local development HTTP remains loopback-only.
- No robot, vendor, welding, wire-feeder, ROS action, or actuator command route was added.

## D455 Recovery

The old Lab1 environment referred to a serial-bearing V4L by-id path that no longer existed after USB re-enumeration. The D455 container retained the old device major/minor and produced approximately one frame per ten seconds.

The environment was backed up and changed to the currently valid D455 color link:

`/dev/v4l/by-id/usb-Intel_R__RealSense_TM__Depth_Camera_455_Intel_R__RealSense_TM__Depth_Camera_455-video-index0`

Before the ten-minute run, direct V4L2 and ROS 2 checks both sustained 30 fps. The recreated container had zero restarts and no subsequent read timeout. This link is adequate while Lab1 has one D455; a second identical D455 will require a custom USB-path udev alias because the current link omits the serial number.

## Ten-Minute Physical Validation

Session: `rb5-lab1-observation-v2-20260824T201414Z-186c0675`  
Capture interval: `2026-08-24T20:14:14.799208Z` to `2026-08-24T20:24:15.070848Z`  
Independent probe duration: `600.112439 s`  
Safety: observation-only; no RB5 movement command was sent.

| Stream | Resolution | Measured rate | p95 / max source gap | p95 / max joint skew | Regressions | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| RB5 joint state | n/a | 30.0000 Hz | 42.54 / 43.96 ms | n/a | 0 | PASS |
| D405 color | 640x480 | 29.9927 Hz | 33.50 / 35.77 ms | 15.81 / 16.81 ms | 0 | PASS |
| D455 color | 640x480 | 29.9867 Hz | 33.50 / 36.55 ms | 15.80 / 17.72 ms | 0 | PASS |
| OTOS welding view | 1920x1080 | 29.7416 Hz | 36.08 / 100.01 ms | 6.95 / 36.64 ms | 0 | PASS |

All streams had zero 250 ms gap events and zero missing/zero header timestamps. Host CPU was 11.34% p95 and 16.24% maximum during the independent probe; no cgroup throttling or I/O pressure event was observed.

Capture-start clock evidence was `LOCKED`, stratum 4 against Central `10.0.0.8`, with a measured system offset of `-1.965 ms` and root dispersion of `3.918 ms`. The probe detected zero clock steps over its 20 ms threshold.

## Storage and Quality Evidence

- Central state: `COMPLETE`
- Total session bytes: `5,351,767,786`
- MCAP bytes: `5,351,761,935`
- Manifest SHA-256: `39cd5e27935635c7252286cdcba6f7c91fd93e9ab1e7ec433bab0f8f54453b45`
- MCAP SHA-256: `d509c08d517c305c6d6badb594db7dcfa844f4e80d14f0e356b69fda81306c50`
- Quality report SHA-256: `83f339678c4f3d8425d78b79a54b85b0e497a032a556d565be155f6a911fdb39`
- Automatic upload: one attempt, no error, completed at `2026-08-24T20:30:29.881Z`
- Automatic quality analysis: `PASS`, four passing streams, zero failed checks, zero warnings

The downloaded report hash matched its Object Store filename. The Catalog showed `COMPLETE / Q PASS`, the per-stream values above, and all three recorded cameras on one `09:59.7` playback timeline.

## Remote Manager Security

- URL: `https://10.0.0.8:18443/`
- Certificate SAN: `central.sang.local`, `10.0.0.8`
- Certificate validity: 2026-08-05 through 2027-08-05
- Lab1-to-Central OpenSSL verification with the public Root CA: `Verify return code: 0 (ok)`
- Login page without a session: HTTP 200
- Protected API without a session: HTTP 401
- Administrator login and identity lookup: PASS (`manager-admin`, role `admin`)
- Session cookie: `Secure`, `HttpOnly`, `SameSite=Strict`; CSRF cookie: `Secure`, `SameSite=Strict`
- Browser headers: HSTS, CSP, `X-Frame-Options: DENY`, `nosniff`, no-referrer, and disabled camera/microphone/geolocation permissions
- Local development HTTP: `127.0.0.1:18091` only
- Remote Manager: `0.0.0.0:18443` only, with authentication enabled

The remote browser must trust the public `root/ca-cert.pem`. The Root private key is not distributed. The existing intermediate CA omits an Authority Key Identifier; OpenSSL/curl verification succeeds, but Python 3.13 strict X.509 mode rejects that legacy intermediate. Reissue the intermediate with AKI during the next planned PKI rotation before supporting strict Python Manager clients.

## Verification

- Docker Compose interpolation passed for Central, secure Manager, and Lab1 observation profiles.
- Browser verification passed for Catalog quality states, detailed evidence, immutable report link, and synchronized three-camera MCAP playback.
- Full Python regression suite: `294 passed`, with one upstream FastAPI `TestClient` deprecation warning.
- `git diff --check` passed.

## Limits

- Camera timestamps are host `CLOCK_REALTIME` when OpenCV returns a frame, not hardware exposure timestamps.
- RB5 timestamps are assigned immediately after the vendor data read, not by a controller hardware clock.
- This validates software synchronization for the current Lab1 data path; it does not claim hard real-time behavior, PTP, or hardware-trigger synchronization.
- The one-hour soak and automatic camera quality reduction remain intentionally deferred.
