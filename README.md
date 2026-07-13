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
    │   ├── manifest.py
    │   └── recorder_node.py
    └── test/test_manifest.py
```

현재 범위는 **M4-① 골격**이며 manifest 로드·검증과 ROS 노드 진입점만 포함합니다. FSM은 상태 enum만
정의되어 있고 FSM 동작과 writer는 아직 구현하지 않았습니다.

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

