FROM ros:jazzy-ros-base

# v0.6.0 릴리스 태그의 커밋 SHA (2026-07-13 검증, V2 §11: 태그가 아닌 SHA로 고정)
ARG LEROBOT_REF=30da8e687a6dfc617fcd94afc367ac7071c376ce
LABEL org.opencontainers.image.lerobot.revision="${LEROBOT_REF}"
ENV LEROBOT_SHA="${LEROBOT_REF}"

# ffmpeg: lerobot v0.6의 torchcodec 비디오 디코딩에 시스템 FFmpeg 공유 라이브러리 필수
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# --ignore-installed: apt가 설치한 packaging 등(RECORD 없음)을 pip가 제거하지 못해
# 실패하는 것을 회피 — 신선한 이미지이므로 기존 배포판 패키지를 덮어써도 안전.
# [training] extra 필수: dataset writer 의존성과 lerobot-train의 accelerate/wandb를 함께 설치한다.
RUN python3 -m pip install --no-cache-dir --break-system-packages --ignore-installed \
    "lerobot[training] @ https://github.com/huggingface/lerobot/archive/${LEROBOT_REF}.tar.gz"

# uvicorn[standard]: WebSocket 프로토콜 지원(websockets) 포함 — 순정 uvicorn은 WS 업그레이드를 404 처리
RUN python3 -m pip install --no-cache-dir --break-system-packages fastapi "uvicorn[standard]"

# 워크스페이스는 이미지를 만들 때 복사하지 않고 실행 시 /ws에 마운트한다.
WORKDIR /ws
