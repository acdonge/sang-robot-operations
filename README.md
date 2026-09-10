# SANG Fleet Workspace

SANG은 이기종 로봇·카메라·Processing PC를 작업 단위로 묶어 관리하는
**OPC UA 기반 분산 로봇 운영·데이터·디지털 트윈 플랫폼**입니다.
ROS 2 관측, 자산/작업 관리, 선택 구간 녹화·재생, 읽기 전용 OPC UA,
AAS 문서 연결, 버전 고정 Action/Workflow와 격리된 시뮬레이션 실행을 통합합니다.
실물 로봇과 용접 장비를 조종하는 제품으로 출시된 상태는 아닙니다.
이전 V2 Recorder와 직접 기록형 LeRobot writer, 학습·추론·HF 업로드, 구 UI는 제거했습니다.
대신 검증된 MCAP 원본에서 observation-only LeRobot v3 데이터셋을 만드는 별도 파생 경로를 제공합니다.

## 처음 읽는 분께

- 전체 목표, 확장된 OPC UA의 의미, PC별 역할과 데이터 흐름:
  [`docs/SANG_PLATFORM_OVERVIEW_KO.md`](docs/SANG_PLATFORM_OVERVIEW_KO.md)
- 현재 상태와 다음 우선순위: [`SANG_MASTER_IMPLEMENTATION_PLAN.md`](SANG_MASTER_IMPLEMENTATION_PLAN.md)
- GitHub에 포함되는 소스와 별도로 준비할 데이터/에셋:
  [`docs/GITHUB_SOURCE_POLICY.md`](docs/GITHUB_SOURCE_POLICY.md)

2026-09-10 체크포인트: 고정된 Scenario 1의 5단계를 웹에서 Isaac으로 실행하고,
별도 Unity 관측 화면이 측정값을 따라 표시하는 경로를 검증했습니다. 같은 Isaac 세션에서
2회 완료·Reset·PASS를 확인했습니다. 운반체와 집기는 단순화한 가상 모델이며
OMY 베이스 시각 표시와 장기 반복 오차는 보완 대상입니다.
[`실행·검증 기록`](docs/ISAAC_SCENARIO1_EXECUTION_2026-09-09.md)을 참조하세요.

## 기준 문서

- 전체 구현 상태와 다음 순서는 [`SANG_MASTER_IMPLEMENTATION_PLAN.md`](SANG_MASTER_IMPLEMENTATION_PLAN.md)가
  단독 기준입니다.
- 필수 기능·보안·안전 경계는 [`docs/requirements.md`](docs/requirements.md)를 따릅니다.
- 설계·운영·날짜별 검증 문서의 역할과 우선순위는
  [`docs/DOCUMENT_AUTHORITY_INDEX.md`](docs/DOCUMENT_AUTHORITY_INDEX.md)에서 확인합니다.

루트의 다른 `PLAN_*.md`는 해당 구현 당시의 실행 기록입니다. 날짜가 붙은 결과 문서는 당시 빌드와
토폴로지에 대한 증거이며, 현재 로드맵이나 다른 하드웨어의 승인으로 확대 해석하지 않습니다.

## 현재 구성

```text
SO-101 buses/cameras ──> so101_readonly_adapter ──> ROS 2 topics
RB5 data port 5001 ──> rb5_readonly_adapter ──> ROS 2 joint state
RB5 D405 Robot Agent PC ──> compressed ROS 2 image ──> on-demand preview
Robot ROS 2 topics ──> robot_agent ──heartbeat v1(loopback) / v2(mTLS)──> fleet_control_plane ──> ui-admin
          ├─local health snapshot─> sang_opcua_gateway ──OPC UA read-only──> Central shadow Client
          ├─allowlisted observations─> sang_mcap_recorder ──> local immutable MCAP ──verified upload─> Central Object Store
          │                                                          └─quality PASS─> observation-only LeRobot v3
          ├─fixed capture worker <──mTLS pull── Task Run Capture Job <── ui-operator
          └────────raw values────> sang_opcua_gateway
                                                        ├─run scope──────> ui-operator
                                                        └─SQLite snapshot/catalog─> Central read-only OPC UA
Galaxy XR ──> xr_teleop_bridge ──> xr_teleop_retarget (개발 중, 입력/preview 전용)
```

- `src/fleet_control_plane`: 중앙 Agent/자산/TaskRun registry, 인증·감사, 영속 Capture Job,
  녹화 catalog/Object Store, observation-only LeRobot 파생, backup/restore와 read-only OPC UA aggregate
- `src/robot_agent`: manifest 기반 ROS 2 관절/action/camera topic freshness 감시
- `src/so101_readonly_adapter`: SO-101 4개 bus 읽기와 Innomaker 카메라 MJPG publish
- `src/sang_opcua_gateway`: Phase 0 계약을 사용하는 loopback read-only OPC UA Edge sidecar
- `src/rb5_readonly_adapter`: RB5 제어박스 데이터 포트의 6축 실측 관절 읽기
- `src/sang_mcap_recorder`: 허용된 관측 토픽만 로컬 MCAP 원본 세션으로 기록
- `contracts/assets`: immutable Asset Package, Unity Scene Binding, scene inventory와
  output-disconnected Simulation Qualification JSON Schema
- `contracts/aas`: 고정된 IDTA 3.1.2 metamodel JSON Schema와 출처·해시 lock
- `frontend/admin`: 모든 웹 화면의 React + TypeScript + Ant Design 원본
- `ui-admin`: 전체 Fleet 진단과 loopback 운영 구성 Console
- `ui-operator`: 선택한 Task Run의 관측과 고정 프로필 MCAP 녹화만 수행하는 Processing/Operator 화면
- `src/xr_teleop_*`: Galaxy XR 입력과 SO-ARM target preview 개발 코드
- `manifests`: 로봇별 ROS 2 topic과 상태 감시 계약

`robot_agent`, MCAP recorder, LeRobot 파생기와 XR는 actuator 명령을 publish하지 않습니다.
현재 파생기는 측정 관절값과 카메라만 내보내며 `action` feature를 만들지 않습니다. VLA 학습과
정책 실행은 아직 이 저장소에 포함되어 있지 않습니다.

