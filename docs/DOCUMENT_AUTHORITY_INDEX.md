# SANG Document Authority Index

- Date: 2026-09-10
- Status: **Canonical documentation map**

## 1. Why this index exists

The repository grew through many verified implementation slices. A root `PLAN_*.md` usually records
how one slice was executed; a dated `docs/*RESULT*`, `*EVIDENCE*`, `*VALIDATION*`, or `*ACCEPTANCE*`
file records what was tested at that time. Neither should silently become the current whole-product
roadmap.

Use this index to find the right authority and avoid combining incompatible phase numbers.

## 2. Authority classes

| Class | Responsibility | Rule |
|---|---|---|
| A0 | Machine-readable contracts, migrations and tests | Exact schema/wire/persistence behavior; changes require versioning and tests |
| A1 | Mandatory requirements and safety boundaries | `MUST`/`MUST NOT` product constraints; cannot be weakened by a plan or UI |
| A2 | Master implementation plan | Current status, sequence, priority, completion and deferral |
| A3 | Subsystem design | Detailed architecture within A1/A2 scope |
| A4 | Runbook and component contract | How to deploy or operate a named release |
| A5 | Dated evidence | Proof for the named build/topology/time only; preserve rather than rewrite |
| A6 | Historical execution plan | Planning/decision trace; not current status unless A2 explicitly adopts it |

If documents conflict, compare their responsibility rather than their age. Safety requirements and
machine-readable invariants cannot be overridden by the roadmap. For current status and next work,
the master plan wins over every old phase plan.

## 3. Canonical core

| Document | Class | Authority |
|---|---|---|
| [`../SANG_MASTER_IMPLEMENTATION_PLAN.md`](../SANG_MASTER_IMPLEMENTATION_PLAN.md) | A2 | Sole whole-program current roadmap |
| [`requirements.md`](requirements.md) | A1 | Mandatory functional, security, data and safety requirements |
| [`../README.md`](../README.md) | A4 | Workspace entry point and current operating instructions |
| [`DOCUMENT_AUTHORITY_INDEX.md`](DOCUMENT_AUTHORITY_INDEX.md) | A2 support | Document classification and precedence |
| [`SANG_PLATFORM_OVERVIEW_KO.md`](SANG_PLATFORM_OVERVIEW_KO.md) | A4 onboarding | Shared product concept, current implementation snapshot, terminology and source map; not a second roadmap |
| [`GITHUB_SOURCE_POLICY.md`](GITHUB_SOURCE_POLICY.md) | A4 publication | Source/data separation, external assets, exclusions and reproducibility boundary |

The task-local [`../PLAN_DOCUMENT_CONSOLIDATION.md`](../PLAN_DOCUMENT_CONSOLIDATION.md) is the execution
record for creating this hierarchy. It becomes A6 after its final gate.

## 4. Normative subsystem designs

| Document | Class | Current use |
|---|---|---|
| [`OPCUA_ROBOTICS_AAS_ARCHITECTURE_AUDIT_2026-09-01.md`](OPCUA_ROBOTICS_AAS_ARCHITECTURE_AUDIT_2026-09-01.md) | A3/ADR | Official OPC UA Robotics, Machine Vision and AAS alignment; source for Waves 1-3 and AAS policy |
| [`MULTI_ROBOT_ORCHESTRATOR_IMPLEMENTATION_DESIGN.md`](MULTI_ROBOT_ORCHESTRATOR_IMPLEMENTATION_DESIGN.md) | A3 | Generic control, lease, recovery, safety and G0-G9 contract; G1-G6a software is implemented |
| [`AAS_ASSET_PACKAGE_DYNAMIC_UNITY_ADAPTER_DESIGN.md`](AAS_ASSET_PACKAGE_DYNAMIC_UNITY_ADAPTER_DESIGN.md) | A3 | A0-A6 target; bounded A1/A2 runtime exists, release/second-scene/A4 qualification remains open |
| [`WORKFLOW_STUDIO_V1_DESIGN.md`](WORKFLOW_STUDIO_V1_DESIGN.md) | A3 | Immutable Task/Workflow authoring, cloning, publishing and simulation queue contract |
| [`PARAMETERIZED_SKILL_EDITOR_V1_DESIGN.md`](PARAMETERIZED_SKILL_EDITOR_V1_DESIGN.md) | A3 | Versioned Skill/input contract retained beneath the simpler Action UI |
| [`ROBOT_CAMERA_OPERATIONS_V1.md`](ROBOT_CAMERA_OPERATIONS_V1.md) | A3/A4 | Accepted observation, recording, storage, backup and read-only release contract |
| [`LEROBOT_DERIVATION_CONTRACT.md`](LEROBOT_DERIVATION_CONTRACT.md) | A3/A4 | Observation-only LeRobot v3 derivation contract |
| [`RB5_WELD_CELL_HARDWARE_CONTRACT.md`](RB5_WELD_CELL_HARDWARE_CONTRACT.md) | A3 | Future hardware boundary only; does not authorize process I/O |
| [`UNITY_SAFETY_SHADOW_PHASE0.md`](UNITY_SAFETY_SHADOW_PHASE0.md) | A3 | Command-free Unity safety evidence semantics |
| [`UI_DESIGN_STANDARD.md`](UI_DESIGN_STANDARD.md) | A3 | Ant Design visual and interaction standard for new first-party pages |
| [`../PLAN_TASK_SCOPED_OPERATIONS.md`](../PLAN_TASK_SCOPED_OPERATIONS.md) | A6 with retained A3 detail | Mixed-era architecture history; master plan overrides status and sequence |

