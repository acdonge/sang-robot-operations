# SANG

**로봇 관측, 작업 관리, 데이터 수집과 디지털 트윈을 연결하는 운영 플랫폼**

SO-101·OMY·RB5와 카메라를 작업 단위로 묶어 관리합니다. ROS 2 관측값을 수집하고,
선택한 구간을 MCAP으로 보존하며, 웹에서 상태·녹화·시뮬레이션 결과를 확인합니다.
OPC UA는 읽기 전용 정보 연계를, AAS는 자산 문서 연결을 담당합니다.

[플랫폼 소개](docs/SANG_PLATFORM_OVERVIEW_KO.md) ·
[설치·운영](docs/ROBOT_CAMERA_OPERATIONS_V1.md) ·
[상세 사용법](docs/USAGE.md) ·
[요구사항](docs/requirements.md)

## 시스템 구조

![SANG 구조도: 로봇·카메라에서 Edge와 Central을 거쳐 사용자 화면으로 이어지는 관측·데이터 경로와 별도의 시뮬레이션 경로](docs/assets/sang-system-overview.svg?v=2)

1. **Workcell** — 로봇 관절과 카메라 영상을 ROS 2 어댑터로 관측합니다.
2. **Edge** — Robot Agent가 상태를 보고하고, MCAP Recorder가 지정 구간을 녹화합니다.
3. **Central** — 자산·작업·권한·메타데이터는 SQLite에, 검증된 녹화와 파생 객체는 Object Store에 보존합니다.
4. **Interfaces** — Admin·Operator에서 조회·허용된 작업 관리를 수행하고, OPC UA Client로 정보를 연계합니다.

시뮬레이션은 별도 Worker가 검증된 고정 프로필을 실행합니다. Unity 실행 프로필과
Isaac 실행 + Unity 관측 프로필은 실물 관측 경로와 분리되어 있습니다.

## 주요 기능

| 기능 | 제공하는 것 |
|---|---|
| **Fleet & Task** | 자산·Processing PC·Workcell 등록, 작업 배정, Agent 상태와 이벤트 관리 |
| **Camera & Recording** | 선택 카메라 프리뷰, Task Run 구간 녹화, 무결성 검증 업로드, 다중 뷰 재생 |
| **OPC UA & AAS** | 읽기 전용 Edge·Central 정보 투영, 고정 NodeSet·매핑 계약, 자산 문서 연결 |
| **Workflow & Simulation** | 버전 고정 Action·Workflow, 출력이 분리된 Unity·Isaac 시뮬레이션 실행 |
| **Data Export** | 품질 검증된 MCAP에서 관절 상태와 카메라를 LeRobot v3 데이터셋으로 파생 |

**현재 범위:** 실물 경로는 관측·녹화용입니다. LeRobot 파생 데이터에는 `action` feature가
없으며, VLA 학습·정책 실행과 실물 로봇·용접 장비 구동은 이 저장소의 제공 기능에 포함되지 않습니다.
OPC UA 표준 적합성 인증을 주장하지 않습니다.

## 빠른 시작

먼저 소스를 내려받고 웹 화면을 빌드합니다. Docker와 Git, 프로젝트 의존성이 지원하는
Node.js/npm 환경이 필요합니다.

```bash
git clone https://github.com/acdonge/sang-robot-operations.git
cd sang-robot-operations

mkdir -p ui-admin ui-operator
cd frontend/admin
npm ci
npm run build
cd ../..
```

ROS 2 Jazzy 개발 컨테이너를 열고 워크스페이스를 빌드합니다.

```bash
docker/run.sh
```

컨테이너 안에서:

```bash
source /opt/ros/jazzy/setup.bash
colcon build --symlink-install
source install/setup.bash
```

이후 [최초 설치와 관리자 계정 설정](docs/ROBOT_CAMERA_OPERATIONS_V1.md#first-local-installation)을
따라 Central을 시작합니다. 장비 연결·시뮬레이터 실행·원격 배포는 사용 환경별 설정이 필요합니다.
Unity·Isaac 런타임과 외부 모델·장면은 별도로 준비하며, 자세한 의존성은
[소스 게시 정책](docs/GITHUB_SOURCE_POLICY.md#reproduction-requirements)을 확인하세요.

## 저장소 구성

```text
src/
├── fleet_control_plane/    # 중앙 API · 자산/작업 · 데이터 · 시뮬레이션 Worker
├── robot_agent/            # ROS 2 토픽 상태 · heartbeat
├── so101_readonly_adapter/ # SO-101 관측
├── rb5_readonly_adapter/   # RB5 관측
├── sang_mcap_recorder/     # 허용된 토픽의 MCAP 녹화
├── sang_opcua_gateway/     # 읽기 전용 OPC UA
├── sang_security/          # 인증서 · TLS · 보안
└── xr_*/                   # XR 입력 · target preview
frontend/admin/            # React + TypeScript + Ant Design
contracts/                 # 버전 고정 스키마 · 매핑 · 실행 계약
manifests/ · config/        # 장비 · 토픽 · 카메라 프로필
sim/ · unity/              # 격리된 시뮬레이션 · 관측 화면
docker/ · deploy/          # 개발 환경 · 배포 구성
scripts/                   # 실행 · 검증 · 운영 도구
docs/                      # 공개 소개 · 사용 안내 · 구조도
```

## 문서 찾아보기

| 목적 | 문서 |
|---|---|
| 전체 개념과 데이터 흐름 이해 | [플랫폼 소개](docs/SANG_PLATFORM_OVERVIEW_KO.md) |
| 설치·원격 배포·백업·업그레이드 | [운영 가이드](docs/ROBOT_CAMERA_OPERATIONS_V1.md) |
| 장비 관측·녹화·시뮬레이션 명령 | [상세 사용 안내](docs/USAGE.md) |
| 기능과 권한·보안 경계 확인 | [요구사항](docs/requirements.md) |
| OPC UA 매핑·보안 설정 | [OPC UA 계약](contracts/opcua/README.md) |
| Action·Workflow 인터페이스 | [오케스트레이션 계약](contracts/orchestration/README.md) |
| LeRobot 파생 데이터 형식 | [데이터 계약](docs/LEROBOT_DERIVATION_CONTRACT.md) |
| 포함된 소스와 외부 의존성 | [소스 게시 정책](docs/GITHUB_SOURCE_POLICY.md) |

이 저장소에는 공개용 코드와 사용 문서를 보관합니다. 내부 계획과 작업 기록은 별도 비공개 저장소에서 관리합니다.
