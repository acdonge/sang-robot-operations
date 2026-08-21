# ROBOTIS OMY hardware handoff

The Isaac adapter is intentionally not a hardware driver. Real OMY operation
will reuse ROBOTIS' supported ROS 2 Jazzy stack and safety-aware Cartesian
controller.

## Official components

- [`ROBOTIS-GIT/open_manipulator`](https://github.com/ROBOTIS-GIT/open_manipulator)
  supplies the OMY-F3M URDF, `ros2_control` hardware interface, controller
  configuration, and bringup launch files.
- [`ROBOTIS-GIT/cyclo_control`](https://github.com/ROBOTIS-GIT/cyclo_control)
  supplies the OMY MoveL QP controller, model limits, and self-collision model.
- The [ROBOTIS OMY Cyclo guide](https://ai.robotis.com/omy/advanced_motion_controller_omy.html)
  is the operational source of truth.

The contracts inspected for this adapter were `open_manipulator` commit
`26807fca45c472c315ad163b732aa6cc391ec999` and Cyclo Control release `0.3.0`
(`ce5a4100900bbc7c3af90c5c9c34cb9c845b20e4`). Revalidate topic and parameter
names before connecting a different release.

## Intended data path

```text
/xr/omy/target
  -> hardware safety gate
  -> robotis_interfaces/msg/MoveL
  -> /omy_movel_controller/movel
  -> official Cyclo OMY QP controller
  -> private six-joint trajectory topic
  -> XR hardware safety mux + rh_r1_joint
  -> /leader/joint_trajectory
  -> official OMY-F3M ros2_control
```

The safety mux must be the only publisher to `/leader/joint_trajectory`. It
must reject stale, disabled, invalid-frame, discontinuous, or out-of-workspace
XR targets and publish a measured-position hold instead. This avoids racing a
separate gripper publisher against the 100 Hz Cyclo arm output.

## ROBOTIS launch boundary

On the Robot PC, the documented bringup is:

```bash
ros2 launch open_manipulator_bringup omy_f3m_follower_ai.launch.py
ros2 launch cyclo_motion_controller_ros omy_controller.launch.py controller_type:=movel
```

The follower AI controller runs at 400 Hz and accepts the official joints
`joint1` through `joint6` plus `rh_r1_joint`. Cyclo consumes
`robotis_interfaces/msg/MoveL` in `link0` for `end_effector_link` and emits arm
joint trajectories. For continuous XR servo goals, `time_from_start` is zero.

Do not enable this path until simulation calibration, frame direction, joint
limit, stale-input hold, E-stop access, and low-speed workspace tests all pass.