## 5. Machine-readable and component contracts

The following directories outrank prose for exact payload and schema behavior:

- `contracts/opcua/`: frozen SANG OPC UA model, mapping, security and phase contracts.
- `contracts/orchestration/`: workflow, binding, grant, lease, policy, mode and fixture contracts.
- `contracts/safety/`: command-free and Safety Evidence contracts.
- `contracts/assets/`: A0 internal asset/package/binding/qualification schemas and fixtures.
- `contracts/aas/`: pinned official IDTA 3.1.2 JSON metamodel and provenance lock.
- `contracts/tasks/`: immutable Task bundle contracts.
- `manifests/`: robot/camera source, rate, unit and runtime declarations.
- `config/`: deployment profiles and capture/runtime configuration.
- `src/*/test/`: executable invariants and regression coverage.
- `src/fleet_control_plane/fleet_control_plane/commissioning_evidence.py`: versioned G6b evidence,
  review, persistence, expiry, and permanently non-authorizing report contract.

Passing tests do not establish physical safety or standards certification unless the relevant dated
site/conformance evidence explicitly says so.

## 6. Dated evidence and validation

These records are A5. Keep their original verdict and limitations even when later work supersedes the
deployment.

### 6.1 Robot, camera, recording and data

- [`ROBOT_CAMERA_OPERATIONS_V1_VALIDATION_2026-08-21.md`](ROBOT_CAMERA_OPERATIONS_V1_VALIDATION_2026-08-21.md)
- [`ROBOT_CAMERA_V1_SITE_ACCEPTANCE_2026-08-26.md`](ROBOT_CAMERA_V1_SITE_ACCEPTANCE_2026-08-26.md)
- [`RB5_LAB1_MCAP_RESULT_2026-08-06.md`](RB5_LAB1_MCAP_RESULT_2026-08-06.md)
- [`RECORDING_INVENTORY_RESULT_2026-08-06.md`](RECORDING_INVENTORY_RESULT_2026-08-06.md)
- [`RB5_LAB1_SYNC_RESULT_2026-08-05.md`](RB5_LAB1_SYNC_RESULT_2026-08-05.md)
- [`RB5_LAB1_SYNC_HARDENING_RESULT_2026-08-06.md`](RB5_LAB1_SYNC_HARDENING_RESULT_2026-08-06.md)
- [`RB5_DUAL_REALSENSE_SYNC_RESULT_2026-08-06.md`](RB5_DUAL_REALSENSE_SYNC_RESULT_2026-08-06.md)
- [`RB5_DUAL_REALSENSE_RECORDING_RESULT_2026-08-23.md`](RB5_DUAL_REALSENSE_RECORDING_RESULT_2026-08-23.md)
- [`RB5_LAB1_ATTENDED_SYNC_TEST.md`](RB5_LAB1_ATTENDED_SYNC_TEST.md)
- [`RB5_LAB1_SECURE_PREVIEW_RESULT_2026-08-23.md`](RB5_LAB1_SECURE_PREVIEW_RESULT_2026-08-23.md)
- [`RB5_FOUR_SOURCE_STORAGE_SOAK_RESULT_2026-08-23.md`](RB5_FOUR_SOURCE_STORAGE_SOAK_RESULT_2026-08-23.md)
- [`AUTOMATIC_EDGE_RECORDING_UPLOAD_RESULT_2026-08-24.md`](AUTOMATIC_EDGE_RECORDING_UPLOAD_RESULT_2026-08-24.md)
- [`RB5_FOUR_SOURCE_AUTOMATIC_UPLOAD_1H_RESULT_2026-08-24.md`](RB5_FOUR_SOURCE_AUTOMATIC_UPLOAD_1H_RESULT_2026-08-24.md)
- [`RECORDING_QUALITY_REMOTE_MANAGER_ACCEPTANCE_2026-08-24.md`](RECORDING_QUALITY_REMOTE_MANAGER_ACCEPTANCE_2026-08-24.md)
- [`TASK_RUN_UI_CAPTURE_ACCEPTANCE_2026-08-24.md`](TASK_RUN_UI_CAPTURE_ACCEPTANCE_2026-08-24.md)
- [`MCAP_WEB_PLAYBACK_ACCEPTANCE_2026-08-24.md`](MCAP_WEB_PLAYBACK_ACCEPTANCE_2026-08-24.md)
- [`LEROBOT_DERIVATION_ACCEPTANCE_2026-08-26.md`](LEROBOT_DERIVATION_ACCEPTANCE_2026-08-26.md)

