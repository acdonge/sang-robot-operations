# PLAN - Vendor Isaac Sim integration

## Planner

### Goal

ROBOTIS와 Rainbow Robotics에서 제공한 자산 및 제어 계약을 기준으로 OMY F3M과
RB5-850E를 Isaac Sim에서 안전하게 실행하고, 이후 실제 로봇과 같은 ROS 2 관측 계약으로
확장할 수 있는 공통 시뮬레이션 기반을 만든다.

### Scope for this iteration

- 제조사 원본 USD는 수정하지 않는다.
- OMY는 ROBOTIS `robotis_lab`의 OMY 설정을 source of truth로 사용한다.
- RB5는 제공된 `rb5_850e_scene.usd`와 `rbpodo_ros2`의 관절 이름을 source of truth로 사용한다.
- 두 전체 장면을 headless/GUI로 열고, 하드웨어와 분리된 소폭 관절 운동을 검증한다.
- 검증 결과를 machine-readable JSON으로 남긴다.
- 실제 로봇 명령, 용접 공정 물리, 정책 학습, ROS 2 카메라 스트리밍은 이번 범위에서 제외한다.

### Tasks

1. 제조사 provenance와 USD prim/joint/camera 계약을 profile로 고정한다.
2. profile을 Isaac Sim 없이도 검증할 수 있는 CLI와 단위 테스트를 만든다.
3. 공통 Isaac runner로 장면을 열고 준비 자세와 제한된 joint exercise를 수행한다.
4. GUI/headless 실행 스크립트, sim manifest, 운영 문서를 추가한다.

### Acceptance Criteria

- OMY profile은 공식 ROBOTIS OMY USD의 SHA-256과 7개 제어 관절을 검증한다.
- RB5 profile은 제공 USD/URDF 기반 6개 `rbpodo_ros2` 관절을 검증한다.
- 두 장면이 Isaac Sim 5.1에서 headless로 열리고 articulation과 필수 관절이 발견된다.
- 각 로봇의 test joint가 제한된 범위에서 움직이고 오차가 JSON 보고서에 기록된다.
- GUI 실행 명령이 제공되며 기본 실행은 실제 ROS domain/하드웨어를 건드리지 않는다.
- 기존 관리자/Agent 테스트가 회귀 없이 통과한다.

### Risks / Notes

- 시뮬레이션은 인터페이스, 관절 구조, 제한, 카메라 배치를 맞출 수 있지만 실제 마찰,
  백래시, 용접 열/접촉 물리는 추후 실측 system identification이 필요하다.
- RB5 장면의 peg OBJ reference 경고는 로봇 articulation 검증을 막지 않는다. 원본을
  직접 고치지 않고 필요 시 별도 overlay layer에서 처리한다.
- 실제 actuator로 이어지는 ROS 2 command bridge는 Control Mode Manager, TTL/deadman,
  local safety gate가 준비되기 전까지 구현하지 않는다.
- AAS는 지금 구현하지 않지만 profile/manifest 식별자를 향후 AAS Submodel로 매핑할 수
  있도록 유지한다.

### Developer Instruction

`v2_ws` 안에 vendor-neutral profile/validation/runner를 최소 구현하고, 두 장면에서 실제
headless motion 검증을 수행한다. 기존 사용자 변경과 제조사 USD 원본은 수정하지 않는다.

## Milestones

- [x] M1: Vendor profile/provenance validator - verified by 5 profile tests and CLI hashes
- [x] M2: OMY headless scene/motion report - 0.1 rad motion, 0.0000105 rad error
- [x] M3: RB5 headless scene/motion report - 0.1 rad motion, 0.0001474 rad error
- [x] M4: GUI launch, sim manifests, and operating guide - manifests parsed and tests passed

## Decisions Log

- 2026-07-22: OMY는 ROBOTIS `robotis_lab`을 source of truth로 선택했다. 사용자 OMY USD가
  공식 자산과 SHA-256까지 동일하고 로컬 Isaac Lab commit도 공식 submodule과 일치한다.
- 2026-07-22: 공개된 Rainbow 전용 Isaac 패키지를 찾지 못해 제공받은 RB5 USD/URDF를
  vendor solution으로 사용하고, 실기 호환 계약은 공식 `rbpodo_ros2`를 따른다.
- 2026-07-22: 원본 scene의 ActionGraph/drive를 직접 편집하지 않고 runtime adapter로
  검증한다. 이 방식은 공급사 업데이트를 다시 받아도 변경점을 격리한다.
- 2026-07-22: 제조사 scene의 domain 30 ActionGraph는 비활성화한다. runner는 기본 domain
  99와 localhost discovery를 사용하고 첫 단계에서는 ROS bridge를 활성화하지 않는다.
- 2026-07-22: OMY full scene의 passive gripper joint 세 개에는 drive가 없으므로 원본 USD가
  아닌 runtime stage에 ROBOTIS 공식 implicit actuator 값을 적용한다.
- 2026-07-22: RB5 scene의 OBJ/TCP visual reference 오류는 profile의 known issue로 노출하고,
  articulation 및 카메라 prim 검증은 계속 수행한다.

## Current State

M1-M4 완료. Isaac Sim 5.1.0 / Isaac Lab v2.3.0에서 OMY와 RB5 전체 scene의 bounded
motion smoke가 통과했고 JSON 보고서를 `output/validation/`에 저장했다. 기존 Fleet 회귀 테스트
45개와 profile/manifest 테스트 11개도 통과했다. 다음 단계는 이 profile을 사용하는 ROS 2
observation bridge다. OMY는 `robotis_lab` 방식으로 cam_top/cam_wrist를 생성하고, RB5는 scene의
D455 RGB/Depth prim을 publish한다. 실제 command subscriber는 Control Mode Manager가 준비될
때까지 계속 비활성화한다.
