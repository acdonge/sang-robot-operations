# PLAN — M4: 사용 가능한 recorder (2026-07-13)

Goal: 브라우저 UI에서 start/stop/abort로 bi_so101 v3 에피소드를 수집·검증·승격하는 단일 시스템.
Done when: fake_robot(합성 5캠+12차원) 대상 M4 수락 게이트 통과 —
  "5캠·12차원·30Hz 에피소드 수집·finalize 후 별도 프로세스에서 v3 재오픈 전수 검증" + UI로 동일 루프 조작 가능.
Out of scope: HF 업로드, 학습/추론, 데이터 편집, 실로봇 연결(M3 이후), WebRTC.

## Milestones
- [x] M4-①: 골격+Docker+빌드 검증 — commit 9a7b7c4 ✅
- [ ] M4-②: manifest 기반 구독·latest-value 버퍼·30Hz 샘플러·freshness 게이트 + fake_robot 퍼블리셔
       — verify: 컨테이너에서 fake_robot 대상 health OK + 소스 드롭 시 게이트 발동
- [ ] M4-③: v3 writer + FSM(PREFLIGHT→…→READY, ABORTING 원자 트랜잭션) + staging→reopen→승격
       — verify: 수락 게이트(위) 통과, abort 시 잔여물 없음
- [ ] M4-④: validate_dataset CLI(별도 프로세스 재오픈 검증) — ③ 검증에 사용
- [ ] M4-⑤: FastAPI 게이트웨이(REST/WS/MJPEG) + 단일 페이지 UI(로봇 카드·N캠 그리드·start/stop/abort·health)
       — verify: 브라우저 수동 확인 + curl 시나리오

## Decisions log
- 2026-07-13: MVP는 단일 프로세스(rclpy 스레드 + FastAPI 메인). 분리는 soak 결과에 따라.
- 2026-07-13: UI는 React 빌드체인 없이 vanilla HTML/JS 단일 페이지(FastAPI 정적 서빙) — Claude 직접 작성.
- 2026-07-13: 샘플러의 벡터 조립은 이름 기반 매핑(순서 무관, 전 이름 필수).

## Current state
M4-① 완료. M4-② Codex 스펙 발주.