범용 다중 로봇 제어는 기존 읽기 전용 경로에 임의로 명령을 추가하지 않고
[`docs/MULTI_ROBOT_ORCHESTRATOR_IMPLEMENTATION_DESIGN.md`](docs/MULTI_ROBOT_ORCHESTRATOR_IMPLEMENTATION_DESIGN.md)의
계약을 따릅니다. G1-G6a와 출력 권한이 없는 G6b 증빙 제출·검토 소프트웨어는 구현됐지만, 이는
실물 제어 승인이 아닙니다. Central은 의도·감사·delegation grant, Processing PC는 Cell
Orchestrator와 순서·복구, Device Agent는 로컬 제조사 실행을 담당합니다. Scenario 1/2는 제품
분기가 아니라 적합성 fixture이며, G7의 입회 승인 전까지 물리 `command_sink_present`는
`false`로 유지합니다. G6b의 측정 자료와 첨부파일은 해시가 확인된 불변 Object Store 객체로
제출하며, Manager와 Safety Officer의 검토가 끝나도 G7 권한은 생기지 않습니다. 현재 G0 JSON
Schema, 정적 검증 규칙, 3대/12대 카메라 예제는
[`contracts/orchestration/README.md`](contracts/orchestration/README.md)에서 확인할 수 있습니다.
구현 상태와 남은 G6b/G7 조건은
[`docs/G1_G6_IMPLEMENTATION_STATUS_2026-08-28.md`](docs/G1_G6_IMPLEMENTATION_STATUS_2026-08-28.md)에
기록돼 있습니다.

Robot/Camera Operations v1의 저장소 구분, 최초 설치, Edge 업로드, 원격 TLS/OPC UA,
backup·upgrade·rollback 절차는 [`docs/ROBOT_CAMERA_OPERATIONS_V1.md`](docs/ROBOT_CAMERA_OPERATIONS_V1.md)를
기준으로 합니다. 최신 소프트웨어 게이트 결과는
[`docs/ROBOT_CAMERA_OPERATIONS_V1_VALIDATION_2026-08-21.md`](docs/ROBOT_CAMERA_OPERATIONS_V1_VALIDATION_2026-08-21.md)에
기록했습니다. AAS와 벡터 DB는 이 계약을 대체하지 않는 후속 계층입니다. Central은 IDTA 3.1.2
JSON 및 JSON-bearing AASX를 검증해 기존 Asset에 읽기 전용으로 연결하고, 원본·정규화본을 Object
Store와 검증 백업에 보존합니다. Unity 쪽에는 안정적인 Asset/Frame/Sensor inventory 패키지가
추가됐습니다. 실제 Scenario 1 Player에 9개 자산의 연결·컴파일을 적용했고, 게시된 Task의 역할과
동작을 정확한 자산/장면/소스 버전에 연결하는 동적 adapter를 구현했습니다. 현재는 미리 빌드한 한
장면과 순차 동작만 지원하며, Reset 후 OMY 반복 동작 검증·서명된 배포·두 번째 장면 재사용·ROS 2
녹화 연동은 남아 있습니다. 실행 증거와 제한은
[`docs/UNITY_DYNAMIC_BINDING_A1_A2_2026-09-09.md`](docs/UNITY_DYNAMIC_BINDING_A1_A2_2026-09-09.md),
전체 설계는 [`docs/AAS_ASSET_PACKAGE_DYNAMIC_UNITY_ADAPTER_DESIGN.md`](docs/AAS_ASSET_PACKAGE_DYNAMIC_UNITY_ADAPTER_DESIGN.md)에 있습니다.

### 로컬 MCAP 녹화 Inventory

`robot_agent --capture-root /var/lib/sang/captures`를 지정하면 Agent는 로컬 세션의
`session-manifest.json`만 제한적으로 읽어 Central heartbeat에 메타데이터를 추가합니다. Admin의
Fleet 요약과 Agent/Robot 상세에서 완료·실패·partial·invalid 수, 선언된 크기, 토픽·메시지 수,
ROS 전송 손실과 Task Run 참조를 확인할 수 있습니다. 최근 20개만 전송하며 최대 1,000개를
집계합니다.

Heartbeat는 MCAP 원본, 이미지·영상, SHA-256 체크섬, recorder log와 로컬 경로를 전송하지
않습니다. 완료 세션은 독립된 `fleet_recording_upload` 경로로만 SHA-256과 함께 중앙 Object
Store에 전송되고, SQLite에는 검색용 metadata만 저장됩니다. Operator는 ACTIVE Task Run에서
ONLINE Capture Worker의 고정된 읽기 전용 프로필만 시작·중지할 수 있습니다. Job과 이벤트는
Central SQLite에 먼저 저장되므로 UI 새로고침과 Robot Agent 재시작 후에도 상태를 복구하며,
동일 client request ID는 새 녹화를 만들지 않습니다. `.partial`은 보존되고 실패로 명시됩니다.
Admin Catalog는 녹화 명령을 만들지 않고 완료된 MCAP, 객체별 SHA-256과 provenance만 조회합니다.
완료 세션의 `sensor_msgs/msg/CompressedImage` JPEG는 Catalog에서 별도 도구 없이 재생할 수
있습니다. 세션을 고른 뒤 최대 네 카메라를 선택하면 하나의 MCAP(Message Capture) 기록 시간축으로
재생·탐색하며, 각 화면은 요청 시점과 실제 프레임의 차이를 표시합니다. 브라우저는 전체 MCAP을
받지 않고 선택한 프레임만 요청하며 Central은 원본 JPEG를 재인코딩하지 않습니다.
분산 배포에서는 additive 필드를 인식하는 Central을 먼저 갱신한 뒤 Edge Agent를 갱신합니다.

Task Run UI 녹화의 구현·운용 계약과 Lab1 실측 결과는
[`PLAN_TASK_RUN_CAPTURE_JOBS.md`](PLAN_TASK_RUN_CAPTURE_JOBS.md)와
[`docs/TASK_RUN_UI_CAPTURE_ACCEPTANCE_2026-08-24.md`](docs/TASK_RUN_UI_CAPTURE_ACCEPTANCE_2026-08-24.md)를
참조합니다.
웹 재생기의 범위와 실측 결과는 [`PLAN_MCAP_WEB_PLAYBACK.md`](PLAN_MCAP_WEB_PLAYBACK.md)와
[`docs/MCAP_WEB_PLAYBACK_ACCEPTANCE_2026-08-24.md`](docs/MCAP_WEB_PLAYBACK_ACCEPTANCE_2026-08-24.md)를
참조합니다.