### 6.2 Multi-edge and OPC UA rollout history

- [`PHASE4_CENTRAL_FIRST_CANARY_2026-08-06.md`](PHASE4_CENTRAL_FIRST_CANARY_2026-08-06.md)
- [`PHASE4_M1_RUNTIME_IDENTITY_2026-08-06.md`](PHASE4_M1_RUNTIME_IDENTITY_2026-08-06.md)
- [`PHASE4_M2_MAPPING_SCHEMA_V2_2026-08-07.md`](PHASE4_M2_MAPPING_SCHEMA_V2_2026-08-07.md)
- [`PHASE4_M3_RUNTIME_GENERATION_2026-08-08.md`](PHASE4_M3_RUNTIME_GENERATION_2026-08-08.md)
- [`PHASE4_M4_MULTI_ROBOT_MAPPING_V2_2026-08-21.md`](PHASE4_M4_MULTI_ROBOT_MAPPING_V2_2026-08-21.md)
- [`PHASE4_M4_LIVE_ROLLOUT_2026-08-21.md`](PHASE4_M4_LIVE_ROLLOUT_2026-08-21.md)
- [`PHASE4_OMY_ISAAC_LIVE_OBSERVATION_2026-08-07.md`](PHASE4_OMY_ISAAC_LIVE_OBSERVATION_2026-08-07.md)
- [`OPCUA_ROBOTICS_AAS_ARCHITECTURE_AUDIT_2026-09-01.md`](OPCUA_ROBOTICS_AAS_ARCHITECTURE_AUDIT_2026-09-01.md), an ADR rather than runtime acceptance
- [`OPCUA_ROBOTICS_AAS_G6_M1_M5_EVIDENCE_2026-09-01.md`](OPCUA_ROBOTICS_AAS_G6_M1_M5_EVIDENCE_2026-09-01.md), software evidence only; physical G6b remains unverified

These records validate custom SANG read-only OPC UA behavior. They do not prove OPC UA Robotics 1.02
conformance.

### 6.3 Orchestration, Unity, Workflow and Actions

