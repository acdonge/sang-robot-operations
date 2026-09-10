# PLAN - Action Library Authoring v1

Goal: Add an Admin Action Library that stores one robot action per C# or Python source file, validates it without executing untrusted code in Central, and exposes only simulation-qualified immutable publications to Workflow Studio.

Done when:
- An Admin can create and edit an Action Draft with robot identity, one C# or Python file, action metadata, an existing Skill capability, fixed parameters, and explicitly editable safe inputs.
- Uploaded source is content-addressed outside SQLite, never returned by list APIs, and never executed by the Central process.
- Source validation checks the runtime contract and rejects unsafe file, process, network, reflection, native-library, dynamic-code, and filesystem APIs.
- Simulation validation requires an exact installed action script digest plus a completed output-disconnected Simulation Run containing the matching step contract.
- Publish is impossible until both validation gates pass; publications are immutable and become visible in Workflow Studio.
- A dedicated Ant Design Action Library page exposes the complete flow and clear blocked-state guidance.
- Focused backend tests, frontend typecheck/build, browser verification, and SQLite integrity checks pass.

Out of scope:
- Executing uploaded source inside the Central API process.
- Automatically modifying a Unity or robot repository from the browser.
- Physical robot, welding, feeder, vendor SDK, ROS topic, endpoint, or joint command authoring.
- Treating static source checks as motion-validation evidence.

## Milestones
- [x] M1: Persist Action Drafts, immutable publications, content-addressed source, and request replay - verified with store tests and SQLite triggers.
- [x] M2: Implement source and adapter-backed simulation gates - verified with safe/unsafe C# and Python checks plus exact run-evidence tests.
- [x] M3: Add Action Library APIs and merge qualified publications into Workflow Studio - verified with focused API and body-boundary tests.
- [x] M4: Build the Ant Design Action Library page and navigation - verified by TypeScript, production build, focused UI tests, and browser flow.
- [x] M5: Run focused/full regression checks, review the diff, and record evidence - verified by 297 backend tests, frontend typecheck/build, browser inspection, API smoke checks, and SQLite integrity.

## Decisions log
- 2026-08-31: Central stores source but never executes it; a trusted simulation build remains the execution boundary.
- 2026-08-31: v1 accepts only `SIMULATION` actions. Physical release remains a separate commissioning path.
- 2026-08-31: Allowed inputs are selected from an existing versioned Skill schema and cannot widen its bounds.
- 2026-08-31: Simulation PASS requires exact script identity and immutable completed output-isolated Run evidence; a contract-only check is not enough.
- 2026-08-31: Built-in catalog actions remain immutable and appear beside dynamically published library actions.

## Current state
All milestones are complete. The deployed page lists five immutable built-ins and opens the four-stage authoring flow. The full fleet-control-plane suite passes 297 tests, the frontend typecheck and production build pass, and implementation evidence is recorded in `docs/ACTION_LIBRARY_AUTHORING_V1_EVIDENCE_2026-08-31.md`.
