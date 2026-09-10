# PLAN - OPC UA Robotics, AAS, and G6 Readiness

Goal: Harden the current SANG OPC UA runtime, add standards-backed Robotics and AAS projections without breaking v1, and complete the non-authorizing G6b evidence workflow.

Done when:

- Central OPC UA reconciles removals, preserves unknown quality, and explicitly enforces read-only access after every refresh.
- Processing Node, Cell Agent, runtime Agent, Asset, and MotionDeviceSystem identities are distinct in a versioned profile contract.
- Official Core, DI, IA, and Robotics NodeSets are pinned, packaged, dependency-checked, and available to the Edge dual-projection runtime.
- One truthful fixture can instantiate the official Robotics hierarchy while incomplete robot profiles remain explicitly partial.
- Existing SANG v1 clients and NodeIds remain unchanged.
- IDTA AAS files can be validated, imported as a one-way read-only projection, linked to SANG assets and OPC UA endpoints, and exported without becoming a command path.
- Unity asset packages can be inventoried and resolved through stable bindings without enabling physical output.
- G6b evidence can be submitted, reviewed, and reported, but missing physical measurements or approvals remain `UNVERIFIED` and cannot authorize G7.
- Focused and full available regressions pass.

Out of scope:

- Claiming OPC UA Robotics conformance before profile and CTT evidence exists.
- Fabricating controller, power-train, safety, calibration, or stop-test values.
- Enabling physical robot, welder, feeder, digital, or analog outputs.
- Treating Unity, AAS, OPC UA, ROS 2, Wi-Fi, or this application as a certified safety controller.
- Committing unrelated user changes or deleting historical evidence.

## Milestones

- [ ] M0: Preserve and verify the current baseline; repair the stale OPC UA bind configuration - verify by: focused baseline tests and healthy configuration validation.
- [x] M1: Implement OPC-C01 through OPC-C04 - verify by: create/update/delete, unknown-quality, explicit-access, and post-refresh audit tests. (`52 passed` focused OPC UA suite)
- [x] M2: Implement identity/profile/mapping contract v2 - verify by: SO-101, OMY, RB5, and fixture contract tests with no identity collision or false conformance claim. (`16 passed` profile/mapping suite)
- [x] M3: Package official NodeSets and implement read-only Robotics dual projection - verify by: official hierarchy browse/type/value tests plus unchanged SANG v1 tests. (`59 passed` full gateway suite; Robotics 1.02 release-tag dependency set pinned)
- [x] M4: Implement dynamic Unity package inventory and official AAS read-only import/projection - verify by: fixture import/export, reference integrity, artifact hash, output-isolation, and backup/restore tests. (included in `329 passed` Fleet regression)
- [x] M5: Implement G6b evidence and approval workflow - verify by: incomplete/expired/out-of-limit evidence stays `UNVERIFIED`, complete evidence requires verified supporting artifacts plus digest-bound Manager and Safety Officer reviews, backup/restore passes, and every report keeps G7 and all command/process outputs disabled. (included in `329 passed` Fleet regression)
- [ ] M6: End-to-end review and documentation - verify by: focused suites, available full regression, diff audit, and updated canonical status.

## Decisions log

- 2026-09-01: Keep SANG observation v1 frozen and add standards support as a parallel projection.
- 2026-09-01: AAS is a one-way durable semantic projection; Central SQL remains operational authority.
- 2026-09-01: G6b software is complete only when it fails closed on absent physical evidence; a lab PASS cannot be inferred from code.
- 2026-09-01: Start with a truthful reference fixture and explicit partial profiles; do not invent mandatory values for SO-101, OMY, or RB5.

## Current state

M1 through M5 are implemented. The full Fleet regression is `329 passed` and the full OPC UA Gateway regression is `59 passed`. The G6b workflow software is complete, but no physical G6b evidence has been supplied and no lab approval is claimed. M0 code/test baseline passed, but the obsolete live SO-101 deployment remains intentionally untouched until its network identity and certificates are reprovisioned together; it must not be represented as healthy. M6 independent review/close-out remains outside the requested M1-M5 boundary.