### LeRobot observation-only 파생

Central의 `COMPLETE` 및 품질 `PASS` 세션에서 하나의 관절 상태와 한 개 이상의 JPEG 카메라를
선택해 LeRobot v3 데이터셋을 만듭니다. 원본 MCAP은 그대로 유지되고, SQLite에는 파생 이력과
해시만, Object Store에는 H.264 영상이 포함된 tar와 `sang-derivation.json`이 저장됩니다.

LeRobot `0.4.3`이 설치된 `leisaac` 환경에서 실행합니다.

```bash
cd "/home/sa08050/app developer/v2_ws"
source "$HOME/miniconda3/etc/profile.d/conda.sh"
conda activate leisaac

PYTHONPATH="$PWD/src/fleet_control_plane:${PYTHONPATH:-}" \
python -m fleet_control_plane.lerobot_derivation \
  --db-path runtime/phase3-lab1/central/fleet.db \
  --object-store-root runtime/phase3-lab1/objects \
  --work-root runtime/phase3-lab1/derivation-work \
  --session-id capture-c0d93d51f619448ca378a5ce3d669e64 \
  --robot-manifest manifests/rb5_lab1_eval_01.yaml \
  --state-topic-key rb5_joint \
  --camera-topic-key d405_color \
  --camera-topic-key d455_color \
  --camera-topic-key welding_view \
  --start-offset-s 10 \
  --end-offset-s 15 \
  --fps 30 \
  --task "Observe RB5 weld-cell motion with D405, D455, and OTOS cameras"
```

같은 요청은 동일한 파생 ID를 반환해 중복 데이터셋을 만들지 않습니다. 결과는 Admin
`/catalog.html`의 `LeRobot 데이터셋` 영역에서 조회하고 tar로 받을 수 있습니다. 정확한 계약과
첫 실측 결과는 [`docs/LEROBOT_DERIVATION_CONTRACT.md`](docs/LEROBOT_DERIVATION_CONTRACT.md)와
[`docs/LEROBOT_DERIVATION_ACCEPTANCE_2026-08-26.md`](docs/LEROBOT_DERIVATION_ACCEPTANCE_2026-08-26.md)를
참조합니다.

## Docker 환경

기존 LeRobot training image 대신 ROS 2 Jazzy와 Admin/Agent/XR 의존성만 설치합니다.

```bash
cd "/home/sa08050/app developer/v2_ws"
docker/run.sh
```

컨테이너 안에서:

```bash
source /opt/ros/jazzy/setup.bash
colcon build --symlink-install
source install/setup.bash
```

## 관리자 앱 실행

Fleet, Robot Detail, Operations Setup, Data Catalog, Login, Operator Station은 하나의 React +
Ant Design 시각 체계와 공통 셸을 사용합니다. 화면 원본은 `frontend/admin/`에 있으며,
`ui-admin/`과 `ui-operator/`는 직접 수정하지 않는 빌드 산출물입니다. UI 변경 후에는 다음 명령으로
전체 진입점을 함께 생성합니다. 이후 개발의 필수 디자인·상호작용·권한 규칙은
[`docs/UI_DESIGN_STANDARD.md`](docs/UI_DESIGN_STANDARD.md)를 따릅니다.
GitHub에는 화면 원본만 보관하므로 새로 내려받은 저장소에서도 서버 실행 전에 빌드해야 합니다.
아래 경로는 본인의 저장소 위치에 맞게 바꾸고, Node.js는 `frontend/admin/package-lock.json`의
도구 요구 버전에 맞춰 준비합니다.

```bash
cd "/home/sa08050/app developer/v2_ws/frontend/admin"
mkdir -p ../../ui-operator
npm ci
npm run build
```

인증된 중앙 서버를 처음 실행하기 전에 관리자 계정을 bootstrap해야 합니다. 전체 명령은
`docs/ROBOT_CAMERA_OPERATIONS_V1.md`를 따릅니다. 중앙 서버:

```bash
ros2 run fleet_control_plane fleet_control_plane \
  --db-path /tmp/sang-fleet.db \
  --ui-dir /ws/ui-admin \
  --operator-ui-dir /ws/ui-operator \
  --object-store-root /tmp/sang-objects \
  --host 127.0.0.1 \
  --port 18091
```

별도 터미널에서 SO-101 Agent:

```bash
source /opt/ros/jazzy/setup.bash
source install/setup.bash
ros2 run robot_agent robot_agent \
  --manifest /ws/manifests/bi_so101_01.yaml \
  --mapping /ws/contracts/opcua/bi_so101_01.mapping.v2.yaml \
  --agent-name "SO-ARM Workcell" \
  --control-plane-url http://127.0.0.1:18091 \
  --preview-port 8101
```

`http://127.0.0.1:18091`을 열면 실제 ROS 2 topic freshness가 표시됩니다. `/config.html`에서
Processing Node, Asset, Workcell, Task Definition과 Task Run을 순서대로 구성하고, 생성된 Run의
`OPERATOR 열기`를 누르면 `/operator/#run/<run_id>`에서 배정된 자산만 볼 수 있습니다. 센서 topic이 아직
발행되지 않으면 로봇은 `DEGRADED`로 보이는 것이 정상입니다.

`/workflow.html`은 실행 Workflow를 만드는 관리자용 Workflow Studio입니다. Draft만 수정할 수 있고,
Publish하면 Task Definition과 Workflow가 하나의 SQLite 트랜잭션으로 함께 등록되어 영구 불변이 됩니다.
변경하려면 Published library에서 `New version`을 선택하고, 기존 Task를 별도 작업의 출발점으로 쓰려면
`Copy as Task`를 선택합니다. 일반 편집 화면에서는 `Robot → registered Action → order`만 선택하며
Role, Capability, Operation, Step ID와 선형 의존성은 등록된 Action 계약에서 자동 생성됩니다. 각 Action은
로봇 종류, 버전, 실행 언어, 진입점과 SHA-256이 고정된 스크립트 하나를 가리키며 브라우저에서 코드를
업로드하거나 임의 경로를 실행할 수 없습니다. 기존 `/config.html` Task Run은 관측·녹화 메타데이터용이며
실행 버튼이 아닙니다.

