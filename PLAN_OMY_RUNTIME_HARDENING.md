# PLAN - OMY Runtime Hardening

Goal: Prevent autonomous-looking OMY motion and fail closed when simulation pose locking or Real-to-Sim input becomes invalid.

Done when:
- The orchestrated Player cannot report ready without a valid OMY idle lock.
- The orchestrated Player never opens UDP 5055.
- Manual Real-to-Sim rejects malformed and non-finite joint packets and clamps/rate-limits valid targets.
- Placeholder robots remain still without data and stale safety is displayed as UNKNOWN.
- Cold start, OMY workflow, Abort/Reset, focused tests, and visual idle checks pass output-disconnected.

Out of scope:
- Physical robot commissioning or command output.
- Unity/OpenXR package migration and existing imported-mesh warnings.
- Safety-rated stop behavior.

## Milestones
- [x] M1: Audit related runtime paths and logs.
- [x] M2: Harden receiver, visual fallbacks, and articulation target application.
- [x] M3: Gate READY on a valid idle lock.
- [x] M4: Build, deploy, and run output-disconnected regression checks.

## Findings
- Placeholder robots deliberately animated while disconnected.
- Safety visuals retained or started at SAFE when packets were absent or stale.
- UDP packets were not structurally validated before publication to consumers.
- Articulation targets accepted non-finite and out-of-limit values without rate limiting.
- Scenario readiness did not require successful OMY lock acquisition.
- The configured manual Real-to-Sim UDP port was not passed to the receiver.
- BasePlate attachment wrote velocity after entering kinematic mode, producing avoidable runtime warnings.

## Current state
Complete. The final Player build succeeded, remained output-disconnected with UDP 5055/5056/5057 closed, and completed the entire five-step Scenario 1 Run `sim-bd4a35105a684aafa719c55769f527dd`. OMY now locks all 10 measured completion targets, requires a continuous one-second settled dwell, and keeps that pose through WAAM. The final live log contains zero OMY idle, kinematic-velocity, orchestrator-failure, and NullReference entries. Abort/Reset regressions pass without a Worker restart; focused backend regressions passed 29/29 and the complete control-plane suite passed 278/278. The canonical Player is running at `output/unity-simulation-player` in `ReadyDelivery`.
