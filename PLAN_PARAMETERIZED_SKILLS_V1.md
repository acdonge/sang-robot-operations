# PLAN - Parameterized Skill Editor v1

Goal: Replace raw JSON motion editing with a versioned, typed Skill catalog that can drive the output-disconnected Unity Scenario 1 simulation from Workflow Studio.

Done when:
- Workflow Studio discovers Skill definitions and renders typed parameter controls.
- The backend validates known Skill parameters, units, defaults, enums, and numeric bounds before publication and simulation queueing.
- Scenario 1 accepts a validated `speed_scale` between 0.50 and 1.00 for all five simulated actions and applies it without enabling physical output.
- Existing Workflow publications without `speed_scale` remain runnable with the 1.00 default.
- Backend tests, frontend build, Unity build, and one parameterized simulation run pass.

Out of scope:
- Physical robot, welder, feeder, ROS topic, vendor endpoint, or raw joint command editing.
- A free-form 3D waypoint/trajectory editor.
- Behavior-tree conditions, loops, fallback branches, and reusable sub-workflows.
- Automatic MoveIt planning inside Central; robot-local Agents will own that later.

## Milestones
- [x] M1: Reference-backed architecture and versioned Skill catalog contract - verified by catalog loading and bounds tests.
- [x] M2: Backend discovery and strict parameter validation - verified by API, publication, and adapter compatibility tests.
- [x] M3: Typed Workflow Studio Skill inspector - verified by TypeScript build and browser interaction.
- [x] M4: Unity consumes safe simulation parameters - verified by exact Unity 6000.5.6f1 build, artifact scan, and rejection tests.
- [x] M5: End-to-end parameterized Scenario 1 - verified by a complete output-disconnected Simulation Run and reset.

## Decisions log
- 2026-08-31: Chose a high-level Workflow plus versioned Skill definitions over a raw trajectory editor. This keeps heterogeneous robot details behind Agent adapters while preserving reusable task intent.
- 2026-08-31: Chose a JSON Schema 2020-12-compatible object subset for typed parameters. Closed schemas prevent accidental command fields and allow the same contract to generate UI controls and backend validation.
- 2026-08-31: Chose a conservative `speed_scale` range of 0.50-1.00 for v1. The tested motion remains the upper bound; faster-than-tested motion and pose editing are intentionally excluded.
- 2026-08-31: Existing Scenario 1 topology, operation names, paths, and safety/output-isolation boundaries remain fixed. Only simulation timing is parameterized.

## Current state
M1-M5 are complete. The checked-in catalog exposes four versioned Scenario 1 Skills, Central validates closed parameter schemas and adapter-fixed operations, and Workflow Studio renders typed controls while retaining raw JSON only for unknown capabilities. Unity 6000.5.6f1 applies the validated `speed_scale` without enabling physical output. The full Fleet Control Plane suite passes (280 tests), the Admin UI production build passes, and the published Scenario 1 v2 workflow completed all five steps at `speed_scale=0.75` before a successful reset. Detailed evidence is recorded in `docs/PARAMETERIZED_SKILL_EDITOR_V1_EVIDENCE.md`.