`/actions.html`은 관리자 전용 Action Library입니다. 여기서는 `Robot → C#/Python source → Action
name → allowed Skill inputs` 순서로 Draft를 만들 수 있습니다. Source 파일은 중앙 데이터 디렉터리에
SHA-256 기반으로 저장되고 API는 원문을 다시 반환하지 않으며, 중앙 서버가 업로드 코드를 실행하지도
않습니다. 정적 Source Gate와 동일한 스크립트 해시가 설치된 output-disconnected Adapter의 완료된
Simulation Run 증거를 확인하는 Simulation Gate를 모두 통과해야만 불변 버전을 Publish할 수 있습니다.
새 소스가 아직 Unity 또는 ROS 2 시뮬레이션 빌드에 설치되지 않았다면 Simulation Gate가 이유를 표시하고
게시를 차단합니다. 검증 빌드 설치는 Build Owner가 별도 신뢰 경계에서 수행합니다.

브라우저에서 허용되는 실행은 물리 출력이 분리된 Unity Scenario 1뿐입니다. HTTP 서버는 실행하지 않고
SQLite 큐에만 기록하며, 같은 중앙 DB를 사용하는 별도 Worker가 고정 adapter profile과 Unity heartbeat를
검증한 뒤 처리합니다. 현재 v1 Worker는 중앙 서버와 같은 호스트에서 다음처럼 실행합니다.

```bash
source /opt/ros/jazzy/setup.bash
source install/setup.bash
ros2 run fleet_control_plane fleet_unity_sim_worker \
  --db-path /tmp/sang-fleet.db \
  --adapter-profile contracts/tasks/iitp-scenario1-unity-adapter.v1.json \
  --scenario-config config/unity_scenario1_simulation.yaml \
  --runtime-root runtime/workflow-simulations
```

Worker가 fresh/ready가 아니거나 profile digest가 다르거나 물리 출력 연결이 감지되면 Simulation Run은
생성되지 않습니다. lease가 만료된 실행은 자동 재시도하지 않고 `INTERRUPTED`로 남겨 입회 확인하게 합니다.
상세 계약과 검증 항목은 [`docs/WORKFLOW_STUDIO_V1_DESIGN.md`](docs/WORKFLOW_STUDIO_V1_DESIGN.md)에 있습니다.

Docker 기반 Lab 배포에서는 같은 Worker를 선택적 profile로 시작할 수 있습니다. Unity가 사용하는 host
bridge 디렉터리는 기본 `/tmp/sang-unity-scenario1`이며, 다른 고정 위치를 쓸 때만
`SANG_UNITY_BRIDGE_ROOT`를 지정합니다.

```bash
docker compose \
  --env-file runtime/phase3-lab1/config/central.env \
  -f docker/compose.phase3-central.yaml \
  -f docker/compose.phase3-lab1-preview.yaml \
  --profile unity-workflow up -d unity-workflow-worker
```

랩 네트워크의 다른 PC에서는 로컬 개발용 HTTP를 노출하지 않고 별도 HTTPS Manager를 사용합니다.
관리자 계정을 먼저 `fleet_identity bootstrap-admin`으로 한 번만 생성한 뒤 다음 서비스를 띄웁니다.

```bash
docker compose \
  --env-file runtime/phase3-lab1/config/central.env \
  -f docker/compose.phase3-central.yaml \
  -f docker/compose.phase3-lab1-preview.yaml \
  -f docker/compose.manager-secure.yaml \
  --profile recording up -d manager
```

기본 주소는 `https://10.0.0.8:18443/`이며 인증과 HTTPS를 끌 수 없습니다. 인증 없는 기존
`http://127.0.0.1:18091/`은 같은 PC의 개발 확인에만 남겨 둡니다.

원격 브라우저에는 공개 Root 인증서
`runtime/phase3-lab1/pki-processing-lab1/root/ca-cert.pem`만 안전한 경로로 전달해 신뢰 저장소에
설치합니다. `root/ca-key.pem`은 어떤 PC에도 복사하지 않습니다. Ubuntu 계열 클라이언트에서는 전달받은
인증서만 다음처럼 설치한 뒤 브라우저를 다시 시작합니다.

```bash
sudo install -m 0644 sang-lab-root-ca.pem \
  /usr/local/share/ca-certificates/sang-lab-root-ca.crt
sudo update-ca-certificates
```

현재 Lab 배포의 초기 로그인 ID는 `manager-admin`이고 비밀번호는 서버의
`runtime/phase3-lab1/pki-processing-lab1/roles/central/manager/auth/admin-password.txt`에만
권한 `0600`으로 보관됩니다. 비밀번호 내용을 로그나 문서에 기록하지 않습니다.

OMY와 RB5-850E도 동일한 Agent backend를 사용합니다. 현재 loopback 전용 단계에서는 Central과
Agent를 같은 PC에서 띄우고, 해당 드라이버가 ROS 2 topic을 발행한 뒤 manifest를 선택해 실행합니다.

```bash
# OMY PC
ros2 run robot_agent robot_agent \
  --manifest /ws/manifests/omy_01.yaml \
  --agent-name "OMY Assembly Cell" \
  --control-plane-url http://127.0.0.1:18091 \
  --preview-port 8102

# 중앙 서버에서 RB5 관리망 + read-only adapter + Agent 시작
scripts/start_rb5_readonly.sh
```

현재 OMY 계약은 ROBOTIS `omy_f3m_config.yaml`의 상태·leader·wrist camera topic과 추가 D455의
`/camera/cam_top/color/image_raw/compressed` RGB preview topic을 사용합니다. D455 depth 원본은
`/camera/cam_top/depth/image_rect_raw`로 발행하지만 현재 Admin preview 계약에는 포함하지 않습니다.
RB5 계약은 제조사 Python 라이브러리의 `rbpodo.CobotData`로 데이터 포트 `5001`만 요청하고,
실측 `jnt_ang`을 rad 단위 `/rbpodo/joint_states`로 발행합니다. command 포트, operation mode,
speed bar, move/task API와 EEF는 포함하지 않습니다. D405 손목 카메라는 별도 Robot Agent PC에서
`/rbpodo/camera/wrist/color/image_rect_raw/compressed`로 발행하며, Admin은 선택할 때만 포트
`8103`의 MJPEG preview를 수신합니다. 공유기에 연결된 제어박스 기본 주소
`10.0.2.7`과 통신하기 위해 스크립트는 서버 관리 인터페이스에 보조 주소 `10.0.2.100/24`를
추가하고, RB5 adapter와 Agent 컨테이너는 Fast DDS 상태 전달을 위해 host IPC를 공유합니다.
중지와 주소 제거는 `scripts/stop_rb5_readonly.sh`로 수행합니다. 실제 원격 Robot PC용 Agent
mTLS와 OPC UA `SignAndEncrypt` 경로는 구현됐으며, 두 번째 노트북 입회 검증 전까지는
`contracts/opcua/PHASE3_SECURITY.md`의 read-only 시험 구성으로만 사용합니다.

