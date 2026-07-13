FROM ros:jazzy-ros-base

# v0.6.0 릴리스 태그의 커밋 SHA (2026-07-13 검증, V2 §11: 태그가 아닌 SHA로 고정)
ARG LEROBOT_REF=30da8e687a6dfc617fcd94afc367ac7071c376ce
LABEL org.opencontainers.image.lerobot.revision="${LEROBOT_REF}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip \
    && rm -rf /var/lib/apt/lists/*

# --ignore-installed: apt가 설치한 packaging 등(RECORD 없음)을 pip가 제거하지 못해
# 실패하는 것을 회피 — 신선한 이미지이므로 기존 배포판 패키지를 덮어써도 안전.
# [dataset] extra 필수: v0.6부터 기본 설치에 데이터셋 의존성(datasets 등)이 빠짐 —
# recorder core의 LeRobotDataset writer가 요구한다.
RUN python3 -m pip install --no-cache-dir --break-system-packages --ignore-installed \
    "lerobot[dataset] @ https://github.com/huggingface/lerobot/archive/${LEROBOT_REF}.tar.gz"

# 워크스페이스는 이미지를 만들 때 복사하지 않고 실행 시 /ws에 마운트한다.
WORKDIR /ws

