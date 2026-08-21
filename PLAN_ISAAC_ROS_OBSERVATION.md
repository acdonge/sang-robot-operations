# PLAN - Isaac ROS 2 observation bridge

## Planner

### Goal

OMY와 RB5 vendor scene의 관절 및 RGB 카메라 관측을 ROS 2 Jazzy로 읽기 전용 발행하고,
기존 Robot Agent와 Admin UI에서 simulation workcell을 실제 topic freshness로 확인한다.

### Scope for this iteration

- Isaac Sim 내장 Jazzy `rclpy`를 사용한다.
- 관절은 `sensor_msgs/msg/JointState`, RGB는 JPEG `sensor_msgs/msg/CompressedImage`로 발행한다.
- OMY `cam_top`/`cam_wrist`, RB5 `cam_front`/`cam_wrist`를 30 FPS로 제공한다.
- 시뮬레이션 전용 ROS domain 99를 유지하고, Docker Agent 호환을 위해 discovery는 SUBNET을
  기본값으로 사용한다. publisher에는 command subscriber가 없으므로 관측 경로만 노출된다.
- command subscriber, trajectory/action 입력, 실제 하드웨어 제어, depth 발행은 구현하지 않는다.

### Tasks

1. Camera/topic/output 설정을 vendor profile에 추가하고 순수 검증 테스트를 만든다.
2. OMY runtime camera와 RB5 existing camera를 생성/초기화한다.
3. read-only ROS 2 observation publisher와 bounded runtime CLI를 구현한다.
4. ROS topic rate/type/image decode를 실측한다.
5. sim Robot Agent를 Central에 연결해 Admin 상태와 heartbeat를 검증한다.

### Acceptance Criteria

- OMY 7개, RB5 6개 관절이 manifest 순서와 같은 이름으로 20 Hz 이상 관찰된다.
- 필수 RGB 카메라 4개가 각 25 Hz 이상이며 JPEG decode와 기대 해상도를 통과한다.
- sim manifest의 모든 required source가 fresh/rate-ok가 되어 Agent snapshot이 `READY`다.
- runner report에 topic별 publish count/rate, 마지막 JPEG 크기와 read-only safety 상태가 남는다.
- ROS graph에 이번 구현이 만든 subscriber가 0개이고 hardware command가 0개다.
- 기존 Fleet 테스트 전체와 vendor profile/manifest 테스트가 회귀 없이 통과한다.

### Risks / Notes

- Isaac rendering 속도는 GPU 부하에 따라 달라진다. 30 FPS를 못 지키면 해상도나 렌더 설정을
  조정하되 topic 계약은 바꾸지 않는다.
- OMY full scene에는 공식 ROBOTIS table asset이 없으므로 table camera-link pose를 직접 합성하면
  작업 영역이 보이지 않는다. 공식 해상도/렌즈는 유지하고 full scene workcell 중심을 바라보는
  runtime top-camera pose를 profile에 둔다. full scene 원본은 수정하지 않는다.
- RB5 원본 peg/TCP visual reference 경고는 camera/articulation telemetry와 별개로 유지된다.
- synthetic RGB는 JPEG로 새로 인코딩되므로 물리 MJPG 카메라의 zero-copy 경로와는 다르다.

### Developer Instruction

`sim/isaac_vendor`에 read-only observation bridge를 최소 구현한다. publisher 외 ROS entity를
생성하지 말고, 두 profile의 실제 topic과 Admin 상태를 end-to-end 검증한다.

## Milestones

- [x] M1: Profile camera/topic contract and pure tests
- [x] M2: OMY runtime cameras and ROS telemetry smoke
- [x] M3: RB5 D455 RGB ROS telemetry smoke
- [x] M4: Robot Agent/Admin READY verification and regression tests

## Decisions Log

- 2026-07-22: LeIsaac 환경에 ROBOTIS DDS Python/rclpy가 기본 설치되어 있지 않으므로,
  Isaac Sim 5.1에 포함된 official Jazzy ROS 2 bridge의 internal `rclpy`를 사용한다.
- 2026-07-22: OmniGraph command node는 만들지 않고 Python publisher만 사용한다.
- 2026-07-22: OMY camera intrinsics/pose는 ROBOTIS `robotis_lab`, RB5 camera prim은 제공 scene을
  source of truth로 사용한다.
- 2026-07-22: OMY top camera는 official table이 빠진 composed scene에 맞춰 world pose만
  workcell-adapted 값으로 사용한다. wrist relative pose와 두 camera intrinsics는 official 값을 유지한다.
- 2026-07-22: ROBOTIS 설정의 `convention="isaac"`는 현재 IsaacLab 2.3의 지원 규약이 아니며
  그대로 렌더링하면 wrist camera가 링크 내부를 향한다. 공식 mount 위치에서 작업면 방향으로
  0.10 m 전진한 composed-scene 보정을 profile에 명시해 검은 영상을 제거한다.
- 2026-07-22: LOCALHOST discovery는 Docker Agent에서 graph만 보이고 payload locator가 연결되지
  않았다. sim 전용 domain 99와 Fast DDS UDPv4를 유지하면서 기본 discovery를 SUBNET으로 바꾼다.
- 2026-07-22: 동기식 DDS publish와 매 frame 전체 pixel 통계는 카메라 주기를 25 Hz 아래로
  떨어뜨렸다. Fast DDS asynchronous publish와 최초 frame 통계만 사용해 관측 주기를 복구한다.

## Current State

M4 완료. 독립 Jazzy Docker subscriber 기준 OMY 관절/848x480 JPEG 두 개는 약 26.8 Hz,
RB5 관절/640x480 JPEG 두 개는 약 29.9 Hz로 수신·디코딩됐다. Admin 연결 검증에서는 OMY가
약 30.18 Hz, RB5가 약 31.0 Hz로 모든 필수 source를 충족해 각각 `READY`가 됐다. publisher의
subscription/hardware-command count는 모두 0이며 집중 테스트 56개가 통과했다.