- [`G1_G6_IMPLEMENTATION_STATUS_2026-08-28.md`](G1_G6_IMPLEMENTATION_STATUS_2026-08-28.md)
- [`UNITY_SAFETY_SHADOW_PHASE0_VALIDATION_2026-08-27.md`](UNITY_SAFETY_SHADOW_PHASE0_VALIDATION_2026-08-27.md)
- [`UNITY_IITP_SCENARIO1_PHASE1_2026-08-27.md`](UNITY_IITP_SCENARIO1_PHASE1_2026-08-27.md)
- [`UNITY_SCENARIO1_ORCHESTRATED_RUN_2026-08-28.md`](UNITY_SCENARIO1_ORCHESTRATED_RUN_2026-08-28.md)
- [`WORKFLOW_STUDIO_UNITY_LIVE_RESULT_2026-08-28.md`](WORKFLOW_STUDIO_UNITY_LIVE_RESULT_2026-08-28.md)
- [`PARAMETERIZED_SKILL_EDITOR_V1_EVIDENCE.md`](PARAMETERIZED_SKILL_EDITOR_V1_EVIDENCE.md)
- [`SIMPLE_ROBOT_ACTION_LIBRARY_V1_EVIDENCE_2026-08-31.md`](SIMPLE_ROBOT_ACTION_LIBRARY_V1_EVIDENCE_2026-08-31.md)
- [`ACTION_LIBRARY_AUTHORING_V1_EVIDENCE_2026-08-31.md`](ACTION_LIBRARY_AUTHORING_V1_EVIDENCE_2026-08-31.md)
- [`UNITY_EXTERNAL_WORKFLOW_UI_ONLY_EVIDENCE_2026-08-31.md`](UNITY_EXTERNAL_WORKFLOW_UI_ONLY_EVIDENCE_2026-08-31.md)
- [`SIMULATION_OPERATIONS_V1_EVIDENCE.md`](SIMULATION_OPERATIONS_V1_EVIDENCE.md)
- [`SIMULATION_VALIDATION_AND_PROMOTION_V1_EVIDENCE_2026-08-31.md`](SIMULATION_VALIDATION_AND_PROMOTION_V1_EVIDENCE_2026-08-31.md)
- [`AAS_ASSET_PACKAGE_A0_EVIDENCE_2026-08-31.md`](AAS_ASSET_PACKAGE_A0_EVIDENCE_2026-08-31.md)
- [`UNITY_DYNAMIC_BINDING_A1_A2_2026-09-09.md`](UNITY_DYNAMIC_BINDING_A1_A2_2026-09-09.md)
- [`A1_UNITY_INVENTORY_2026-09-09.md`](A1_UNITY_INVENTORY_2026-09-09.md)
- [`ISAAC_UNITY_TWIN_PILOT_2026-09-09.md`](ISAAC_UNITY_TWIN_PILOT_2026-09-09.md), bounded Isaac executor / ROS feedback / Unity observer; not full Scenario 1 or physical commissioning
- [`ISAAC_RB5_OBSERVER_BUILD_2026-09-09.md`](ISAAC_RB5_OBSERVER_BUILD_2026-09-09.md), separate observer-only build and reset/rebind evidence
- [`ISAAC_SCENARIO1_EXECUTION_2026-09-09.md`](ISAAC_SCENARIO1_EXECUTION_2026-09-09.md), fixed five-stage Isaac fixture; not physical grasp, mobile dynamics or collision-safety qualification
- [`ISAAC_SCENARIO1_OBSERVER_2026-09-09.md`](ISAAC_SCENARIO1_OBSERVER_2026-09-09.md), passive multi-asset Unity build and source geometry inventory
- [`SO101_OPCUA_IDENTITY_2026-09-09.md`](SO101_OPCUA_IDENTITY_2026-09-09.md), diagnosis only; no live repair or certificate bypass

No file in this group authorizes a physical command sink.

### 6.4 Documentation and architecture validation

- [`DOCUMENT_CONSOLIDATION_VALIDATION_2026-09-01.md`](DOCUMENT_CONSOLIDATION_VALIDATION_2026-09-01.md)

This record validates document coverage and consistency only. It is not a runtime or site acceptance.

## 7. Historical root plans

All entries below are A6. Checked milestones show that the named slice finished; they do not make its
old "next step" the current program priority.

### 7.1 Platform, UI and operations

- [`../PLAN_ADMIN_REBASE.md`](../PLAN_ADMIN_REBASE.md) - complete historical rebase.
- [`../PLAN_ADMIN_CONTINUITY_AND_ROBOT_CONFORMANCE.md`](../PLAN_ADMIN_CONTINUITY_AND_ROBOT_CONFORMANCE.md) - complete.
- [`../PLAN_ANTD_UI_MIGRATION.md`](../PLAN_ANTD_UI_MIGRATION.md) - complete UI migration record.
- [`../PLAN_HEALTH_EVENTS.md`](../PLAN_HEALTH_EVENTS.md) - complete.
- [`../PLAN_TASK_SCOPED_OPERATIONS.md`](../PLAN_TASK_SCOPED_OPERATIONS.md) - superseded as roadmap; retained architecture history.

