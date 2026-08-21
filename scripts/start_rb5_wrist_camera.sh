#!/usr/bin/env bash
set -euo pipefail

ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-30}"
RB5_CAMERA_SERIAL="${RB5_CAMERA_SERIAL:-}"

fail() {
    echo "ERROR: $1" >&2
    exit 1
}

command -v ros2 >/dev/null || fail "ROS 2 is not sourced"
ros2 pkg prefix realsense2_camera >/dev/null 2>&1 || \
    fail "realsense2_camera is not installed in this ROS 2 environment"

export ROS_DOMAIN_ID
export ROS_AUTOMATIC_DISCOVERY_RANGE="${ROS_AUTOMATIC_DISCOVERY_RANGE:-SUBNET}"

launch_args=(
    camera_namespace:=rbpodo/camera
    camera_name:=wrist
    depth_module.depth_profile:=848x480x10
    depth_module.color_profile:=848x480x10
    enable_depth:=false
    enable_color:=true
    enable_gyro:=false
    enable_accel:=false
    pointcloud.enable:=false
    align_depth.enable:=false
    publish_tf:=false
)

if [[ -n "${RB5_CAMERA_SERIAL}" ]]
then
    launch_args+=("serial_no:=_${RB5_CAMERA_SERIAL#_}")
fi

echo "Starting RB5 D405 wrist camera on ROS_DOMAIN_ID=${ROS_DOMAIN_ID}."
echo "Expected topic: /rbpodo/camera/wrist/color/image_rect_raw/compressed"
exec ros2 launch realsense2_camera rs_launch.py "${launch_args[@]}"
