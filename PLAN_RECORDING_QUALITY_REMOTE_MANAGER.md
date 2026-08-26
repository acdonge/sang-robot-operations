# PLAN - Recording Quality, Time Status, and Remote Manager

Goal: Turn completed MCAP uploads into quality-gated training evidence, validate the RB5 Lab1 camera stack for ten minutes, and expose the Manager UI remotely with HTTPS and authentication.

Done when:

- storage integrity and observation quality are separate states;
- every complete camera session can produce a bounded, immutable quality report with configured and measured resolution/rate, frame gaps, timestamp regressions, JPEG size, and camera-to-joint skew;
- the Catalog shows per-session and per-camera PASS/WARNING/FAIL evidence;
- clock synchronization status is recorded and visible rather than left implicit;
- RB5, D405, D455, and OTOS complete a ten-minute attended capture check, or any physical blocker is reported with exact evidence;
- Manager access is available over HTTPS with login and role authorization, never unauthenticated remote HTTP;
- robot, vendor, welding, and feeder command routes remain absent.

Out of scope: automatic camera quality reduction, one-hour soak, hardware-trigger synchronization, PTP rollout, LeRobot conversion, actuator control, welding control, and AAS authoring.

## Milestones

- [x] M1: Implement bounded MCAP quality analysis, durable summary/detail storage, API, and tests.
- [x] M2: Add Catalog quality summaries and per-camera evidence; verify in a browser.
- [x] M3: Collect and project Central/Processing NTP status into quality evidence and UI.
- [x] M4: Restore the Lab1 D455 stream and run the ten-minute RB5/D405/D455/OTOS validation.
- [x] M5: Deploy authenticated HTTPS Manager access on the lab network and verify viewer/admin boundaries.
- [x] M6: Run regression/security checks and publish acceptance evidence.

## Decisions Log

- 2026-08-24: Keep storage `COMPLETE` independent from data quality; a valid file can contain unusable observations.
- 2026-08-24: Analyze existing immutable MCAP objects without rewriting them. Persist a searchable summary in SQLite and an immutable detailed report in the Object Store.
- 2026-08-24: Detect configuration drift but do not auto-adjust camera FPS, resolution, or JPEG quality.
- 2026-08-24: Reuse existing local authentication, HTTPS secret references, host allowlisting, and Lab NTP components.
- 2026-08-24: Skip the one-hour soak at the user's request; retain only the ten-minute attended validation.
- 2026-08-24: Bind the Lab1 D455 through its current stable V4L by-id link; the prior serial-bearing link no longer existed after USB re-enumeration.
- 2026-08-24: Run a non-adjusting Chrony monitor on Processing Lab1 so Agent and recording evidence contain measured Central offset without granting `SYS_TIME`.
- 2026-08-24: Keep local development HTTP on loopback and expose a separate authenticated Manager at `https://10.0.0.8:18443/`.

## Current State

All milestones are complete. D455 sustained 29.99 Hz through the fresh 600-second four-source capture; the independent synchronization probe passed, automatic upload completed in one attempt, storage became `COMPLETE`, and automatic quality became `PASS` with zero failures or warnings. Authenticated HTTPS Manager access is live while local HTTP remains loopback-only. The final 294-test regression suite and Compose/browser/security checks pass. Acceptance evidence is in `docs/RECORDING_QUALITY_REMOTE_MANAGER_ACCEPTANCE_2026-08-24.md`.
