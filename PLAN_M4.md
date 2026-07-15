# PLAN — M4: 사용 가능한 recorder (2026-07-13 ~ 07-14, 완료)

Goal: 브라우저 UI에서 start/stop/abort로 bi_so101 v3 에피소드를 수집·검증·승격하는 단일 시스템.
Done when: fake_robot(합성 5캠+12차원) 대상 M4 수락 게이트 통과 —
  "5캠·12차원·30Hz 에피소드 수집·finalize 후 별도 프로세스에서 v3 재오픈 전수 검증" + UI로 동일 루프 조작 가능.
Out of scope: HF 업로드, 학습/추론, 데이터 편집, 실로봇 연결(M3 이후), WebRTC.

## Milestones — 전체 완료 ✅
- [x] M4-①: 골격+Docker+빌드 검증 — 9a7b7c4
- [x] M4-②: 구독·버퍼·샘플러·게이트 + fake_robot — smoke PASS, 1789a6d
- [x] M4-③/④: v3 writer + FSM + staging→검증→승격 + validate CLI — smoke PASS, a29034b
- [x] M4-⑤: FastAPI 게이트웨이(REST/WS/MJPEG) + UI — smoke PASS + 호스트 라이브 REST 녹화
       사이클로 v3 데이터셋 승격·READY 확인, 573ac74

## 수락 게이트 증적 (2026-07-14)
- 컨테이너 smoke_m4_3/smoke_m4_5 PASS.
- 호스트 라이브: `POST /record/start`(task=browser_demo) → 5s → `POST /record/stop` →
  `datasets/raw/bi_so101_v1/browser_demo/real/bi_so101_v1__browser_demo__real__20260714_173641`
  승격(meta/data/videos/validation.json), FSM READY.

## 실행 방법
```bash
cd "/home/sa08050/app developer/v2_ws"
docker run -d --name v2_recorder -p 8080:8080 \
  -v "$PWD":/ws -v /home/sa08050/robot_store:/home/sa08050/robot_store \
  recorder_core:m4 bash -c '
    set +u; source /opt/ros/jazzy/setup.bash; source /ws/install/setup.bash; set -u; cd /ws
    setsid ros2 run recorder_core fake_robot --ros-args -p manifest_path:=/ws/manifests/bi_so101_01.yaml > /tmp/fake.log 2>&1 &
    sleep 2
    exec ros2 run recorder_core recorder_gateway /ws/manifests/bi_so101_01.yaml --ui-dir /ws/ui'
# 브라우저: http://localhost:8080  (실로봇 전환 시 fake_robot 대신 브리지 노드 + manifest 토픽만 일치시키면 됨)
```

## Decisions log
- 2026-07-13: MVP 단일 프로세스(rclpy 스레드 + FastAPI 메인). 분리는 soak 결과에 따라.
- 2026-07-13: UI는 vanilla HTML/JS 단일 페이지(Claude 작성), 백엔드는 UI 상단 주석의 API 계약 구현.
- 2026-07-13: 벡터 조립은 이름 기반 매핑. writer 블로킹 작업 전부 워커 스레드.

## 디버그 교훈 (전부 컨테이너 검증에서만 발견)
1. LeRobotDataset.create가 executor 블로킹(~1.7s) → 구독 기아 → 게이트 오발동. 워커 스레드로 격리.
2. 에피소드 시작 버퍼 리셋 직후 게이팅 레이스 → WARMUP이 "리셋 후 신규 수신" 요구.
3. rclpy CompressedImage.data는 array.array — isinstance(bytes) 검사는 실기에서 실패.
4. lerobot v0.6 torchcodec 디코딩은 시스템 FFmpeg 필수(없으면 검증만 실패, 인코딩은 됨).
5. ros2 run 래퍼 단일 kill → 자식 노드 좀비 publish. setsid+프로세스그룹 kill.
6. codex 프로세스가 완료 후 미종료(lingering) 가능 — 대기 루프는 로그 정체 시간도 감시할 것.

## 남은 것 (M4 범위 밖)
- 30~60분 soak(인코더 backlog/메모리), 실로봇 브리지 연결(M3 하드웨어 후), HF/학습/추론 게이트.

## Robotis 기능 동등화 (2026-07-15 완료)
- [x] M4-⑥ 다중 에피소드+컨트롤+instruction+시스템상태 (1957c57)
- [x] M4-⑦ 데이터셋 브라우저+HF 업로드 (9451d11)
- [x] M4-⑧ 학습 관리자 — UI발 30스텝 ACT DONE 실증 (84cb5b2)
- [x] M4-⑨ 추론 관리자 — UI발 12.7Hz 폐루프 실증, sim 전용 가드 (58b480b, 41c5078)
데이터 편집기는 raw 불변 원칙에 따라 의도적으로 미구현. 실로봇 추론은 M7 안전게이트 선행.
