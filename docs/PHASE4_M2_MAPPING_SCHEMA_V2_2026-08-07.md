# Phase 4 M2 Mapping Schema v2 Result

Date: 2026-08-07
Scope: SO-101 read-only OPC UA mapping foundation
Result: PASS

## Implemented

- Added `bi_so101_01.mapping.v2.yaml`; the original v1 mapping remains a rollback contract.
- Declared all 12 state and 12 observed-action features independently with source/topic context,
  joint-name selector, dense order, OPC UA `Float`, engineering unit, valid range and semantic.
- Preserved the existing SANG namespace, ApplicationUri rule and every SO-101 NodeId.
- Replaced name-based gripper range inference with the selected feature's file-declared range.
- Added strict manifest coverage, engineering-unit registry, selector, order, type, finite-range and
  semantic validation. Unknown schema or feature-contract fields fail closed.
- Added canonical mapping SHA-256 binding to the Agent/Gateway local-health manifest digest. A v2
  Gateway rejects an Agent snapshot built without the same mapping metadata.
- Updated the secure Phase 3 Edge deployment so Agent and Gateway receive the same v2 mapping file.

No writable OPC UA Variable, Method, camera byte projection or robot command path was added. OMY and
RB5 limits were not inferred from URDF or current samples.

## Verification

- Direct v1 and v2 contract loads passed; v1 reports no mapping digest and v2 reports the same digest
  from both the Agent helper and Gateway validator.
- Focused parser/state/server/shadow suite: 69 tests passed.
- Full Robot Agent, OPC UA Gateway and Fleet Control Plane regression: 226 tests passed.
- Core, adapters, storage, security and Isaac profile suite: 287 tests passed.
- XR suites passed separately to avoid duplicate test-module names: 28, 14 and 16 tests.
- ROS 2 workspace build: 11 packages passed with an isolated build/install directory.
- Phase 3 Edge Compose interpolation-free validation passed.
- Claude Opus safe-mode review: PASS, no critical findings. Its one strictness note about unknown
  `feature_contract` keys was applied and covered by a regression test.

## Rollout Rule

Deploy the Agent and Gateway together with `bi_so101_01.mapping.v2.yaml`. Updating only one side makes
authoritative local health unavailable by design; it must not fall back to a synthesized healthy state.
The legacy v1 file is retained only for a coordinated rollback of both processes.

## Next Slice

Generate per-Agent OPC UA Gateway identities, ports, replay stores and runtime directories with
collision validation. Add OMY/RB5 feature mappings only after vendor-confirmed representation limits
are available.

Follow-up: the vendor limits and all four remaining physical/simulation mappings were added and
validated in `PHASE4_M4_MULTI_ROBOT_MAPPING_V2_2026-08-21.md`.
