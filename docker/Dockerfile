FROM ros:jazzy-ros-base

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      python3-pip \
      python3-pytest \
      python3-zmq \
      python3-yaml \
      iproute2 \
      ros-jazzy-diagnostic-msgs \
      ros-jazzy-geometry-msgs \
      ros-jazzy-rosbag2 \
      ros-jazzy-rosbag2-py \
      ros-jazzy-rosbag2-storage-mcap \
      ros-jazzy-rosidl-default-generators \
      ros-jazzy-sensor-msgs \
      ros-jazzy-std-msgs \
      ros-jazzy-std-srvs \
      ros-jazzy-tf2-ros \
      ros-jazzy-trajectory-msgs \
      ros-jazzy-visualization-msgs \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir --break-system-packages --ignore-installed \
    "asyncua==2.0.1" \
    "cryptography==50.0.0" \
    "fastapi==0.140.0" \
    "feetech-servo-sdk==1.0.0" \
    httpx \
    "opencv-python-headless==4.10.0.84" \
    "pydantic==2.13.4" \
    "pyOpenSSL==26.4.0" \
    "rbpodo==0.16.14" \
    "uvicorn[standard]==0.51.0"

WORKDIR /ws
