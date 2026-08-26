# PLAN - Lab1 Four-Source Capture, Central Upload, and Soak

Goal: Record RB5 joint state plus D455, D405, and OTOS video on Lab1, preserve the finalized session in Central Object Storage, and run a two-hour read-only synchronization soak.

Done when:

- a new four-topic capture profile is deployed without changing any command surface;
- one ten-minute session finalizes with all four topics, zero reported transport loss, valid timestamps, and matching SHA-256 hashes;
- Central catalogs the session as `COMPLETE` and independently verifies every immutable Object Store artifact;
- a 7,200-second four-source synchronization probe completes and its measured rates, gaps, regressions, and camera-to-joint skew are recorded;
- all involved observation services remain running without restart or OOM.

Out of scope: robot motion, RB5 command channels, welding/feeder I/O, hardware-trigger synchronization, PTP deployment, LeRobot conversion, and OTOS image-quality tuning.

## Milestones

- [x] M1: Four-source runtime profile and persistent Object Store are configured - verify by focused tests, Compose rendering, and live service health.
- [x] M2: Ten-minute MCAP is finalized on Lab1 - verify by manifest validation, topic/message counts, loss gate, timestamps, and local artifact hashes.
- [x] M3: Finalized session is uploaded and cataloged centrally - verify by `COMPLETE` API state plus central object size and SHA-256 checks.
- [x] M4: Two-hour four-source read-only soak completes - verify by the generated sync-probe summary and unchanged service restart/OOM counters. Strict max-arrival-gap gate failed.
- [x] M5: Site evidence is documented and the end-to-end state is rechecked.

## Decisions Log

- 2026-08-23: Use profile ID `rb5-lab1-observation-v2` because adding OTOS changes the capture contract; keep all v1 sessions immutable.
- 2026-08-23: Align capture `asset_id` values with the Central registry IDs created for this site.
- 2026-08-23: Keep the live Admin API loopback-only. Pull the finalized Edge session over SSH and exercise the existing uploader locally against loopback rather than exposing a new remote upload listener during this acceptance run.
- 2026-08-23: Run all checks observation-only with the robot stationary; the soak validates transport/timestamp behavior, not moving-robot synchronization.

## Current State

All five milestones are complete. The recording and Central persistence acceptance passed, while the strict two-hour max-arrival-gap gate failed and remains open. Final runtime checks show Central healthy, Agent `online/READY`, all four sources fresh near 30 Hz, and restart/OOM counts at zero. The next iteration must reduce sync-probe observer overhead and repeat the unchanged timing gate on the final Processing PC.
