# PLAN - OPC UA Robotics and AAS architecture audit

Goal: Reconcile the implemented SANG architecture with the official OPC UA Robotics, Machine Vision, and AAS references without weakening the existing ROS 2 control and recording boundaries.

Done when:

- the current implementation is described from code rather than intention;
- each major component is classified as KEEP, FIX, OVERBUILT, DEFER, or MISSING;
- a backward-compatible target information model and migration sequence are documented;
- an independent Claude review is compared critically with the primary-source and code evidence;
- no runtime behavior is changed in this audit iteration.

Out of scope:

- importing official NodeSets into the running Edge server;
- adding OPC UA Methods or writable nodes;
- implementing an AAS repository or AASX runtime;
- changing physical robot, camera, welding, or safety-control paths.

## Planner

### Goal

Produce the architecture decision record that will govern the next implementation phases.

### Scope for this iteration

Audit only. Preserve the frozen read-only v1 contract and propose, but do not implement, a v2 dual projection.

### Tasks

1. Compare official primary references with the pinned local contracts.
2. Trace Edge OPC UA, Central OPC UA, AAS-ready contracts, storage, orchestration, and UI boundaries in code.
3. Obtain an independent Claude critique and resolve disagreements against evidence.
4. Write the target architecture, namespace layering, source-of-truth rules, and migration gates.
5. Verify every conclusion has either a local code reference or a primary-source reference.

### Acceptance Criteria

- The audit distinguishes alignment, implementation, interoperability, and certification claims.
- Existing v1 NodeIds and read-only behavior have a documented compatibility path.
- The document states where OPC UA Methods may eventually live and where they must not live.
- Machine Vision and AAS timing decisions are explicit.
- The final system covers heterogeneous real/sim robots, distributed processing PCs, recording, orchestration, safety, XR, and future equipment without putting high-rate data in OPC UA.

### Risks / Notes

- The worktree contains extensive user and previous-agent changes; this iteration edits documentation only.
- OPC UA safety status is observability, not a certified safety channel.
- Product pages do not substitute for a vendor NodeSet or NodeId manual.

### Developer Instruction

Write `docs/OPCUA_ROBOTICS_AAS_ARCHITECTURE_AUDIT_2026-09-01.md`; do not edit runtime code or frozen v1 contracts.

## Milestones

- [x] M1: Primary-source baseline recorded.
- [x] M2: Current implementation traced in code and contracts.
- [x] M3: Claude review compared and disagreements resolved.
- [x] M4: Audit and target architecture document written.
- [x] M5: Links, code references, classifications, and migration gates verified.

## Decisions log

- 2026-09-01: Preserve `sang-readonly-observation-v1`; propose a separately versioned official-NodeSet-backed projection rather than mutating v1.
- 2026-09-01: Keep ROS 2/vendor SDK as the real-time device plane and MCAP/object storage as the evidence plane.
- 2026-09-01: Keep Central OPC UA read-only; any future high-level Methods terminate at a commissioned Edge authority, not at the browser or Central aggregate.
- 2026-09-01: Fix Central lifecycle, quality, and dynamic read-only enforcement before implementing an official Robotics projection.
- 2026-09-01: Default v2 aggregation to one endpoint per logical Cell Agent/control-authority domain, not blindly per physical PC or per robot; record the resulting availability coupling.
- 2026-09-01: Treat OPC 30270 as a legacy/deprecating AAS mapping and base future work on one pinned current IDTA release.

## Current state

Complete. Primary references and local implementation were inspected; two Claude review rounds resolved disputed claims; the audit and target architecture were written; all external links returned HTTP 200; local references exist; and `git diff --check` passed.
