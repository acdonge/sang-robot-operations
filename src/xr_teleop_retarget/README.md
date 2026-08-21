# Galaxy XR bimanual SO-ARM retarget preview

This package converts both Galaxy XR controller grip poses into synchronized,
calibrated SO-ARM targets. The ROS side intentionally has no IK, joint
trajectory, LeRobot, motor, or servo publisher. Isaac Sim consumes the targets
through the separate latest-frame gateway.

## Mapping

OpenXR uses `+X` right, `+Y` up, and `-Z` forward. The preview follows ROS
REP-103 in `soarm_base`:

```text
robot_x = -xr_z   # forward
robot_y = -xr_x   # left
robot_z =  xr_y   # up
```

Calibration stores both current controller poses as independent origins. Each
origin maps to the SO-ARM gripper-link neutral pose
`(0.29323402, -0.00017677, 0.23437033)` metres in that arm's local base frame.
Each controller's relative translation and rotation are then applied to its
matching arm target.

Translation defaults to a conservative `0.5` scale, so 10 cm of controller
travel requests 5 cm of simulated end-effector travel. The workspace bounds
remain unchanged.

## Safety gates

All gates must pass for both controllers before the bimanual target is enabled:

- a successful two-controller calibration exists;
- the newest XR frame is no more than 250 ms old;
- both grip poses are connected, valid, and tracked;
- both squeeze controls are held at or above 0.5;
- both candidates are inside the configured workspace box;
- each controller's rotation from calibration is no more than 90 degrees.

Each trigger controls its matching simulated gripper (`0` open, `1` closed).
Releasing either squeeze, setting down either controller, or losing input stops
new targets. Isaac Sim then holds the last accepted target.

## ROS API

| Name | Type | Purpose |
| --- | --- | --- |
| `/xr/retarget/calibrate` | `std_srvs/srv/Trigger` | Capture both currently tracked controller poses |
| `/xr/retarget/reset_calibration` | `std_srvs/srv/Trigger` | Clear both captured anchors |
| `/xr/retarget/bimanual_target` | `xr_teleop_interfaces/msg/XRBimanualTarget` | Synchronized left/right targets, grippers, gates, and inhibit reasons |
| `/xr/retarget/preview_pose` | `geometry_msgs/PoseStamped` | Legacy enabled right-arm preview in `soarm_base` |
| `/xr/retarget/preview_gripper` | `std_msgs/Float32` | Legacy enabled right-trigger value |
| `/xr/retarget/enabled` | `std_msgs/Bool` | Current aggregate safety-gate state |
| `/xr/retarget/markers` | `visualization_msgs/MarkerArray` | Legacy right-target visualization |
| `/xr/retarget/diagnostics` | `diagnostic_msgs/DiagnosticArray` | Individual gates and inhibit reason |
| `/tf` | `tf2_msgs/TFMessage` | Enabled left/right target transforms |

The gateway publishes protocol-v1 JSON from
`/xr/retarget/bimanual_target` on `tcp://0.0.0.0:5558`. Isaac Sim subscribes in
latest-frame mode so delayed motion commands do not queue up.

## Wired preview

Stop any older bridge already using TCP 5556, then start the ROS input,
retargeter, and Isaac gateway:

```bash
/home/sa08050/app\ developer/v2_ws/scripts/run_xr_retarget_preview_docker.sh
```

Override the scale only when intentionally tuning simulator sensitivity:

```bash
XR_TRANSLATION_SCALE=0.4 \
  /home/sa08050/app\ developer/v2_ws/scripts/run_xr_retarget_preview_docker.sh
```

Hold both controllers in a comfortable neutral pose with both grip poses
tracked, then calibrate from another terminal:

```bash
docker exec galaxy-xr-ros bash -lc \
  'source /opt/ros/jazzy/setup.bash && \
   source /tmp/xr-retarget-install/setup.bash && \
   ROS_DOMAIN_ID=42 ros2 service call \
   /xr/retarget/calibrate std_srvs/srv/Trigger {}'
```

Start the bimanual Isaac Sim task:

```bash
/home/sa08050/app\ developer/sh_list_issacsim/galaxy_xr_bimanual.sh
```

Hold both squeeze controls and initially move both controllers only a few
centimetres. Releasing either squeeze freezes both simulated targets. This
launcher refuses Galaxy XR recording and real-follower mirroring, so this phase
cannot command physical SO-ARM motors.

Run the isolated automated check inside the Jazzy image:

```bash
scripts/smoke_xr_retarget.sh
```

## Live axis check

After calibration, run the live probe and hold both squeeze controls while
moving the right controller about 5 cm forward, back to center, 5 cm left, back
to center, and 5 cm up. Release squeeze after returning to center:

```bash
python3 /ws/src/xr_teleop_retarget/test/live_axis_probe.py
```

The probe requires at least 1.5 cm of positive `x/forward`, `y/left`, and
`z/up` preview travel and verifies that the node exposes no robot command
publisher.
