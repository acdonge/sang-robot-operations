# PLAN - Simple Robot Action Library v1

Goal: Replace the technical Workflow step editor with a robot-centric action library where one registered, versioned script entry represents one reusable robot action.

Done when:
- Every UI-visible action identifies one robot model, logical action ID, version, runtime, script reference, digest, default timeout, and validated inputs.
- Workflow Studio lets a user build a default-linear Task by selecting `Robot` and `Action`, with only safe inputs such as simulation speed exposed.
- Role, capability, operation, Step ID, and dependency wiring are generated internally and no longer shown in the normal editor.
- Existing Workflow v2 publications, validation Profiles, Unity execution, and non-actuating promotion remain compatible.
- Arbitrary script paths, uploads, shell commands, endpoints, ROS topics, and physical bindings cannot be authored from Central.
- The failed Unity Run is diagnosed, reset through the normal control path, and the updated UI is browser-verified.

Out of scope:
- Uploading or editing source code in the browser.
- Physical robot command execution or automatic sim-to-real release.
- A general graph editor. v1 creates a linear sequence; a later UI can add named parallel groups without exposing raw dependencies.
- Refactoring every existing Scenario 1 controller into a standalone C# implementation in this iteration. Existing handlers receive explicit immutable script identities first.

## Milestones
- [x] M1: Extend the Skill catalog with immutable robot/action/script metadata and validation.
- [x] M2: Replace normal Workflow authoring controls with the Simple Action Sequence editor.
- [x] M3: Add focused backend/frontend compatibility tests and build the UI.
- [x] M4: Reset the failed Unity Run, verify the simplified browser flow, and record evidence.

## Decisions log
- 2026-08-31: Reuse the existing versioned Skill contract as the Action Library instead of creating a second competing registry.
- 2026-08-31: The user chooses only a robot action and safe parameters; Central derives all orchestration identifiers.
- 2026-08-31: `script_ref` is descriptive and allowlisted metadata, never an executable path accepted from a Task.
- 2026-08-31: Unity C# and ROS2 Agent Python implementations may share one logical action ID, but each implementation has its own immutable digest.
- 2026-08-31: Preserve Skill catalog v1 and publish the additive Action Library as catalog v2.

## Current state
All milestones are complete. Five Scenario 1 actions have immutable v2 catalog entries and one C# entry file each; the simplified UI and runtime reset were browser-verified. Focused tests passed 34, the fleet-control-plane suite passed 292, the Unity strict Player build passed, and evidence is recorded in `docs/SIMPLE_ROBOT_ACTION_LIBRARY_V1_EVIDENCE_2026-08-31.md`. The intermittent OMY IK reset condition remains a separate attended runtime issue.
