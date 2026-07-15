# V2 Recorder Workspace

LeRobot v0.6/v3 기반의 manifest-driven ROS 2 녹화 코어 워크스페이스입니다.

설계 근거: [V2_SYSTEM_DESIGN.md](../unified_recording_ui/V2_SYSTEM_DESIGN.md)

## 디렉토리 구조

```text
.
├── config/store.yaml               # 저장 루트의 단일 설정
├── docker/
│   ├── Dockerfile
│   └── run.sh
├── manifests/bi_so101_01.yaml      # bi_so101_v1 로봇 manifest
└── src/recorder_core/              # ROS 2 Jazzy ament_python 패키지
    ├── recorder_core/
    │   ├── fsm.py
    │   ├── manifest.py
    │   ├── validate_cli.py
    │   ├── writer.py
    │   └── recorder_node.py
    └── test/test_manifest.py
```

현재는 M4-③/④까지 구현되어 manifest-driven 30 Hz 샘플링, LeRobot v3 writer,
순수 FSM, 별도 프로세스 검증, staging→raw 원자적 승격과 source-drop abort를 포함합니다.
제어는 `~/record_start`, `~/record_stop`, `~/record_abort` Trigger 서비스를 사용합니다.

## 빌드

ROS 2 Jazzy 환경에서 실행합니다.

```bash
source /opt/ros/jazzy/setup.bash
colcon build --symlink-install
```

## 테스트

manifest 단위 테스트는 ROS 없이 실행할 수 있습니다.

```bash
cd src/recorder_core
python3 -m pytest test/ -q
```

전체 Python 파일의 문법 검사는 워크스페이스 루트에서 실행합니다.

```bash
python3 -m py_compile $(find . -type f -name '*.py')
```

Docker 예시는 `docker/run.sh`에 있습니다. 재현 가능한 이미지는 `LEROBOT_REF`에 검증한 정확한 commit
SHA를 지정해 빌드하고, workspace와 `config/store.yaml`의 `store_root`를 컨테이너에 마운트합니다.

M4-③/④ 전체 smoke는 LeRobot이 설치된 컨테이너 안에서 `scripts/smoke_m4_3.sh`로 실행합니다.

## M4-⑧ 학습 관리자

게이트웨이는 고정된 LeRobot v0.6의 공식 `lerobot-train` CLI를 별도 subprocess로 실행합니다.
학습 프로세스는 게이트웨이에 import되지 않으며, 동시에 하나의 ACT 학습만 허용됩니다.

- `GET /api/runs`: 현재 게이트웨이가 시작한 run 목록
- `POST /api/train`: `{"dataset_id":"...","policy_type":"act","steps":20}` → `{"run_id":"..."}`
- `POST /api/runs/{run_id}/stop`: SIGTERM 요청 후 유예 시간 내 종료되지 않으면 SIGKILL
- `/ws`: `{"type":"train","run_id":"...","state":"RUNNING","step":1,"loss":0.5}` 형식 이벤트

각 run은 설정의 store 아래 `runs/<dataset_id>__act__<UTC timestamp>/`에 생성됩니다.
`lineage.json`은 dataset 절대 경로와 `meta/` SHA-256, LeRobot SHA, 전체 CLI 명령을
학습 시작 전에 기록합니다. LeRobot 산출물은 `output/`, 통합 stdout/stderr는 `train.log`에
보존됩니다. subprocess가 0으로 끝나더라도 실제 checkpoint 파일이 없으면 상태는 `ERROR`입니다.

Resume은 M4-⑧ 범위에서 지원하지 않습니다. 기존 checkpoint의 config를 제자리 수정하거나
이력을 덮어쓰는 경로는 제공하지 않으며, 모든 요청은 새 run 디렉터리에서 시작합니다.

이미 빌드된 컨테이너 안에서 승격된 기존 dataset을 사용한 20-step CPU smoke는 다음과 같습니다.

```bash
scripts/smoke_m4_8.sh
```

## M4-⑨ 추론 관리자

`GET /api/policies`는 현재 gateway가 `DONE`으로 확인했고 최종 LeRobot checkpoint가 실제로
존재하는 run만 반환합니다. `POST /api/inference/start`는
`{"run_id":"...","robot_id":"bi_so101_01"}`을 받아 checkpoint의 정책 및 저장된
pre/post processor를 LeRobot v0.6 공개 API로 로드합니다. 로드 단계에서 정책의 전체 input/output
feature 키와 shape가 manifest의 state, channel-first camera, action 계약과 정확히 일치하지 않으면
409로 시작을 거부합니다. 녹화 FSM이 IDLE/READY가 아니거나 다른 추론이 실행 중이어도 409입니다.

실행 중에는 manifest FPS로 최신 JPEG 카메라를 RGB tensor로 변환하고 state와 함께
`policy.select_action`에 전달한 뒤, 복원된 action을 manifest의 leader `_norm`
`JointTrajectory` 토픽에 publish합니다. 관측 freshness gate는 추론 전후에 적용되며 stale이면
새 명령을 publish하지 않고 즉시 `ERROR`로 정지합니다. `POST /api/inference/stop`은 반복 호출해도
안전한 idempotent stop이고, `/ws`는 `type=inference`, `state`, `hz`,
`last_action_age_ms`를 계속 전송합니다.

> **안전 경고:** 이 executor는 `fake_robot`/simulation 전용입니다. 실제 로봇 bridge에 연결하면
> 안 됩니다. 실로봇 연결 전에는 M7 안전 gate와 bridge deadman이 반드시 먼저 구현·검증되어야
> 합니다. inference 시작 API 응답에도 같은 경고가 `warning` 필드로 포함됩니다.

임시 store에서 dataset 생성 → ACT 20-step 학습 → 정책 명령 확인 → 정지까지 수행하는 smoke는
LeRobot/ROS가 설치된 기존 container 안에서 실행합니다.

```bash
scripts/smoke_m4_9.sh
```