### 7.2 Robot, camera, simulation observation and multi-edge

- [`../PLAN_SO101_OBSERVABILITY.md`](../PLAN_SO101_OBSERVABILITY.md) - complete read-only slice.
- [`../PLAN_OMY_RUNTIME_HARDENING.md`](../PLAN_OMY_RUNTIME_HARDENING.md) - complete simulation hardening.
- [`../PLAN_ISAAC_VENDOR_SIM.md`](../PLAN_ISAAC_VENDOR_SIM.md) - complete vendor-scene validation slice.
- [`../PLAN_ISAAC_ROS_OBSERVATION.md`](../PLAN_ISAAC_ROS_OBSERVATION.md) - complete observation bridge slice.
- [`../PLAN_PHASE4_M3.md`](../PLAN_PHASE4_M3.md) - complete runtime generation slice.
- [`../PLAN_PHASE4_M4_LIVE_ROLLOUT.md`](../PLAN_PHASE4_M4_LIVE_ROLLOUT.md) - complete named rollout slice.
- [`../PLAN_ROBOT_CAMERA_V1_COMPLETION.md`](../PLAN_ROBOT_CAMERA_V1_COMPLETION.md) - complete software release.
- [`../PLAN_ROBOT_CAMERA_V1_SITE_ACCEPTANCE.md`](../PLAN_ROBOT_CAMERA_V1_SITE_ACCEPTANCE.md) - complete named site acceptance.

### 7.3 Recording, synchronization, storage and learning data

- [`../PLAN_MCAP_STORAGE_MVP.md`](../PLAN_MCAP_STORAGE_MVP.md) - complete.
- [`../PLAN_RECORDING_INVENTORY.md`](../PLAN_RECORDING_INVENTORY.md) - complete.
- [`../PLAN_SYNC_HARDENING.md`](../PLAN_SYNC_HARDENING.md) - interrupted/superseded OTOS gate; do not mark PASS retroactively.
- [`../PLAN_DUAL_REALSENSE_SYNC.md`](../PLAN_DUAL_REALSENSE_SYNC.md) - complete replacement test slice.
- [`../PLAN_LAB1_CAPTURE_UPLOAD_SOAK.md`](../PLAN_LAB1_CAPTURE_UPLOAD_SOAK.md) - complete named Lab1 slice.
- [`../PLAN_LAB1_SECURE_PREVIEW.md`](../PLAN_LAB1_SECURE_PREVIEW.md) - complete.
- [`../PLAN_EDGE_AUTOMATIC_RECORDING_UPLOAD.md`](../PLAN_EDGE_AUTOMATIC_RECORDING_UPLOAD.md) - complete.
- [`../PLAN_RECORDING_QUALITY_REMOTE_MANAGER.md`](../PLAN_RECORDING_QUALITY_REMOTE_MANAGER.md) - complete.
- [`../PLAN_TASK_RUN_CAPTURE_JOBS.md`](../PLAN_TASK_RUN_CAPTURE_JOBS.md) - complete.
- [`../PLAN_MCAP_WEB_PLAYBACK.md`](../PLAN_MCAP_WEB_PLAYBACK.md) - complete.
- [`../PLAN_LEROBOT_DERIVATION.md`](../PLAN_LEROBOT_DERIVATION.md) - complete observation-only derivation.

### 7.4 Orchestration, Workflow, Unity and Action authoring

