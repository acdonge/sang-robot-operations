FROM ros:jazzy-ros-base

ARG LEROBOT_REF=v0.6.0
# v0.6.0은 개발 편의 기본값이다. 재현 가능한 빌드에서는 이 인자를 정확한
# 검증 commit SHA로 해석·지정하고 아래 이미지 라벨에 그 SHA를 기록한다(V2 §11).
LABEL org.opencontainers.image.lerobot.revision="${LEROBOT_REF}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir --break-system-packages \
    "https://github.com/huggingface/lerobot/archive/${LEROBOT_REF}.tar.gz"

# 워크스페이스는 이미지를 만들 때 복사하지 않고 실행 시 /ws에 마운트한다.
WORKDIR /ws

