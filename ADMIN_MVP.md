# SANG Fleet/Admin MVP

## 구현 범위

현재 프로그램은 읽기 전용 로봇 관측 경로와 loopback 운영 구성 vertical slice입니다.

- heartbeat v1 validation과 SQLite 최신 snapshot 저장
- Agent/topic freshness·rate 전이 이력 10,000건과 중복 없는 reconnect 집계
- 중앙 수신 시각 기준 `online <= 12 s`, `stale <= 30 s`, `offline > 30 s`
- Agent/robot/source/storage 상태, topic Hz/age/sequence, 중복 없는 attention 집계
- timer 기반 WebSocket 갱신과 반응형 Admin UI
- Fleet Sources 진입점, 사이드바 메뉴, 로봇 선택기를 가진 읽기 전용 Robot Detail 화면
- 선택한 카메라 한 대만 명시적으로 연결하는 Agent 로컬 MJPEG 미리보기
- Processing Node, Asset, Workcell, 버전된 Task Definition, Task Run 구성
- Task Run에 배정된 자산만 받는 별도 Operator 앱과 run-scoped WebSocket
- manifest 기반 `robot_agent`의 ROS 2 topic freshness 감시
- manifest 최소 Hz 계약과 strict soak/reconnect 검증 CLI
- SO-101, OMY, RB5 화면 검증을 위한 선택적 seed

관리자 backend의 write endpoint는 Agent heartbeat 수신과 loopback 구성 생성뿐입니다. 구성 API는
Node·Asset·Workcell·Task Definition·Task Run 메타데이터만 기록합니다. Fleet/Robot Detail,
Operator와 카메라 미리보기는 read-only 경로만 사용합니다. 로봇 제어, 녹화 시작, 정책 실행,
학습, 업로드, workflow 실행 endpoint는 없습니다.

## Admin과 Operator 분리

- Admin Fleet(`/`)는 전체 Agent/robot/source를 진단하는 저수준 운영 화면입니다.
- Admin 구성(`/config.html`)은 관측 후보를 자산으로 확정하고 Workcell, Task 역할, Task Run을
  만드는 loopback 전용 화면입니다.
- Operator(`/operator/#run/<run_id>`)는 선택한 Task Run의 배정 자산만 봅니다. 브라우저에서 전체
  Fleet를 받은 뒤 숨기지 않고 backend가 Agent/robot/camera source를 먼저 필터링합니다.
- 카메라는 Operator 화면 진입만으로 연결되지 않으며, 사용자가 선택한 한 스트림만 MJPEG로
  수신합니다.
- 상세 설계와 불변 조건은 `PLAN_TASK_SCOPED_OPERATIONS.md`를 기준으로 합니다.

## Robot Detail과 카메라 미리보기

- URL은 `#fleet`과 `#robot/<agent_id>/<robot_id>` 두 화면으로 구분합니다. Agent와 robot ID를
  함께 써 여러 Agent가 같은 robot ID를 사용해도 충돌하지 않습니다.
- Fleet 행의 Sources 셀, selected-agent inspector의 `로봇 상세`, 사이드바의 `로봇 상세`가 같은
  상세화면으로 연결됩니다. Fleet 행의 나머지 영역은 기존 Agent 선택 동작을 유지합니다.
- 상세화면은 robot/source/host resource/event/local dataset 정보를 한 곳에 보여줍니다. resource와
  dataset은 로봇별 값이 아니라 **호스트 Agent 기준**임을 UI에 표시합니다.
- 상세화면 진입만으로 영상은 수신하지 않습니다. 사용자가 카메라를 고르고 `미리보기 시작`을
  누르면 한 스트림만 열며, 카메라 변경·화면 이탈·중지 시 연결을 닫습니다.
- `robot_agent`는 `CompressedImage`의 최신 JPEG 한 장만 RAM에 보관하고 MJPEG로 전달합니다.
  JPEG bytes는 heartbeat와 SQLite에 저장하지 않습니다.