- [`../PLAN_MULTI_ROBOT_ORCHESTRATOR_DESIGN.md`](../PLAN_MULTI_ROBOT_ORCHESTRATOR_DESIGN.md) - design review complete.
- [`../PLAN_G0_ORCHESTRATION_CONTRACTS.md`](../PLAN_G0_ORCHESTRATION_CONTRACTS.md) - complete contract freeze.
- [`../PLAN_G1_G6_ORCHESTRATION_RUNTIME.md`](../PLAN_G1_G6_ORCHESTRATION_RUNTIME.md) - G1-G6a software complete; physical gates open.
- [`../PLAN_UNITY_SAFETY_SHADOW.md`](../PLAN_UNITY_SAFETY_SHADOW.md) - complete command-free phase.
- [`../PLAN_UNITY_IITP_SCENARIO1_INTEGRATION.md`](../PLAN_UNITY_IITP_SCENARIO1_INTEGRATION.md) - complete named integration slice.
- [`../PLAN_UNITY_SCENARIO1_ORCHESTRATED_RUN.md`](../PLAN_UNITY_SCENARIO1_ORCHESTRATED_RUN.md) - complete simulation run.
- [`../PLAN_WORKFLOW_STUDIO_V1.md`](../PLAN_WORKFLOW_STUDIO_V1.md) - complete authoring slice.
- [`../PLAN_WORKFLOW_STUDIO_UNITY_LIVE.md`](../PLAN_WORKFLOW_STUDIO_UNITY_LIVE.md) - complete live simulation slice.
- [`../PLAN_PARAMETERIZED_SKILLS_V1.md`](../PLAN_PARAMETERIZED_SKILLS_V1.md) - complete; retained beneath Action abstraction.
- [`../PLAN_SIMPLE_ROBOT_ACTION_LIBRARY_V1.md`](../PLAN_SIMPLE_ROBOT_ACTION_LIBRARY_V1.md) - complete.
- [`../PLAN_ACTION_LIBRARY_AUTHORING_V1.md`](../PLAN_ACTION_LIBRARY_AUTHORING_V1.md) - complete.
- [`../PLAN_UNITY_EXTERNAL_WORKFLOW_UI.md`](../PLAN_UNITY_EXTERNAL_WORKFLOW_UI.md) - complete UI-only control slice.
- [`../PLAN_SIMULATION_OPERATIONS_V1.md`](../PLAN_SIMULATION_OPERATIONS_V1.md) - complete lifecycle slice.
- [`../PLAN_SIMULATION_VALIDATION_AND_PROMOTION_V1.md`](../PLAN_SIMULATION_VALIDATION_AND_PROMOTION_V1.md) - complete non-actuating promotion slice.
- [`../PLAN_AAS_ASSET_PACKAGE_A0.md`](../PLAN_AAS_ASSET_PACKAGE_A0.md) - A0 contract complete; A1+ open.

### 7.5 XR development

- [`../PLAN_XR_TELEOP.md`](../PLAN_XR_TELEOP.md) - wired Galaxy XR input milestone complete; no robot command.
- [`../PLAN_XR_RETARGET.md`](../PLAN_XR_RETARGET.md) - simulation retarget preview complete for the stated milestone.
- [`../PLAN_XR_OMY.md`](../PLAN_XR_OMY.md) - development/handoff design; no physical acceptance claim.

### 7.6 Standards review

- [`../PLAN_OPCUA_ROBOTICS_AAS_ARCHITECTURE_AUDIT.md`](../PLAN_OPCUA_ROBOTICS_AAS_ARCHITECTURE_AUDIT.md) - audit complete; its ADR feeds the master roadmap.
- [`../PLAN_OPCUA_ROBOTICS_AAS_G6_IMPLEMENTATION.md`](../PLAN_OPCUA_ROBOTICS_AAS_G6_IMPLEMENTATION.md) - M1-M5 software complete; M0 live deployment and M6 close-out remain.

## 8. Visual architecture assets

- [`SANG_CURRENT_AND_FUTURE_ARCHITECTURE_KO.svg`](SANG_CURRENT_AND_FUTURE_ARCHITECTURE_KO.svg)
- [`SANG_CURRENT_AND_FUTURE_ARCHITECTURE_KO.png`](SANG_CURRENT_AND_FUTURE_ARCHITECTURE_KO.png)

Visuals explain topology but never override machine-readable contracts or the master status table.

## 9. Rules for future documents

1. A new implementation slice gets one temporary root `PLAN_<SLICE>.md` with observable done criteria.
2. Its completion gets one dated evidence file naming version, topology, test result and limitations.
3. Update the status and next priority only in the master plan.
4. Add the new design/evidence/plan to this index.
5. Do not rewrite old evidence after later changes; add a superseding record and link it.
6. Do not create another whole-program roadmap.
