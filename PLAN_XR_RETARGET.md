# PLAN - Galaxy XR to SO-ARM retarget preview

Goal: Convert both Galaxy XR controller poses into calibrated, synchronized
SO-ARM base targets with explicit safety gates and visualization, without
publishing any robot command.

Done when: The XR-to-ROS basis and relative-pose math pass unit tests; a ROS 2
node calibrates from a live bimanual frame; tracking, freshness, deadman, and
workspace gates are observable for both controllers; target/TF outputs follow
small real controller motions; and the ROS graph contains no motor or joint
command topic.

Out of scope: IK, joint trajectories, ros2_control, LeRobot motor writes, real
arm motion, collision checking, force feedback, and dataset recording.

## Milestones

- [x] M1: Retarget contract and transform math - verify by: pure unit tests for
  axis mapping, calibration identity, translation, rotation, and limits.
- [x] M2: Input-only ROS preview node - verify by: node tests for calibration,
  freshness, tracking, deadman, workspace, preview topics, TF, and diagnostics.
- [x] M3: Visualization and reproducible launch path - verify by: package build,
  RViz configuration audit, and isolated ROS smoke test.
- [x] M4: Galaxy XR hardware preview - verify by: live calibration and observed
  forward/left/up motion from controlled approximately 5 cm movements.
- [x] M5: Calibration passthrough view - verify by: Android XR runtime selects
  `XR_ENVIRONMENT_BLEND_MODE_ALPHA_BLEND`, clears the projection background with
  zero alpha, and keeps an opaque fallback for unsupported runtimes.
- [x] M6: Passthrough device regression - verify by: APK build/install, live
  headset passthrough, active controller frames in ROS 2, and mirror operation.
- [x] M7: Read-only desktop stream host - verify by: X11 capture produces a
  1280x720 JPEG stream at 10 Hz over loopback TCP with bounded frame sizes.
- [x] M8: In-app desktop quad - verify by: the Android receiver decodes off the
  render thread and submits a head-locked OpenXR quad without frame errors.
- [x] M9: Wired desktop launch path - verify by: ADB reverse configures desktop
  TCP 5557 independently from telemetry TCP 5556 and scripts pass syntax checks.
- [x] M10: Live XR desktop panel - verify by: the PC desktop is readable over
  passthrough while XR-to-ROS input remains fresh and error-free.
- [x] M11: Remove desktop display - verify by: the APK has no desktop receiver,
  quad layer, or TCP 5557 connection and the wired scripts configure only
  telemetry TCP 5556.
- [x] M12: Controller interaction visualization - verify by: tracked controller
  models, aim rays, and fixed-distance reticles render over passthrough.
- [x] M13: Optional hand visualization - verify by: `XR_EXT_hand_tracking`
  renders a joint-and-bone outline when permission and hand tracking are active,
  with controller-only fallback when unavailable.
- [x] M14: Input visualization device regression - verify by: APK install,
  visual device capture, fresh ROS input, and zero compositor or xrEndFrame
  errors.
- [x] M15: Bimanual Isaac handoff - verify by: synchronized left/right ROS
  targets, both-squeeze aggregate gating, latest-frame gateway payload, and a
  headless Isaac Sim run that moves both simulated arms.

## Decisions log

- 2026-07-21: Map one right-hand controller to one SO-ARM preview for the first
  iteration; M15 later superseded this with independent bimanual mapping.
- 2026-07-21: Map OpenXR axes to ROS REP-103 as `robot_x=-xr_z`,
  `robot_y=-xr_x`, and `robot_z=xr_y`.
- 2026-07-21: Use right squeeze as a hold-to-enable deadman and right trigger as
  a normalized gripper preview. Calibration is an explicit ROS service.
- 2026-07-21: Publish only namespaced preview data, TF, markers, and diagnostics.
  Do not create a robot command adapter in this package.
- 2026-07-21: Start at a configurable neutral preview pose of
  `(0.25, 0.0, 0.20)` metres in `soarm_base`; limit preview positions to a
  conservative box before any later IK or hardware work. M15 replaced this
  initial preview value with the SO-ARM gripper-link FK neutral pose.
