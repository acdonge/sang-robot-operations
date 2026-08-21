FROM osrf/ros:humble-desktop

ARG RBPODO_COMMIT=f6ef41adf629dd27c96b0d3e7ebf3d3fc0cc53c4
ARG RBPODO_ROS2_COMMIT=5e8294a985e7ce5e20e70564c2681130afc5f502

SHELL ["/bin/bash", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      cmake \
      git \
      python3-colcon-common-extensions \
      python3-rosdep \
      ros-humble-ament-cmake \
      ros-humble-joint-state-publisher \
      ros-humble-moveit \
      ros-humble-pluginlib \
      ros-humble-robot-state-publisher \
      ros-humble-ros2-control \
      ros-humble-ros2-controllers \
      ros-humble-rviz2 \
      ros-humble-urdf-launch \
      ros-humble-xacro \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/RainbowRobotics/rbpodo.git /opt/src/rbpodo \
    && git -C /opt/src/rbpodo checkout "${RBPODO_COMMIT}" \
    && cmake -S /opt/src/rbpodo -B /opt/src/rbpodo/build \
      -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /opt/src/rbpodo/build --parallel \
    && cmake --install /opt/src/rbpodo/build \
    && ldconfig

RUN mkdir -p /opt/rbpodo_ros2_ws/src \
    && git clone https://github.com/RainbowRobotics/rbpodo_ros2.git \
      /opt/rbpodo_ros2_ws/src/rbpodo_ros2 \
    && git -C /opt/rbpodo_ros2_ws/src/rbpodo_ros2 checkout "${RBPODO_ROS2_COMMIT}"

COPY rbpodo_ros2-startup-speed.patch /tmp/rbpodo_ros2-startup-speed.patch
RUN git -C /opt/rbpodo_ros2_ws/src/rbpodo_ros2 apply \
      /tmp/rbpodo_ros2-startup-speed.patch

RUN source /opt/ros/humble/setup.bash \
    && cd /opt/rbpodo_ros2_ws \
    && colcon build --cmake-args -DCMAKE_BUILD_TYPE=Release

COPY entrypoint.sh /usr/local/bin/rb5-control-entrypoint
RUN chmod 0755 /usr/local/bin/rb5-control-entrypoint

LABEL org.opencontainers.image.source="https://github.com/RainbowRobotics/rbpodo_ros2" \
      org.opencontainers.image.revision="${RBPODO_ROS2_COMMIT}" \
      io.sang.safety-patch="startup-speed-bar-0.01" \
      io.sang.control-scope="attended-lab-only"

ENTRYPOINT ["/usr/local/bin/rb5-control-entrypoint"]
CMD ["bash"]