RB5 D405를 소유한 외부 Ubuntu Robot Agent PC에서 ROS 2 Jazzy와 `realsense2_camera`를 source한
뒤 아래 스크립트를 실행합니다. 848x480 10 fps RGB만 열고 depth, IMU, point cloud와 TF는
비활성화합니다. 카메라가 여러 대면 `RB5_CAMERA_SERIAL`에 D405 serial을 지정합니다.

```bash
cd "/path/to/v2_ws"
RB5_CAMERA_SERIAL=123456789012 scripts/start_rb5_wrist_camera.sh
```

Rainbow 제어박스 `10.0.2.7` 자체는 영상 스트리밍이나 일반 원격 shell을 제공하지 않으므로,
제조사 승인 없이 제어박스 OS에 패키지를 설치하지 않습니다. D405 USB는 중앙 PC 또는 별도
Robot Agent PC가 소유해야 합니다.

`--preview-port`를 지정한 Agent는 최신 `CompressedImage` JPEG 한 장만 RAM에 보관합니다. Admin의
Robot Detail에서 사용자가 `미리보기 시작`을 누를 때 선택한 카메라 한 대만 MJPEG로 전송하며,
영상 bytes는 heartbeat나 SQLite에 저장하지 않습니다. 이번 loopback MVP에서는 Agent마다 서로
다른 포트를 사용합니다. 분산 Agent는 외부에서 접근 가능한 HTTPS preview URL만 광고합니다.
Central은 Task Run에 배정된 카메라를 사용자가 선택했을 때만 relay하며, Edge는 공유 CA뿐 아니라
Central preview-client certificate의 정확한 SHA-256 pin을 확인합니다.

Central은 Agent online/stale/offline, topic stale/recovered, rate low/recovered 전이만 SQLite에
저장합니다. 동일 heartbeat는 이벤트를 중복 생성하지 않으며 최신 10,000건을 유지합니다.
Admin의 `Health history`에서 선택한 Agent의 최근 이벤트와 복구 횟수를 볼 수 있고,
`GET /api/v1/events?agent_id=<id>&limit=100`으로도 조회할 수 있습니다.

### SO-101 실제 센서 실행

Agent보다 먼저 서로 다른 두 터미널에서 read-only adapter를 하나씩 실행합니다.

팔 reader 터미널:

```bash
export ROS_DOMAIN_ID=42
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST

ros2 run so101_readonly_adapter arm_reader \
  --config /ws/config/bi_so101_hardware.yaml
```

카메라 reader 터미널:

```bash
export ROS_DOMAIN_ID=42
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST

ros2 run so101_readonly_adapter camera_reader \
  --config /ws/config/bi_so101_hardware.yaml
```

카메라는 `config/bi_so101_hardware.yaml`의 USB `by-path`를 사용하므로 `/dev/videoN` 번호가
바뀌어도 물리 포트 기준 이름을 유지합니다. Docker에서 물리 재연결까지 지원하려면 시작 시점의
`/dev/videoN`만 `--device`로 고정하지 말고, 아래처럼 host `/dev`의 hot-plug 변화를 전파하면서
video major(81)만 cgroup으로 허용합니다. 팔 serial은 필요한 네 장치만 별도 `--device`로
허용하고 `--privileged`는 사용하지 않습니다.

```bash
--device-cgroup-rule='c 81:* rmw' \
--mount type=bind,source=/dev,target=/dev,bind-propagation=rslave
```

Adapter는
`MJPG 640x480 @30fps` 협상을 강제하고 카메라의 압축 JPEG를 디코딩/재압축 없이
`sensor_msgs/msg/CompressedImage`로 전달합니다.

### SO-101 OPC UA Phase 1

SO-101 adapter와 Agent가 실행된 상태에서 별도 터미널로 읽기 전용 Edge sidecar를 시작합니다.
카메라 영상 bytes는 OPC UA에 포함되지 않고, Agent의 on-demand MJPEG URI와 source health만
노출됩니다. Phase 1 endpoint는 loopback과 `SecurityPolicy=None`만 허용합니다.

```bash
ros2 run sang_opcua_gateway sang_opcua_gateway \
  --agent-id workcell-soarm-01 \
  --endpoint opc.tcp://127.0.0.1:4840/sang/ \
  --preview-base-url http://127.0.0.1:8101
```

실기 ROS 값·`SourceTimestamp`, idle StatusCode, 5개 카메라 health, 쓰기 거부와 영상 bytes
비노출을 한 번에 확인합니다.

```bash
ros2 run sang_opcua_gateway sang_opcua_validate \
  --duration-s 10 \
  --report /ws/output/validation/opcua-phase1-live-final.json
```

Gateway OFF/ON 각각 동일 조건 5분 soak를 3회 실행한 뒤 비교합니다.

```bash
ros2 run fleet_control_plane fleet_soak_compare \
  --baseline output/validation/opcua-off-1.json output/validation/opcua-off-2.json output/validation/opcua-off-3.json \
  --gateway output/validation/opcua-on-1.json output/validation/opcua-on-2.json output/validation/opcua-on-3.json \
  --report output/validation/opcua-phase1-performance.json
```

### SO-101 OPC UA Phase 2

Agent가 heartbeat와 동일한 versioned health snapshot을 loopback HTTP로 제공합니다. Gateway는
snapshot의 identity, boot sequence, manifest/payload digest와 health 일관성을 검증해 그대로
투영하고, raw 관절/카메라 값과 개별 `StatusCode`만 자체 ROS subscription에서 계산합니다.
12초 동안 새 snapshot이 없으면 자체 health로 fallback하지 않고 명시적으로 degraded가 됩니다.

Central shadow는 SQLite나 `health_events`에 쓰지 않으며, snapshot time이 일치한 37개 health
Variable만 비교합니다. Agent heartbeat의 online/age/진행 여부와 OPC UA 연결 epoch별 strict
비교도 함께 검사합니다.

