# Galaxy XR ROS 2 input bridge

This package is the input-only boundary between a Galaxy XR application and the
PC ROS 2 graph. The headset opens TCP to `127.0.0.1:5556`; during wired
development, `adb reverse` carries that connection over USB to this bridge.

```text
Galaxy XR app -> TCP/NDJSON -> adb reverse -> xr_teleop_bridge -> ROS 2
```

The bridge does **not** publish joint trajectories, motor commands, IK output,
or any other topic that can move SO-ARM. Retargeting and safety gating belong to
a later node and must remain disconnected during input development.

## ROS outputs

| Topic | Type | Purpose |
| --- | --- | --- |
| `/xr/raw/frame` | `xr_teleop_interfaces/XRFrame` | Synchronized head, controllers, buttons, and device timestamp |
| `/xr/head/pose` | `geometry_msgs/PoseStamped` | Valid head pose |
| `/xr/left/grip_pose` | `geometry_msgs/PoseStamped` | Valid left grip pose |
| `/xr/right/grip_pose` | `geometry_msgs/PoseStamped` | Valid right grip pose |
| `/tf` | `tf2_msgs/TFMessage` | Tracked head and grip transforms |
| `/xr/diagnostics` | `diagnostic_msgs/DiagnosticArray` | TCP state, freshness, drops, and validation counters |

Raw and pose topics use best-effort depth-one QoS. The gateway keeps only the
freshest unpublished frame so latency cannot grow behind a stale queue.

## Wire contract v1

Each TCP record is one UTF-8 JSON object followed by `\n`. The root must contain
`schema: "xr_frame_v1"`, an increasing unsigned 32-bit `sequence`, monotonic
`device_time_ns`, `source`, `frame_id`, and synchronized `head`, `left`, and
`right` values.

```json
{
  "schema": "xr_frame_v1",
  "sequence": 1,
  "device_time_ns": 123456789,
  "source": "galaxy_xr",
  "frame_id": "xr_local",
  "head": {
    "position": [0.0, 1.65, 0.0],
    "orientation": [0.0, 0.0, 0.0, 1.0],
    "valid": true,
    "tracked": true
  },
  "left": {
    "connected": true,
    "grip": {"position": [-0.25, 1.25, -0.35], "orientation": [0.0, 0.0, 0.0, 1.0], "valid": true, "tracked": true},
    "aim": {"position": [-0.25, 1.25, -0.35], "orientation": [0.0, 0.0, 0.0, 1.0], "valid": true, "tracked": true},
    "linear_velocity": [0.0, 0.0, 0.0],
    "angular_velocity": [0.0, 0.0, 0.0],
    "trigger": 0.0,
    "squeeze": 0.0,
    "thumbstick": [0.0, 0.0],
    "buttons": 0
  },
  "right": {
    "connected": true,
    "grip": {"position": [0.25, 1.25, -0.35], "orientation": [0.0, 0.0, 0.0, 1.0], "valid": true, "tracked": true},
    "aim": {"position": [0.25, 1.25, -0.35], "orientation": [0.0, 0.0, 0.0, 1.0], "valid": true, "tracked": true},
    "linear_velocity": [0.0, 0.0, 0.0],
    "angular_velocity": [0.0, 0.0, 0.0],
    "trigger": 0.0,
    "squeeze": 0.0,
    "thumbstick": [0.0, 0.0],
    "buttons": 0
  }
}
```

Positions are metres and quaternions are `[x, y, z, w]`. Version 1 preserves
the producer's declared `frame_id`; it does not silently remap XR axes. The
Galaxy app's coordinate adapter and the later robot retargeter must establish
and test the explicit XR-to-ROS basis before any robot command node is enabled.

## Wired run

On the headset, enable developer mode and USB debugging, connect a data-capable
USB-C cable, and approve the RSA prompt. Then run on the PC:

```bash
scripts/connect_galaxy_xr_usb.sh
source /opt/ros/jazzy/setup.bash
colcon build --symlink-install --packages-select xr_teleop_interfaces xr_teleop_bridge
source install/setup.bash
ros2 run xr_teleop_bridge xr_teleop_bridge
```

The Galaxy XR sender APK is maintained in the sibling `oculus_reader/app`
project. With the headset connected, use two terminals from `v2_ws`:

```bash
# Terminal 1: build and run the input-only ROS bridge in Jazzy Docker.
scripts/run_xr_bridge_docker.sh

# Terminal 2: build, install, configure adb reverse, and launch the XR app.
scripts/deploy_galaxy_xr_app.sh
```

The bridge terminal reports `XR input fresh` when real frames arrive. To test
the PC path without hardware instead, run:

```bash
ros2 run xr_teleop_bridge xr_mock_sender --frames 300 --hz 60
```

Run the complete isolated integration smoke in the existing Jazzy container:

```bash
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD:/ws" -w /ws sang-fleet:jazzy bash -lc \
  'scripts/smoke_xr_teleop.sh'
```
