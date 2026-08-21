# PLAN - Admin and Robot Agent rebase

Goal: Keep the new Fleet/Admin program and active XR ROS 2 work while removing the old
Recorder, training, inference, upload, and legacy UI implementation.

Done when:
- `fleet_control_plane`, `robot_agent`, and the three active XR packages are the only ROS 2 packages.
- `robot_agent` monitors manifest-declared ROS 2 state/action/camera topics and sends heartbeat v1 directly.
- No Admin code depends on `recorder_core` or its gateway.
- Old M4 scripts, Recorder UI, LeRobot-heavy Docker build, and historical Recorder plans are gone.
- Focused tests, `colcon build`, and the Admin browser flow pass.

Out of scope:
- Deleting or refactoring the active XR implementation.
- Reintroducing data recording, LeRobot training, policy execution, or remote robot control.
- Remote Agent transport before authentication and TLS.

## Milestones
- [x] M1: Audit dependencies and deletion boundary.
- [x] M2: Add minimal manifest-driven `robot_agent`.
- [x] M3: Remove legacy Recorder surface and update packaging/docs.
- [x] M4: Run full build, tests, and browser verification.

## Decisions log
- 2026-07-21: XR is custom work in progress, not part of LeRobot; preserve all XR packages and scripts.
- 2026-07-21: Replace the old HTTP gateway reporter with direct heartbeat push from `robot_agent`.
- 2026-07-21: Keep Agent and Control Plane loopback-only until authenticated TLS transport exists.
- 2026-07-21: Remove LeRobot from the base Docker image; data/VLA capabilities return later behind the new Agent contracts.

## Current state
Rebase complete. The workspace contains Fleet/Admin, read-only Robot Agent, and active XR
packages only. The new lightweight Docker image builds all five packages and all 49 tests pass.
