#!/usr/bin/env bash
set -e

source /opt/ros/humble/setup.bash
source /opt/rbpodo_ros2_ws/install/setup.bash

exec "$@"