```bash
ros2 run fleet_control_plane fleet_opcua_shadow \
  --agent-id workcell-soarm-01 \
  --robot-id bi_so101_01 \
  --duration-s 7200 \
  --final-gate \
  --model-contract /ws/contracts/opcua/model-contract.yaml \
  --report /ws/output/validation/opcua-phase2-soak-2h.json
```

`--final-gate`의 7,200초는 첫 post-grace strict 일치 이후부터 계산됩니다. 이 동안 OPC UA 연결이
한 번이라도 끊기면 즉시 실패하고 deadline에서 연결·heartbeat·37개 health 필드를 다시 검사합니다.
재연결 복구는 별도의 120초 gate에서 검증합니다.

Gateway 재시작 검증 시 shadow는 반드시 별도 컨테이너나 Monitoring PC에서 실행합니다. 강화된
70초 실기와 full 40초 reconnect grace 검증은 모든 connection epoch에서 strict 비교를 완료했고
divergence, timing miss와 alignment timeout 0으로 통과했습니다. 상세 상태는
`contracts/opcua/PHASE2_VALIDATION.md`에 있습니다.

Phase 4 M2부터 SO-101의 기본 계약은
`contracts/opcua/bi_so101_01.mapping.v2.yaml`입니다. 각 state/action feature가 selector,
순서, OPC UA `Float`, engineering unit, 유효 범위와 semantic을 독립적으로 선언하며 Gateway는
그 범위를 직접 적용합니다. Agent와 Gateway에는 반드시 같은 v2 파일을 전달해야 합니다. 정규화된
mapping SHA-256이 local-health의 manifest digest에 결합되므로 파일이 다르면 authoritative health를
fail-closed로 거부합니다. 기존 `bi_so101_01.mapping.yaml` v1은 NodeId가 같은 롤백 계약으로만
보존합니다.

2026-07-21 실기 검증에서 follower/leader 네 bus는 약 20 Hz, Innomaker 다섯 카메라는 각각
약 29.5 Hz로 동시에 관찰됐습니다. Adapter에는 motor register write, torque 변경, action 전송
API가 없습니다.

SO-101 manifest는 팔 최소 18 Hz, 카메라 최소 25 Hz를 요구합니다. Agent는 시작 직후 장치가
열리는 동안 false incident가 생기지 않도록 첫 heartbeat에 기본 3초 grace를 적용합니다.

## Soak와 재연결 검증

Agent와 Central이 `READY / 7 of 7 healthy`인 상태에서 2시간 strict soak를 실행합니다.
이 명령은 Central의 GET API만 읽으며 ROS topic이나 로봇 명령을 publish하지 않습니다.

```bash
ros2 run fleet_control_plane fleet_soak \
  --control-plane-url http://127.0.0.1:18091 \
  --agent-id workcell-soarm-01 \
  --robot-id bi_so101_01 \
  --duration-s 7200 \
  --interval-s 1 \
  --report /ws/output/so101-soak-2h.json
```

카메라 하나의 물리 재연결을 검증할 때는 아래 명령을 시작한 뒤 해당 카메라를 한 번 분리하고
다시 연결합니다. 최종 상태가 healthy이고 새 이벤트가 `topic_stale -> topic_recovered` 순서로
기록돼야 통과합니다.

```bash
ros2 run fleet_control_plane fleet_soak \
  --control-plane-url http://127.0.0.1:18091 \
  --agent-id workcell-soarm-01 \
  --robot-id bi_so101_01 \
  --duration-s 60 \
  --expect-reconnect-topic \
    /bi_so101/camera/right_wrist/color/image_rect_raw/compressed \
  --report /ws/output/right-wrist-reconnect.json
```

팔 serial/전원 재연결은 로봇을 고정하고 별도 안전 절차로 torque가 해제된 것이 확인된 경우에만
수행합니다. 현재 검증 도구는 torque 상태를 바꾸지 않습니다.

UI만 확인하려면 중앙 서버 시작 전에 다음 seed를 한 번 실행할 수 있습니다.

```bash
ros2 run fleet_control_plane fleet_seed --db-path /tmp/sang-fleet.db
```

## 테스트

```bash
PYTHONPATH="$PWD/src/sang_security:$PWD/src/fleet_control_plane:$PWD/src/robot_agent:$PWD/src/so101_readonly_adapter:$PWD/src/rb5_readonly_adapter:$PWD/src/sang_opcua_gateway:$PWD/sim:${PYTHONPATH:-}" \
/usr/bin/python3 -m pytest -p no:cacheprovider \
  src/sang_security/test src/fleet_control_plane/test src/robot_agent/test src/so101_readonly_adapter/test \
  src/rb5_readonly_adapter/test src/sang_opcua_gateway/test \
  sim/isaac_vendor/test_profile.py sim/isaac_vendor/test_scene_runner.py -q

colcon build --symlink-install
```

보안·분산 Phase 3 집중 테스트의 최신 명령과 결과는
`contracts/opcua/PHASE3_VALIDATION.md`를 단일 근거로 사용합니다. Isaac vendor profile 테스트는
컨테이너에 `/issacsim_setting_usd`를 mount한 환경에서 실행해야 합니다. 30초 실기 strict soak는
31개 sample, unhealthy 0,
fetch error 0으로 통과했고, 합성 `right_wrist` 4초 중단은 stale/recovery 이벤트와 최종 복구를
검증했습니다.

2026-07-22 실제 검증에서는 물리 `right_wrist` 카메라를 분리·재연결한 뒤 새 `/dev/videoN`으로
자동 복구했고, 이어진 2시간 strict soak가 7,189개 sample, unhealthy 0, fetch error 0,
stale 0, low-rate 0으로 통과했습니다. 팔의 최소 관측 주기는 약 19.98 Hz, 카메라는 약
29.39 Hz였습니다. 원본 JSON 보고서는 `output/validation/`에 보존합니다.

## 보안 경계

기본 Admin HTTP는 계속 같은 PC의 loopback만 허용합니다. Phase 3 원격 시험용 Agent mTLS ingress와
OPC UA `Basic256Sha256` `SignAndEncrypt`, 역할별 PKI, replay 방지, desired/observed profile 및
회전/폐기 경로는 로컬 분산 검증을 통과했으며
[Phase 3 security contract](contracts/opcua/PHASE3_SECURITY.md)를 따릅니다. 다른 노트북의 입회
soak/reconnect evidence 전에는 원격 경로를 완료 상태로 간주하지 않습니다.
Admin에는 robot command endpoint가 없으며, 향후에도 raw actuator 명령을 직접 소유하지 않습니다.

