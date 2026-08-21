# XR to OMY development plan

## Current iteration

The current boundary is Galaxy XR through a safe Isaac Sim OMY articulation
adapter. It uses the official F3M frames (`link0`, `end_effector_link`) and
normalized RH-P12-RN gripper demand, but intentionally has no real-robot
command publisher.

Implemented data path:

```text
Galaxy XR app -> TCP 5556 -> XRFrame -> OMY retarget/clutch
              -> /xr/omy/target -> ZMQ TCP 5559 -> Isaac OMY adapter
              -> 6D IK -> joint1..joint6 + rh_r1_joint
```

Safety gates are calibration, input age, controller tracking, squeeze deadman,
Cartesian workspace, and per-clutch rotation. Translation uses fixed OpenXR
local axes and never depends on head pose or gaze. Any failed gate sends
`enabled=false`; the gateway still forwards that inhibited frame.

## Isaac environment

The supplied stage is:

```text
../issacsim_setting_usd/OMY_ai3m/usd/omy_f3m.usd
```

It was verified with Isaac Sim 5.1.0-rc.19. The articulation root is
`/World/OMY/OMY/world`; this is the USD equivalent of the official `link0`
frame. PhysX exposes ten DOFs in this order:

```text
joint1 joint2 joint3 joint4 joint5 joint6 rh_l1 rh_r1_joint rh_l2 rh_r2
```

Only indices `0..5` and `7` are commanded. The other gripper joints use the
USD mimic constraints. The TCP prim is
`/World/OMY/OMY/link6/end_effector_flange_link/end_effector_link`.

The adapter verifies protocol, frame/link names, sequence, finite pose values,
and receive age before solving 6D IK. It rate-limits articulation targets and
holds the measured pose on inhibit, invalid IK, or a 250 ms receive timeout.
The local IK does not perform collision avoidance.

## Real OMY handoff

Real hardware will use the official ROBOTIS packages rather than sending the
Isaac IK result to motors:

1. `ROBOTIS-GIT/open_manipulator` provides
   `omy_f3m_follower_ai.launch.py`, the 400 Hz `ros2_control` setup, official
   URDF, joint names, and the `/leader/joint_trajectory` remap.
2. `ROBOTIS-GIT/cyclo_control` provides the OMY MoveL QP controller. XR poses
   map to `robotis_interfaces/msg/MoveL` on `/omy_movel_controller/movel` with
   `link0` and `end_effector_link`.
3. A hardware-only safety mux will be the sole publisher to
   `/leader/joint_trajectory`. It will combine Cyclo's six arm joints with
   `rh_r1_joint`, and hold measured feedback whenever XR is inhibited or stale.

This separation preserves ROBOTIS joint limits and self-collision filtering on
the physical robot. See `src/xr_omy_retarget/docs/robotis_hardware_handoff.md`.

## Acceptance checks

- Official ready TCP is the zero-motion target.
- A squeeze edge fixes the controller origin; later controller displacement is
  mapped 1:1 onto the OMY TCP using fixed XR local axes.
- Head pose and gaze changes do not rotate or translate the OMY target.
- Release/reposition/regrasp has no Cartesian target jump.
- Stale or invalid tracking inhibits ROS and ZMQ outputs within 250 ms.
- ZMQ retains frame, TCP link, pose, gripper, and inhibit state.
- Isaac commands only the verified arm and driven gripper DOF indices.
- Disabled, stale, malformed, or unsolved targets hold the current pose.
- The OMY XR ROS node exposes no real joint, motor, or servo command publisher.
