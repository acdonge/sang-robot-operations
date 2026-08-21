# PLAN - SO-101 read-only observability

Goal: Connect the four SO-101 buses and five cameras to the Fleet/Admin program without
issuing actuator commands, and expose source-by-source health in the Admin UI.

Done when:
- Robot Agent heartbeat v1 includes validated state, action, and camera source telemetry.
- Admin shows every source topic, freshness, measured rate, age, and sequence count.
- A standalone SO-101 adapter reads calibrated positions from follower/leader buses and
  publishes the manifest topics without any motor-register write path.
- A reusable OpenCV camera adapter publishes all five configured camera topics.
- Unit tests, ROS build/tests, fault/recovery integration, browser verification, and a
  real read-only hardware check pass.

Out of scope:
- Teleoperation, policy execution, torque changes, goal-position writes, or safety-mode changes.
- Recording, training, inference, or remote Agent transport.
- OMY and RB5 hardware adapters in this iteration.

## Milestones
- [x] M1: Add source telemetry to the manifest, heartbeat, and Control Plane contract.
- [x] M2: Render source-level state in the Admin inspector.
- [x] M3: Add write-incapable SO-101 serial and reusable OpenCV camera adapters.
- [x] M4: Verify synthetic stale/recovery behavior and the Admin browser flow.
- [x] M5: Verify the four real SO-101 buses and five cameras read-only, then stop all services.

## Decisions log
- 2026-07-21: Keep logical ROS interfaces in the robot manifest and physical device paths in
  an adapter config so the same manifest can be used by real, simulated, or remote adapters.
- 2026-07-21: Do not reuse the former LeRobot SO-ARM bridge because its connect/configure path
  writes configuration and torque registers even in its disabled control mode.
- 2026-07-21: The new serial transport exposes ping/read/close only; close must not disable
  torque or write any register.
- 2026-07-21: Follower state remains required; leader action observations remain optional.
- 2026-07-21: Require MJPG capture and publish the camera-provided JPEG bytes directly;
  OpenCV decode/re-encode is unnecessary for ROS `CompressedImage` observation topics.
- 2026-07-21: Do not set the V4L2 capture buffer to one. It reduced these cameras from
  about 29.5 Hz to about 15 Hz even though freshness was still maintained.

## Current state
M1-M2 complete: source telemetry round-trips through heartbeat v1 and the Admin inspector
renders all seven SO-101 sources, including optional/no-data and stale states. Playwright
confirmed the full topic list and a clean browser console. M3 added four-arm read-only
position publishing and one-process/five-thread camera publishing. The adapter core has no
motor write API; its fake SDK test fixes the operation sequence to handshake/read/close.
The headless image is 388 MB and 32 focused tests pass. Synthetic ROS integration verified
`READY -> DEGRADED -> READY` when only the required
`right_wrist` stream was paused, while Agent connectivity remained online. Stopping the
Agent transitioned the central snapshot to offline. M5 then verified all four real buses
read-only at about 20 Hz and all five Innomaker cameras at about 29.5 Hz using stable USB
`by-path` bindings and raw MJPG passthrough. The live heartbeat stayed `READY` with all
seven sources fresh. Playwright displayed the actual rates and topic ages with zero console
errors or warnings. The next hardware step is a longer soak/reconnect test before OMY and
RB5 adapter conformance work.

Follow-up implementation added transition-only SQLite health history, explicit 18 Hz arm and
25 Hz camera minimums, Admin incident/recovery rendering, and the read-only `fleet_soak` CLI.
See `PLAN_HEALTH_EVENTS.md` for the completed automated and short real-hardware checks; the
operator-run two-hour soak and attended physical reconnect remain.