### Phase 4 Agent별 v3 canary

Phase 4의 원격 Agent는 기존 Phase 3 `roles/central`, `roles/edge`를 복사하지 않습니다. 중앙의 같은
인증기관 아래에서 Agent별 HTTPS 상호 TLS(mutual TLS, mTLS) 인증서를 발급하되, Edge에는 해당
Agent의 client key만 배포합니다. `deploy/phase4/agent.env.example`을 기준으로 환경을 정한 뒤 중앙
PC에서 등록하고 Agent를 시작합니다.

```bash
set -a
source runtime/phase4/config/sim-isaac-omy-01.env
set +a
scripts/enroll_phase4_agent.sh

docker compose \
  --project-name sang-phase4-omy-canary \
  --env-file runtime/phase4/config/sim-isaac-omy-01.env \
  -f docker/compose.phase4-agent.yaml up -d
```

이 Compose는 heartbeat schema v3를 명시적으로 사용하며 로봇 명령 publisher를 만들지 않습니다.
현재 검증 결과와 롤백 경계는
`docs/PHASE4_CENTRAL_FIRST_CANARY_2026-08-06.md`를 기준으로 합니다.

### Phase 4 Agent/Gateway 런타임 생성

M3부터 heartbeat Agent와 read-only OPC UA Gateway를 하나의 격리된 runtime으로 생성합니다.
인벤토리는 Agent/robot/Processing Node identity, endpoint, port, ROS domain, manifest, mapping과
runtime directory를 선언합니다. 생성기는 충돌과 계약 drift를 먼저 거부한 뒤 Agent별 HTTPS client,
OPC UA server, Central server pin, local-health HMAC, replay DB 경로, env와 deployment record를 만듭니다.

샘플의 `192.0.2.x`는 문서 전용 주소이므로 실제 배포 전에 Central과 Processing PC 주소로 바꿔야
합니다. 생성 명령 자체는 컨테이너나 로봇을 시작하지 않습니다.

```bash
export SANG_PKI_ROOT=/absolute/path/to/central-pki
export SANG_CENTRAL_DATA=/absolute/path/to/central-data
scripts/generate_phase4_runtimes.sh \
  deploy/phase4/runtime-inventory.so101-canary.yaml

docker compose \
  --project-name sang-phase4-so101 \
  --env-file runtime/phase4/config/workcell-soarm-01.env \
  -f docker/compose.phase4-runtime.yaml config --quiet
```

운영 절차, 출력 구조와 재실행/변경 제한은 [Phase 4 deployment guide](deploy/phase4/README.md)를
따릅니다. 기존 heartbeat-only `compose.phase4-agent.yaml`은 롤백 경계로 유지됩니다.

## Galaxy XR

XR는 LeRobot 기능이 아니라 현재 개발 중인 별도 ROS 2 입력 adapter입니다.

- 입력 bridge: [src/xr_teleop_bridge/README.md](src/xr_teleop_bridge/README.md)
- SO-ARM preview: [src/xr_teleop_retarget/README.md](src/xr_teleop_retarget/README.md)
- 계획: [PLAN_XR_TELEOP.md](PLAN_XR_TELEOP.md), [PLAN_XR_RETARGET.md](PLAN_XR_RETARGET.md)

XR가 실제 로봇 명령으로 연결될 때는 반드시 Control Mode Manager, command mux, TTL/deadman과
local safety gate를 통과해야 합니다.

## Unity Safety Shadow Phase 0

Unity 안전 판단의 첫 단계는 실제 장비 정지 없이 판정만 기록하는 Shadow Mode입니다. Processing
PC의 Unity가 사람-로봇 거리, 금지구역 점유, Task 순서와 입력 신선도를 `SafetySnapshotV1`로 만들고,
버전된 정책으로 `SAFE / WARNING / UNSAFE / UNKNOWN`을 판정합니다. 중앙은 최신 판정과 의미 있는
변화만 SQLite에 보관하며 고주파 원본과 영상은 기존 MCAP/Object Storage에 둡니다.

```bash
PYTHONPATH="$PWD/src/fleet_control_plane" \
python3 -m fleet_control_plane.safety_shadow evaluate \
  --snapshot contracts/safety/examples/safe-snapshot.json \
  --policy contracts/safety/examples/default-policy.json \
  --assessed-at 2026-08-27T12:00:00Z
```

계약, API, Unity 연결 전에 필요한 사용자 입력과 안전 경계는
[Unity Safety Shadow Phase 0](docs/UNITY_SAFETY_SHADOW_PHASE0.md)을 따릅니다. 이 단계의
`task_pause_candidate`는 추천 정보일 뿐 실제 Task Pause나 E-stop 명령이 아닙니다.

제공된 IITP Unity Scenario 1은
`contracts/tasks/iitp-scenario1-shadow.v1.json`의 재현 가능한 Task 번들로 등록합니다. 이 Run은
Unity 시뮬레이션 자산만 사용하는 `READY` Shadow 개발 Run이며 실물 Lab1 자산과 혼합하지 않습니다.

```bash
PYTHONPATH="$PWD/src/fleet_control_plane" \
python3 -m fleet_control_plane.task_bundle \
  --bundle contracts/tasks/iitp-scenario1-shadow.v1.json

export SANG_WORKSPACE="$PWD"
docker compose -p sang-unity-safety \
  -f docker/compose.unity-safety-shadow.yaml up -d

curl http://127.0.0.1:18092/healthz

UNITY_PROJECT="/home/sa08050/app developer/unity_IITP/unity_IITP/100.Whole_integration/Scenario1_app"
PYTHONPATH="$PWD/src/fleet_control_plane" \
python3 -m fleet_control_plane.unity_safety_validation \
  --bundle contracts/tasks/iitp-scenario1-shadow.v1.json \
  --unity-project "$UNITY_PROJECT"
```

