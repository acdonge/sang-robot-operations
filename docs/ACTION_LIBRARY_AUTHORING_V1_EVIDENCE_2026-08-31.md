# Action Library Authoring v1 Evidence

Date: 2026-08-31

## Result

PASS. The Admin application now has a dedicated `/actions.html` Action Library for creating a
robot-specific Action Draft from one C# or Python source file, selecting the existing versioned Skill
contract and allowed inputs, running Source and Simulation gates, and publishing an immutable Action
version. Qualified publications are merged into Workflow Studio automatically.

This feature adds no physical robot, welding, feeder, vendor SDK, ROS topic, endpoint, joint, or policy
command path. Central stores uploaded source but never imports, compiles, or executes it.

## Trust boundary

- Source accepts only UTF-8 `.cs` or `.py` files up to 256 KiB.
- Source is stored outside SQLite by SHA-256 with mode `0600`; API responses expose identity and hash,
  never source text or a server filesystem path.
- C# validation requires the `ISangUnityRobotAction` contract and rejects process, file, network,
  reflection, native, dynamic-code, and deployment API tokens.
- Python validation uses the AST without importing the module and rejects process, file, network,
  dynamic import, native-library, evaluation, and dunder-access paths.
- Allowed inputs are a subset of an existing versioned Skill schema, so a Draft cannot widen bounds.
- Simulation PASS requires the exact script hash and entrypoint to be installed in a trusted adapter,
  an exact step/capability/fixed-parameter match, and an immutable completed Simulation Run reporting
  `physical_output_connected=false`.
- Publish rejects any Draft unless both gates are PASS. Published records are protected by SQLite
  immutability triggers.

## Admin flow

The Ant Design page provides Robot, Script, Allowed inputs, and Review stages. Draft rows expose Edit,
Source Gate, Simulation Gate, and Publish actions. Publish remains disabled until both gate states are
PASS. Built-in Scenario 1 Actions are visible but immutable.

A newly uploaded script intentionally cannot be executed from the browser. The Build Owner must first
install the same SHA-256 source in an output-disconnected Unity or ROS 2 simulation build and register
that adapter contract. The Simulation Gate then accepts only matching completed-run evidence. Automating
that trusted build/install handoff is a later Build Owner pipeline, not a Central execution feature.

## Verification

- Full fleet-control-plane suite: `297 passed`, with one existing Starlette/httpx deprecation warning.
- Focused Workflow and Admin UI suite: `39 passed` before the full regression run.
- Python syntax compilation: passed.
- React/TypeScript typecheck: passed.
- Production Vite build: passed and generated `ui-admin/actions.html` plus `actions.js`.
- Build warning only: the existing shared `styles.js` chunk is larger than 500 kB.
- Browser: `/actions.html` rendered five built-in Actions, gate states, and the four-stage New Action
  dialog without creating an operational Draft.
- Runtime API: health returned `ok`; Action Library returned five built-ins and did not return source
  code.
- Production SQLite: `PRAGMA integrity_check` returned `ok`; dynamic Draft and publication counts
  remained zero after verification.
- Route-surface test explicitly allows only the five Action Library authoring writes and confirms there
  is still no robot, inference, welding, or feeder command writer.
- `git diff --check`: passed.

## Remaining work

The trusted Build Owner install/attestation pipeline is deliberately separate. Until it installs a new
script into a signed output-disconnected simulation build and produces matching run evidence, the new
Draft remains blocked at Simulation Gate and cannot be published. Physical release remains a separate
commissioning and safety-gate phase.
