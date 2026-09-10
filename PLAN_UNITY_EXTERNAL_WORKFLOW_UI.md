# PLAN - Unity External Workflow UI Only

Goal: Run Unity Scenario 1 as a visual simulation asset executor whose only control surface is the SANG web Workflow UI.

Done when:
- The `SANG_SIMULATION_PLAYER` build renders the scene assets without any Unity/XR Canvas, button, overlay, or direct pointer input.
- Hidden state components required by the existing Scenario 1 implementation remain available so the orchestrator can execute all five steps.
- The build manifest declares the external Workflow UI as the sole control surface.
- Physical output remains disconnected and the artifact scan passes.
- A browser-queued run completes all five steps and Reset returns Unity to ReadyDelivery.

Out of scope:
- Removing UI from the Unity Editor, Galaxy XR, or Meta Quest builds.
- Physical robot, welder, feeder, ROS command, or vendor SDK control.
- Replacing the fixed Scenario 1 Skill implementations.

## Milestones
- [x] M1: Disable all in-Player visual and direct-input control surfaces.
- [x] M2: Rebuild with Unity 6000.5.6f1 and verify output isolation.
- [x] M3: Complete and reset Scenario 1 from Workflow Studio only.
- [x] M4: Record runtime and browser evidence.

## Decisions log
- 2026-08-31: Limited the change to `SANG_SIMULATION_PLAYER`; Editor, Galaxy XR, and Meta Quest keep their existing UI.
- 2026-08-31: Retained a renderless compatibility state object because the legacy Scenario 1 scripts still exchange state through `Text` and `Button` references. It has no Canvas, EventSystem, raycaster, visible geometry, or direct input path.
- 2026-08-31: Declared the web Workflow UI as the sole control surface in the build manifest while keeping every physical output disconnected.

## Current state
Complete. Unity 6000.5.6f1 build `output/unity-simulation-player-web-only-v2` is running. Browser-queued Run `sim-6b3d9ced93e14ceea5945deb75fb258e` completed all five Scenario 1 steps, acknowledged Hold/Resume/Reset, and returned to `ReadyDelivery`. The compiled artifact scan passed with SHA-256 `0d988cd16c0cbf05b4796c3b014bb49179546dff1b4e587248e291fd0b698bb8`; the Control Plane suite passed 280 tests and the Admin UI production build succeeded.
