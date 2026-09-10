# SANG Documentation Consolidation Validation - 2026-09-01

## Verdict

**PASS for documentation consolidation.** One master roadmap now owns current status and sequence,
all first-party plans and documents are classified, the core status contradictions are corrected, and
no runtime behavior or dated acceptance evidence was rewritten.

This verdict does not close the Central OPC UA correctness defects listed for the next sprint and does
not authorize any physical robot or process output.

## Delivered artifacts

- `SANG_MASTER_IMPLEMENTATION_PLAN.md`: sole current whole-program roadmap.
- `docs/DOCUMENT_AUTHORITY_INDEX.md`: authority, precedence and complete plan/document inventory.
- `PLAN_DOCUMENT_CONSOLIDATION.md`: task-local execution and decision record.
- Authority/status corrections in README, requirements, Task-Scoped Operations, OPC UA/AAS audit,
  Multi-Robot Orchestrator, Dynamic Unity/AAS, Workflow Studio and Robot/Camera v1 documents.

## Corrected contradictions

1. Central OPC UA is no longer described as Client-only; the custom read-only aggregate exists.
2. G1-G6a is recorded as software/disconnected-HIL complete while G6b/G7-G9 remains unimplemented or
   unauthorized.
3. The custom SANG OPC UA tree is not described as OPC UA Robotics 1.02 conformance.
4. The current zero-Method policy is a safety/product decision, not a claim that Robotics 1.02 lacks
   `SystemOperation` or optional `TaskControl` operations.
5. Raw D405/D455/OTOS/MJPEG cameras are not classified as complete OPC UA Machine Vision systems.
6. A0 is classified as internal AAS-ready contracts, not an IDTA metamodel/API/AASX/Repository.
7. Simulation approval remains non-actuating and cannot imply physical commissioning.

## Code cross-check

The four P0 Central OPC UA findings were rechecked against
`src/fleet_control_plane/fleet_control_plane/central_opcua.py`:

- entity/variable maps only create or update and do not reconcile removals;
- `bool(...)` coerces missing/unobserved freshness/rate state to `false` without an explicit bad or
  uncertain StatusCode;
- `_add_variable()` does not set explicit application `AccessLevel`/`UserAccessLevel`; and
- `audit_read_only()` runs before server start but is not repeated after periodic projection refresh.

They are real runtime hardening work, not documentation errors. They remain outside this
documentation-only iteration and are Wave 1 in the master plan.

## Verification

- Every root `PLAN_*.md` is present in the authority index.
- Every first-party `docs/*.md` file is present in the authority index.
- All 94 first-party Markdown files were scanned; 148 local Markdown links resolve to existing targets.
- The 11 canonical/core files passed a separate local-link check.
- New canonical files contain no trailing whitespace.
- `git diff --check` passed for tracked changes.
- Conflict-phrase search confirmed the retired Client-only and wholly-unimplemented-Orchestrator
  statements are absent from current authority text.

No application test suite was run because this iteration changes documentation only.

## Independent review record

The consolidation incorporates the earlier two-round Claude Opus critique recorded in
`docs/OPCUA_ROBOTICS_AAS_ARCHITECTURE_AUDIT_2026-09-01.md`, including the withdrawn stale standards
claims and the accepted local-code findings.

A fresh read-only Opus review of the consolidated documents was attempted. The first high-effort run
did not return a report and was stopped; the bounded retry returned `You've hit your session limit`.
Therefore this record does **not** claim a fresh Claude PASS. A later quota-available review may add a
new dated evidence file, but the missing retry does not alter the completed local consistency checks.

## Residual items

- Implement Wave 1 Central OPC UA correctness before official Robotics projection work.
- Keep the interrupted OTOS Phase 3.7 plan marked superseded; do not retroactively convert it to PASS.
- Continue treating all dated site evidence as topology-specific.