- 현재 MVP의 preview listener와 public URL은 loopback HTTP만 허용합니다. 중앙 PC에서 실행되는
  Agent와 로컬 관리자 브라우저 검증용 경계이며, Robot PC 분산 배치 전에는 Agent 인증과 TLS/mTLS,
  중앙 reverse proxy 또는 승인된 Agent URL 계약이 먼저 필요합니다. 브라우저 CORS도 현재 Admin의
  loopback origin(`127.0.0.1:8090`, `localhost:8090`, IPv6 loopback)만 허용합니다.

## Robot Agent 경계

`robot_agent`는 manifest에 선언된 다음 message type을 구독만 합니다.

- `sensor_msgs/msg/JointState`
- `trajectory_msgs/msg/JointTrajectory`
- `sensor_msgs/msg/CompressedImage`

평상시 health에 필요한 source는 `monitoring.required_sources`로 따로 지정합니다. SO-101의 action
topic은 idle일 때 발행되지 않을 수 있으므로 관찰은 하지만 기본 health 필수 source에는 포함하지
않습니다. 한 Agent 프로세스에 `--manifest`를 여러 번 전달해 같은 PC의 여러 로봇을 묶을 수 있습니다.
카메라 미리보기가 필요한 Agent만 `--preview-port <port>`를 지정합니다. 기본값 `0`은 preview
server 비활성화이며, listener는 이번 버전에서 `127.0.0.1` 또는 `localhost`에만 bind할 수 있습니다.

## 의도적으로 제거한 기능

- 이전 `recorder_core`와 Recorder UI/FSM
- LeRobot dataset writer와 validation/promotion 구현
- 로컬 학습·추론·Hugging Face 업로드 manager
- fake robot과 M4 smoke scripts
- gateway polling reporter

이 기능들은 새 Agent 계약, 데이터 시간 정합, 안전 제어권과 분산 인증이 정리된 후 독립 worker나
Operator 앱으로 다시 설계합니다. 삭제한 기존 gateway를 다시 중앙 제어 backend로 복구하지 않습니다.

## 다음 개발 순서

1. [완료] `fleet_soak`로 SO-101 2시간 soak와 사용자 입회 USB 재연결 수행
2. [완료] OMY와 RB5 read-only manifest 및 공식 topic contract 추가
3. [진행] OMY 실물 Agent의 Robot Detail과 손목 카메라 on-demand MJPEG 검증
4. [다음] OMY와 RB5 Robot PC에서 실제 topic/주기/재연결 conformance 검증
5. Agent 인증과 TLS/mTLS를 추가해 원격 Robot PC 연결과 preview relay 구성
6. Task Run lifecycle 전이와 사용자/권한, Agent 인증, 원격 preview relay 구현
7. Control Mode Manager, command mux, lease, TTL/deadman과 local safety 구현
8. timestamp/skew 계약을 가진 dataset spool worker와 중앙 catalog 구현
9. training worker, policy approval/release, sim-to-real 검증 추가
10. typed workflow, resource/capacity/zone lock 기반 다중 로봇 실행 구현

RB5 공식 [`rbpodo_ros2`](https://github.com/RainbowRobotics/rbpodo_ros2)는 현재 ROS 2 Humble을
대상으로 하고 active development 경고가 있으므로, Jazzy 중앙 환경에 직접 섞지 않습니다. 먼저
별도 Robot PC에서 vendor driver와 read-only Agent의 관찰 호환성을 검증한 뒤 원격 연결 보안을
추가합니다.

## AAS

AAS는 나중에 robot/camera/EEF/calibration/capability/maintenance/dataset/policy metadata를 외부에
노출하는 상호운용 계층으로 추가합니다. ROS 2 Actions, safety, lease와 고주파 telemetry의 runtime
경로를 AAS로 대체하지 않습니다.
