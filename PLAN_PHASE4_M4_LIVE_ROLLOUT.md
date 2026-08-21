# PLAN - Phase 4 M4 Live Read-Only Rollout

Goal: move the locally verified M3 runtime generator into a safe SO-101 live deployment and add
mapping schema v2 coverage for the remaining OMY and RB5 physical/simulation profiles.

Done when:

- the current Fleet implementation is preserved in Git without runtime PKI, databases, or captures;
- Central Admin and mTLS ingress are healthy and obsolete restart-loop containers are disabled;
- one generated SO-101 Agent/Gateway pair reports to Central and exposes read-only OPC UA;
- OMY, RB5, OMY-sim, and RB5-sim mappings cover every manifest feature with pinned units and bounds;
- focused regressions, a four-package ROS build, live API checks, and an independent review pass.

Out of scope:

- actuator commands, torque changes, writable OPC UA Variables or Methods;
- five-runtime simultaneous shadow validation, failure isolation, and the two-hour final gate;
- AAS content, camera byte relay, historian, or VLA training orchestration.

## Milestones

- [x] M1: audit source/runtime separation, pass the 259-test M3 regression, and create baseline `0ea2b85`.
- [x] M2: repair runtime dependency paths, Central routing, and the lab NTP service.
- [x] M3: generate and deploy the SO-101 live read-only runtime.
- [x] M4: add and validate OMY/RB5 physical and simulation mapping schema v2 contracts.
- [x] M5: run regression/build/live UI checks, document evidence, and complete independent review.

## Decisions log

- 2026-08-21: physical OMY cameras retain the attended USB 2.0 profile `848x480 @ 10 fps` with an
  `8 Hz` health floor; Isaac cameras retain their profile-defined 30 fps targets.
- 2026-08-21: OMY validity bounds are pinned to ROBOTIS `open_manipulator` commit
  `5adfd9cc91240c7cb6bedad6d5ebbd29188aa59d`; RB5 bounds use the manufacturer system
  specification rather than the bundled generic `+/-3.14` URDF values.
- 2026-08-21: physical and simulation mappings share feature names, order, units and joint bounds,
  while retaining runtime-specific camera contracts and distinct identities.

## Current state

M1-M5 are complete. The generated physical SO-101 Agent/Gateway is live and its 60-second secure
shadow passed. All four new mappings load with exact manifest coverage, the complete regression/build
checks pass, and two independent Opus reviews found no blockers. The explicitly excluded five-runtime
concurrent shadow, failure-isolation exercise and two-hour final gate remain deferred.
