# PLAN - Galaxy XR wired ROS 2 teleoperation input

Goal: Receive Galaxy XR head/controller data over USB-backed TCP and expose one
validated, observable ROS 2 input boundary without publishing robot commands.

Done when: A versioned XR frame contract builds in ROS 2 Jazzy; the TCP gateway
accepts newline-delimited frames, rejects invalid/stale-order input, publishes
the raw frame plus pose/TF/diagnostics topics, and a mock sender passes unit and
ROS integration smoke checks.

Out of scope: UDP/Wi-Fi transport, retargeting, IK, motor commands, real-arm
motion, video streaming, and haptics.

## Milestones

- [x] M1: Wire and ROS message contracts - verify by: parser tests and Jazzy interface build.
- [x] M2: TCP gateway and ROS publishers - verify by: server/node tests and Python compilation.
- [x] M3: Mock sender and end-to-end ROS smoke - verify by: live TCP to ROS topic/TF/diagnostic assertions.
- [x] M4: Documentation and handoff audit - verify by: full tests, shell syntax, diff check, and ADB status.
- [x] M5: Galaxy XR sender APK - verify by: arm64 Gradle build, packaged Android XR manifest, and native library audit.
- [x] M6: Galaxy XR hardware bring-up - verify by: APK launch, live OpenXR
  logcat, fresh ROS diagnostics, and real head/controller frames over ADB reverse.

## Decisions log

- 2026-07-21: Keep ROS 2 off the headset. USB uses TCP through `adb reverse`; Wi-Fi later changes only the gateway transport.
- 2026-07-21: Use a single `XRFrame` for synchronized head/left/right/button data and derive convenience pose/TF topics.
- 2026-07-21: Use a new `xr_frame_v1` NDJSON wire contract instead of coupling the ROS package to the Meta-specific legacy matrix string.
- 2026-07-21: Place the packages in `v2_ws`, whose container pins official LeRobot 0.6.0; do not use the custom LeRobot 0.4.3 source tree.
- 2026-07-21: This iteration is input-only. No topic capable of moving a robot is created.
- 2026-07-21: Reuse the existing Quest OpenXR frame framework, but replace its Meta-only demo and legacy matrix stream with a minimal Android XR/OpenXR 1.1 `xr_frame_v1` TCP sender.
- 2026-07-21: Keep M6 input-only on real hardware; do not connect retargeting,
  IK, joint trajectories, or SO-ARM motor output during device bring-up.
- 2026-07-21: Galaxy XR firmware based on Android 14 requires the legacy
  `org.khronos.openxr.intent.category.IMMERSIVE_HMD` activity category in
  addition to the Android XR full-space property before it creates an immersive
  OpenXR session.
- 2026-07-21: Keep the standard Oculus Touch interaction profile for the 6DoF
  controllers. Android XR documents that profile and the device runtime exposes
  it; do not add guessed vendor bindings while the system reports both physical
  controllers as disconnected.
- 2026-07-21: The Galaxy XR runtime initially exposes idle controllers through
  `khr/simple_controller`, then selects `oculus/touch_controller` when they are
  held in tracking view. Treat loss of grip pose after setting a controller down
  as expected tracking loss, not a transport disconnect.

## Current state

PC and Android implementation is complete for the wired input milestone. The
Jazzy integration smoke delivered 120/120 synthetic frames, all three TF child
frames, fresh/stale diagnostics, and zero robot command publishers. The arm64
Galaxy XR APK builds and contains the OpenXR 1.1/controller manifest contract.

Galaxy XR `SM-I610` is authorized over ADB, the desktop screen-mirror launcher
works, and the installed APK has an active immersive OpenXR session. Real head
poses reach ROS 2 at approximately 72 Hz; diagnostics report a fresh connection,
zero malformed frames, zero out-of-order frames, and zero sequence gaps.

M6 hardware bring-up is complete. Both `Kinetics6DOF` controllers connected at
100% battery, transitioned to valid left/right tracking, and produced real ROS 2
trigger values across 0.0-1.0 plus grip and touch changes. The app stayed near
72 Hz while diagnostics reported zero malformed, out-of-order, or missing
frames. Robot retargeting stays intentionally disconnected until an explicit
XR-to-ROS coordinate-basis test and motion safety gate are implemented.
