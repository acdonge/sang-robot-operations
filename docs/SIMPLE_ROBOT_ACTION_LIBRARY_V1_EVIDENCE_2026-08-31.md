# Simple Robot Action Library v1 Evidence

Date: 2026-08-31

## Result

PASS. Workflow Studio now exposes a robot-centric, top-to-bottom Action sequence instead of the
technical Role, Capability, Operation, Step ID, dependency, and JSON editor. Existing immutable
Workflow v2 publications and the output-disconnected Unity Scenario 1 adapter remain compatible.

This change adds no physical robot, welding, feeder, vendor, policy, or arbitrary script command
path. Browser source upload and runtime path authoring remain absent.

## Action contract

The original `iitp-scenario1-skill-catalog.v1.json` remains unchanged. The additive v2 catalog
registers five actions:

| Robot | Action | Entrypoint | Source SHA-256 |
| --- | --- | --- | --- |
| Mobile Carrier | `mobile-deliver-base-plate@1` | `MobileDeliverBasePlateAction` | `098d6edae933993942886610ad3bc6aef0da133a9674b685a36a2006e2ca63e7` |
| OMY F3M | `omy-place-on-waam-table@1` | `OmyPlaceOnWaamTableAction` | `a871d9618731a11520f371a12cf9e0a30ac9909a90f21f738f3fced16e0cc949` |
| RB5-850E | `rb5-follow-waam-path@1` | `Rb5FollowWaamPathAction` | `9f0dc3df8c69142bfc90c2e6435262512d644c527877cbb74961725daee1ecaf` |
| OMY F3M | `omy-return-to-carrier@1` | `OmyReturnToCarrierAction` | `3bf2150cbe04e5234704a2e638faa25fad92b840f9c90d17413a996cf24fd6d0` |
| Mobile Carrier | `mobile-return-with-base-plate@1` | `MobileReturnWithBasePlateAction` | `01948a684a44aa7ce705833974f1ba0322f3fb0709c2bba5d8fc0d7bc0071118` |

Each entry fixes its robot type, logical version, Skill capability, operation, role template, Step ID,
timeout, runtime, script reference, entrypoint, and digest. Only catalog-declared safe inputs such as
simulation speed are editable. Central derives a linear dependency chain after add, edit, remove, or
reorder.

The five C# entry files implement `ISangUnityRobotAction` and are registered by
`SangUnityScenarioAgent`. They currently adapt the proven Scenario 1 state machine rather than
duplicating its trajectory implementation. Future robot-specific Python Device Agents can implement
the same logical Action contract under a separately approved physical-control gate.

## Verification

- Workflow authoring and Admin UI tests: `34 passed`.
- Full fleet-control-plane suite: `292 passed`.
- React/TypeScript production build: passed.
- Unity `6000.5.6f1` strict Linux Player build: passed.
- Candidate Player: `output/unity-action-library-v1/SangScenario1Simulation.x86_64`.
- Compiled `Assembly-CSharp.dll` contains all five registered Action entrypoint classes.
- All five catalog SHA-256 values match their Unity C# source files.
- SQLite `PRAGMA integrity_check`: `ok`.
- Browser: existing Scenario 1 Draft rendered five robot Actions in the correct order; the edit dialog
  exposed only Robot, Action, and Motion speed.
- Deployed Central action endpoint returned five entries from catalog v2.

The broader canonical suite produced `516 passed` and one unrelated failure in
`sim/isaac_vendor/test_profile.py` because a local vendor asset hash no longer matches its pinned
profile. No Action Library or Workflow file participates in that failure.

## Existing Unity failure

Run `sim-70ba92d0988e4bf4b51608db8347cda7` failed in the existing OMY lift segment, not in the browser
editor. Unity reported an IK error of `0.239 m`, above the configured `0.20 m` stop threshold, after a
different plate/gripper geometry state. The failed Run remains immutable evidence with state `FAILED`
and validation `FAIL`.

Reset was requested through the normal Workflow Studio control path and acknowledged. The live
runtime returned to `ReadyDelivery`; `physical_output_connected=false`. The Action Library change does
not claim to fix this separate Unity scene-determinism issue.

## Remaining boundary

The newly built candidate proves the separated Action entry files compile. The currently running
validated Player was reset in place and remains behavior-compatible because the published Workflow
contract is unchanged. Promoting the candidate Player to the long-lived Supervisor, re-running a full
Scenario 1 pass, and hardening the intermittent OMY IK reset condition are separate attended runtime
steps.