- 2026-07-21: Live input produced 532 enabled preview samples with positive
  forward/left/up travel of 0.115/0.178/0.048 m and no robot-command publisher.
  Axis signs are validated. Hand-estimated forward/left travel was larger than
  intended, so metric scale tuning remains required before commanding hardware.
- 2026-07-21: Use core OpenXR alpha-blend passthrough on Android XR instead of
  the Meta-only `XR_FB_passthrough` extension. Select it only when enumerated by
  the runtime and retain the existing opaque background as a fallback.
- 2026-07-21: Galaxy XR selected alpha blend at runtime; a device screenshot
  showed the physical lab through the full projection view. ROS input remained
  fresh at about 72 Hz with zero malformed, out-of-order, or missing frames.
- 2026-07-21: For the first desktop-panel iteration, use a read-only 1280x720
  JPEG stream over USB loopback TCP 5557 and a head-locked OpenXR quad. Keep it
  independent from telemetry TCP 5556; defer remote keyboard and mouse input.
- 2026-07-21: Live Galaxy XR capture showed the primary PC monitor correctly
  oriented on the 16:9 quad over room passthrough. The host held 10.0 fps at
  about 85-90 KiB per frame, while ROS diagnostics stayed fresh with zero
  malformed, out-of-order, or missing telemetry frames.
- 2026-07-21: Android XR requires a newly released quad swapchain image on each
  XR frame. Cache the latest decoded desktop frame and refresh only stale
  swapchain images while still acquiring and releasing the quad every frame;
  this removed all compositor latch and xrEndFrame errors.
- 2026-07-21: Replace the desktop panel with adaptive input visualization.
  Render controllers when controller grip tracking is active; otherwise render
  optional OpenXR hand joints. Use a 2 m aim ray and reticle without requesting
  scene-understanding permission for physical-surface raycasts.
- 2026-07-21: Galaxy XR device captures verified cyan/amber controller outlines
  and aim rays aligned with both tracked controllers, then automatic switching
  to 26-joint hand outlines with both hand aim rays and endpoint reticles. ROS
  diagnostics remained fresh with zero malformed, out-of-order, or missing
  frames, and the app log had no compositor, swapchain, or xrEndFrame failure.
- 2026-07-21: Extend the validated right-arm preview into independent left/right
  retargeters. Both squeeze gates must pass before the synchronized target is
  accepted, and each arm uses the SO-ARM gripper-link neutral pose
  `(0.29323402, -0.00017677, 0.23437033)` in its local base frame.
- 2026-07-21: Keep ROS 2 canonical and bridge only the synchronized target to
  Isaac Sim over latest-frame ZMQ. The LeIsaac Galaxy XR path rejects recording
  and real-follower mirroring during simulator validation.
- 2026-07-22: Reduce the default controller translation scale from 1.0 to 0.5
  after live movement reached the workspace boundary. Keep the conservative
  workspace unchanged and expose `XR_TRANSLATION_SCALE` for simulator-only
  tuning before considering any wider limits.

## Current state

M15 of the wired Galaxy XR input plan is complete. Real head and both controller
frames reach ROS 2 near 72 Hz. Retarget math has 15 passing unit tests, the ROS
packages build, and both isolated and live Galaxy XR checks pass with no robot
command publishers. Forward/left/up axis signs and deadman release are verified;
the physical robot remains intentionally disconnected from the ROS graph.
Android XR alpha-blend passthrough and the wired ROS input path are installed
and visually verified. The desktop-panel experiment passed M7-M10 and its APK,
TCP 5557, and host-script paths have now been removed. Adaptive controller and
hand outlines plus 2 m aim rays are built, installed, and visually verified on
Galaxy XR. Extension discovery, permission grant, both hand trackers,
controller/hand profile switching, ROS telemetry, and compositor regression
checks pass. The app remains input-only and the physical robot stays
disconnected from this ROS graph. The bimanual ROS target and Isaac gateway now
feed LeIsaac's 16-dimensional dual-arm differential-IK action; an automated
180-step Isaac run moved both simulated arms while hardware output remained
unavailable. Live scale tuning now defaults to 0.5 while preserving the
existing workspace and all deadman, tracking, freshness, and rotation gates.
