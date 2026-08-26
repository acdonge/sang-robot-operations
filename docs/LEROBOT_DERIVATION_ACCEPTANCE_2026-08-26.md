# LeRobot Observation Derivation Acceptance - 2026-08-26

Status: **PASS**

## Scope

The acceptance run derived one bounded, observation-only LeRobot v3 episode from the immutable RB5
session `capture-c0d93d51f619448ca378a5ce3d669e64`. The source belongs to Task Run
`rb5-ui-capture-acceptance-20260824`, whose recording quality is `PASS` and clock state is `LOCKED`.
No robot, policy, welding or digital-output command interface was enabled.

## Result

| Field | Result |
|---|---|
| Derivation ID | `lerobot-130a79833af4a0ccc5ad5a5b` |
| Source interval | 10.0 through 15.0 seconds |
| Target rate | 30 Hz |
| Frames / episodes | 151 / 1 |
| Robot state | RB5 six-axis `observation.state` |
| Cameras | D405 640x480, D455 640x480, OTOS 1920x1080 |
| Action feature | absent |
| Output size | 6,799,360 bytes |
| Output SHA-256 | `a64fc9aba838d4a2cfd1834e8a190b6be4f9572b543f177ba385a27dabca9b59` |
| Manifest SHA-256 | `55af3f1b523504524ba9c78b458433c747b4c6882200754c0ea8ef770973bcd1` |

Maximum absolute source-to-target skew was 8.996 ms for D405, 11.978 ms for D455, 17.109 ms for
RB5 joints and 23.408 ms for OTOS. Every stream remained below the 50 ms admission limit.

## Verification

- The published tar download hash matched the cataloged output SHA-256.
- LeRobot `0.4.3` reopened the extracted dataset as v3 with 151 frames and one episode.
- The middle frame was timestamp 2.5 seconds with state shape `[6]` and three RGB video features.
- The feature set contained no `action`.
- Admin Catalog displayed one completed derivation, its segment, features, hash and download link.
- An identical request returned the existing immutable derivation rather than creating a duplicate.
- All 440 tests across the ten workspace package suites passed after schema migration 11.
- Backup `/home/sa08050/sang-backups/robot-camera-lerobot-v1-20260826T135014Z` independently
  verified schema versions 1 through 11 and 29 objects totaling 50,092,099,490 bytes.
- Static route and source inspection found no write endpoint, ROS publisher or vendor command in the
  derivation path.

The authoritative source remains MCAP. LeRobot video is an H.264 training derivative, and the
software timestamps do not claim camera hardware-trigger or PTP exposure synchronization.
