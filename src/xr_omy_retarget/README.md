# Galaxy XR to ROBOTIS OMY target controller

This package converts a tracked Galaxy XR controller into a safe, single-arm
Cartesian target for the ROBOTIS OMY F3M. The included Isaac Sim adapter drives
the supplied OMY USD, but no node publishes real joint trajectories, motor
commands, or hardware controller actions.

## Control mapping

- Right controller by default (`controller_hand:=left` is also supported).
- Controller translation uses fixed OpenXR local axes; head pose and gaze never
  affect the OMY target.
- Each squeeze fixes the current controller position as the source origin.
- Controller displacement maps 1:1 onto OMY TCP displacement in `link0`.
- Translation-only control is the safe default while axis mapping is tuned.
- Set `XR_OMY_TRACK_ORIENTATION=true` to add the controller's full 6D orientation.
- Squeeze is the deadman and clutch. Release, reposition, and squeeze again to
  continue from the last accepted target without a jump.
- Trigger is normalized gripper demand: `0.0` open and `1.0` closed.

The default neutral target is FK of the official OMY ready pose
`[0, -1.5708, 2.6529, -1.0821, 1.5708, 0]` at `end_effector_link`:

```text
position:         [0.19080657, -0.11299910, 0.39004890] m
orientation_xyzw: [0, 0, 0.70710678, 0.70710678]
```

## Run

From the workspace root:

```bash
./scripts/run_xr_omy_preview_docker.sh
```

Only one process can listen for the XR app on TCP 5556. Stop the older SO-ARM
preview container before starting this script. Defaults can be changed with:

```bash
XR_PORT=5556 \
OMY_ISAAC_PORT=5559 \
XR_CONTROLLER_HAND=right \
XR_OMY_TRANSLATION_SCALE=1.0 \
./scripts/run_xr_omy_preview_docker.sh
```

With the Galaxy XR app streaming, call:

```bash
docker exec galaxy-xr-omy-ros bash -lc \
  'source /tmp/xr-omy-install/setup.bash && ros2 service call /xr/omy/calibrate std_srvs/srv/Trigger {}'
```

Keep squeeze released while calibrating. Each squeeze anchors that exact hand
position to the current OMY TCP, and only later controller displacement is
applied. Release squeeze at any time to inhibit output.

Start the supplied OMY Isaac stage in another terminal:

```bash
./scripts/run_xr_omy_isaac.sh
```

The default opens Isaac Sim 5.1 with
`../issacsim_setting_usd/OMY_ai3m/usd/omy_f3m.usd` and connects to ZMQ TCP
5559. It initializes the arm at the official ready pose. Squeeze enables pose
tracking and trigger closes the simulated RH-P12-RN hand. To verify the USD,
articulation drives, 6D IK, gripper, and stale-input hold without the XR app:

```bash
OMY_ISAAC_HEADLESS=1 OMY_ISAAC_SELF_TEST=1 \
  ./scripts/run_xr_omy_isaac.sh
```

Useful overrides are `ISAAC_SIM_ROOT`, `OMY_USD`, `OMY_ISAAC_ENDPOINT`,
`OMY_ARTICULATION_PRIM`, `OMY_TCP_PRIM`, `OMY_ARM_STIFFNESS`, and
`OMY_ARM_DAMPING`. The verified `5000/100` default arm gains are applied only
in memory; they remove the large gravity sag observed with the USD's original
`120/4` gains without modifying the stage file.

The stage's `/World/ActionGraph` contains a ROS2 articulation controller that
would compete with this adapter for the same joints. The launcher deactivates
that graph only in memory; override its path with `OMY_ACTION_GRAPH_PRIM` if the
stage is reorganized.

The Isaac Python runtime also needs `pyzmq`. The launcher checks this before
opening Isaac; install it once if requested:

```bash
~/isaacsim/python.sh -m pip install --user 'pyzmq>=25,<28'
```

## ROS and Isaac contract

The canonical ROS output is `/xr/omy/target` (`XROmyTarget`). Status is also
available on `/xr/omy/enabled`, `/xr/omy/diagnostics`, and TF child
`omy_xr_target`. Calibration services are `/xr/omy/calibrate` and
`/xr/omy/reset_calibration`.

The ZMQ PUB gateway binds to TCP 5559 and emits `xr_omy_target_v1` JSON:

```json
{
  "protocol": "xr_omy_target_v1",
  "sequence": 42,
  "ros_stamp_ns": 123456789,
  "target_link": "end_effector_link",
  "target": {
    "frame_id": "link0",
    "position": [0.2, -0.1, 0.4],
    "orientation_xyzw": [0.0, 0.0, 0.7071, 0.7071]
  },
  "gripper": 0.5,
  "enabled": true,
  "inhibit_reason": "enabled",
  "clutch_epoch": 2
}
```

`clutch_epoch` increases on every squeeze re-anchor and remains in later
frames, so a receiver can detect clutch changes without relying on a one-frame
event. The gateway conflates its queue so a slow receiver gets current state
instead of replaying obsolete targets.

The Isaac adapter applies targets only when `enabled` is true and the latest
validated packet is at most 250 ms old. A false frame, invalid IK, or receive
timeout captures and holds measured joint position. It commands PhysX indices
`0..5` (`joint1..joint6`) and `7` (`rh_r1_joint`); the other three hand DOFs are
USD mimic joints. Gripper demand is mapped to the smaller of the ROBOTIS closed
position and the actual USD upper limit.

The local DLS IK is for this simulation only and is not collision-aware. Real
hardware will use the official ROBOTIS `open_manipulator` bringup and Cyclo
MoveL QP controller described in
[`docs/robotis_hardware_handoff.md`](docs/robotis_hardware_handoff.md).

## Verify

Run the isolated Jazzy test from the ROS image:

```bash
docker run --rm --network host \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD:/ws" -w /ws sang-fleet:jazzy \
  bash scripts/smoke_xr_omy.sh
```
