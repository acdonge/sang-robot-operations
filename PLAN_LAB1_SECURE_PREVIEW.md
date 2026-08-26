# PLAN - Lab1 Secure Selected-Camera Preview

Goal: Connect the Lab1 RB5 D455, D405, and OTOS WGC200 previews to Central through the existing single-camera relay using mutual TLS.

Done when:

- The Lab1 Agent advertises canonical `https://10.0.0.9:8101` preview URLs.
- The Edge preview server accepts only a client certificate issued by the lab HTTPS CA and pinned to the exact Central preview-client SHA-256 fingerprint.
- Central validates the Edge server certificate and relays one registered camera at a time.
- D455, D405, and OTOS are registered as bound camera assets and appear with relay URLs.
- A valid relay returns MJPEG while missing/untrusted client credentials are rejected.

Out of scope:

- Robot, EEF, welder, or feeder commands.
- MCAP capture/upload and the two-hour soak gate.
- Moving RB5 or changing camera exposure/calibration.

## Milestones

- [x] M1: Add deployment contracts for the Lab1 Edge and Phase 3 Central preview override - verify with focused tests and Compose validation.
- [x] M2: Issue and deploy role-separated Edge server and Central preview-client certificates - verify SAN, chain, key modes, and fingerprint.
- [x] M3: Restart only the Central admin and Lab1 Agent with mTLS preview enabled - verify heartbeat remains online and direct unauthorized TLS fails.
- [x] M4: Register the Lab1 Processing Node, RB5, and three camera assets - verify Central emits relay URLs only for the bound cameras.
- [x] M5: Exercise each camera through the Central relay and verify single-stream admission, then record site evidence.

## Decisions log

- 2026-08-23: Keep the current Phase 3 Central/UI at `127.0.0.1:18091`; add a narrow Compose override instead of migrating the live database to the Operations v1 remote profile.
- 2026-08-23: Advertise `10.0.0.9`, not the Lab1 Wi-Fi address `10.0.0.4`, because mTLS heartbeats reach Central through `eth0` and Central binds preview hosts to the observed peer address.
- 2026-08-23: OTOS remains optional for robot health, but all three cameras are eligible for explicit on-demand preview.

## Current state

All milestones are complete. The Edge rejects missing and unpinned certificates, Central relays each registered camera, the UI rendered a live 1920x1080 OTOS frame, and the one-stream lease was enforced and released correctly. Site evidence is recorded in `docs/RB5_LAB1_SECURE_PREVIEW_RESULT_2026-08-23.md`.