Unity `6000.5.6f1` 프로젝트의 자동 Reporter는 로컬 Edge에 Snapshot만 보내고, Edge가 동일한 Python
참조 평가기로 Assessment를 생성해 Central에 전달합니다. `command_capability`는 항상 `false`이며
기존 OMY/RB5 Sim-to-Real UDP launcher는 이 경로에서 실행되지 않습니다. Unity 시작 환경에
`IITP_RB5_SIM_TO_REAL=1` 또는 `IITP_OMY_SIM_TO_REAL=1`이 있으면 Reporter도 송신을 거부합니다.

## Isaac execution + Unity observer pilot

The separate, output-disconnected RB5 pilot is documented in
[`docs/ISAAC_UNITY_TWIN_PILOT_2026-09-09.md`](docs/ISAAC_UNITY_TWIN_PILOT_2026-09-09.md).
Workflow Studio runs the published **Isaac RB5 + Unity digital twin** Task with
**Validate in Isaac**. Isaac owns the physics and two bounded joint actions;
Unity only mirrors measured ROS 2 feedback. This does not enable physical motion,
replace the OPC UA management plane, or complete the multi-robot Scenario 1.
Its dedicated runtime must be started explicitly; **Start Unity** on that page
still controls the legacy Scenario Player, not this pilot. Reset changes the
observation epoch and requires explicit observer relaunch.

The Scenario 1 extension adds carrier delivery, OMY table transfer, the 25-point
RB5 path, OMY return and carrier return. Use **Isaac Scenario 1 + Unity observer**
and explicitly select its Isaac adapter in Workflow Studio. It remains a virtual
fixture: carrier dynamics and grasp/table contacts are not physically qualified.
Implementation, acceptance evidence and limits are in
[`docs/ISAAC_SCENARIO1_EXECUTION_2026-09-09.md`](docs/ISAAC_SCENARIO1_EXECUTION_2026-09-09.md).

## OMY/RB5 Isaac Sim (existing observation profiles)

OMY는 ROBOTIS 공식 [`robotis_lab`](https://github.com/ROBOTIS-GIT/robotis_lab)의 OMY 자산,
준비 자세, actuator와 DDS topic 계약을 기준으로 합니다. RB5-850E는 제조사에서 제공한
전체 scene/URDF와 공식 [`rbpodo_ros2`](https://github.com/RainbowRobotics/rbpodo_ros2)의 6축
관절 이름을 기준으로 합니다. 원본 USD는 수정하지 않고
`sim/isaac_vendor/profiles.yaml`의 runtime profile로 차이를 격리합니다.

먼저 자산 해시와 계약을 Isaac Sim 없이 확인합니다.

```bash
cd "/home/sa08050/app developer/v2_ws"
PYTHONPATH="$PWD/sim" python3 -m isaac_vendor.validate
```

두 scene을 실제로 열고 0.1 rad 이내의 제한된 관절 운동을 검증합니다. 이 runner는 scene의
ROS ActionGraph를 비활성화하고 기본 `ROS_DOMAIN_ID=99`를 사용하며 실제 하드웨어 명령은
발행하지 않습니다. Lab에서 99를 이미 사용한다면
`SANG_ISAAC_ROS_DOMAIN_ID`로 시뮬레이션 전용 domain을 명시합니다.

```bash
scripts/run_vendor_isaac_scene.sh omy --exercise
scripts/run_vendor_isaac_scene.sh rb5 --exercise
```

관절과 RGB 카메라를 읽기 전용 ROS 2 topic으로 발행할 때는 실행 시간을 반드시 지정합니다.
기본 discovery는 같은 호스트로 제한하는 `LOCALHOST`, transport는 `UDPv4`이며, publisher는
command subscriber를 만들지 않습니다. 분산 discovery가 필요한 별도 검증에서만 `SUBNET`을
명시적으로 선택합니다.

```bash
scripts/run_vendor_isaac_scene.sh omy --publish-ros --run-seconds 60
scripts/run_vendor_isaac_scene.sh rb5 --publish-ros --run-seconds 60
```

Phase 4 OMY canary처럼 터미널 종료와 무관하게 bounded publisher를 유지할 때는 user systemd 관리
launcher를 사용합니다. 같은 profile의 중복 실행은 거부되며 기본 실행 시간은 2시간입니다.

```bash
set -a
source runtime/phase4/config/omy-isaac-observation.env
set +a

scripts/manage_isaac_observation.sh start omy
scripts/manage_isaac_observation.sh status omy
scripts/manage_isaac_observation.sh restart omy
scripts/manage_isaac_observation.sh stop omy
```

로그와 정상 만료 시 생성되는 최종 보고서는 `runtime/isaac_vendor/<profile>/`에 남습니다. 관리
launcher는 `--exercise`를 전달하지 않으며 ROS 2 observation publisher만 실행합니다. 프로세스
내부 실행시간에 5분을 더한 systemd `RuntimeMaxSec`도 함께 설정하므로, Isaac이 종료 단계에서
멈추더라도 무기한 남지 않습니다. 강제 상한이나 중간 `stop`으로 종료된 실행은 최종 보고서를
보장하지 않으며 로그가 근거입니다.

같은 `ROS_DOMAIN_ID=99`에서 `manifests/omy_sim_01.yaml` 또는
`manifests/rb5_850e_sim_01.yaml`로 Robot Agent를 실행하면 Admin에 simulation workcell이
등록됩니다. 시뮬레이터가 발행 중이면 `READY`, 종료되어 topic이 stale이면 `DEGRADED`가
정상입니다.

GUI로 scene을 볼 때는 `--gui`를 붙입니다. `--exercise`를 생략하면 준비 자세만 유지합니다.

```bash
scripts/run_vendor_isaac_scene.sh omy --gui
scripts/run_vendor_isaac_scene.sh rb5 --gui
```

결과는 `output/validation/isaac-<profile>-<UTC>.json`에 저장됩니다. 보고서에는 topic별 발행
개수/주기, JPEG 크기·해상도, 첫 frame 통계와 subscriber/hardware-command 0개 여부가 남습니다.
실제 마찰, 백래시, EEF 하중과 용접 접촉/열 물리는 실기 계측 후 별도 보정합니다.
RB5 원본 scene은 peg OBJ 두 개를 USD reference로 연결하고 TCP visual reference 하나를 잘못
지정해 로드 경고가 발생합니다. 6축 articulation과 D455 camera prim 검증에는 영향이 없으며,
원본을 수정하지 않고 후속 overlay에서 환경 visual만 보완할 예정입니다.
