# PLAN - Simulation Validation and Promotion v1

Goal: Let a Manager bind an immutable validation profile to a published Task, run it in an output-disconnected Unity Player from the web UI, inspect live preview and evidence, and promote only a passing result to a non-actuating commissioning candidate.

Done when:
- Versioned Simulation Validation Profiles define the exact adapter, scenario, run-count, duration, output-isolation, step-success, and Reset requirements.
- Every queued Simulation Run snapshots one Profile identity and produces a durable machine-readable validation report.
- A report cannot pass until all required checks pass; Reset acknowledgement is required when configured.
- Only a passing report can create an immutable `SIMULATION_APPROVED` commissioning candidate, and that action creates no robot command.
- Workflow Studio exposes Profile creation, profile-bound simulation queueing, report details, preview, and promotion state.
- A fixed-path Processing runtime supervisor can start/stop the windowed Unity Player and expose a bounded MJPEG preview without accepting arbitrary processes or commands.
- Backend, frontend, migration, runtime, and browser checks pass with physical output disconnected.

Out of scope:
- Physical robot, welder, feeder, ROS, vendor SDK, raw joint, XR, or policy actuation.
- Safety-rated approval, automatic real execution, or bypassing commissioning.
- WebRTC transport; v1 uses the same replaceable preview contract with bounded MJPEG.
- General Unity scene authoring or arbitrary executable launch.

## Milestones
- [x] M1: Persist schema and API contracts for Profiles, reports, and commissioning candidates.
- [x] M2: Attach Profiles to Runs and evaluate terminal/Reset evidence deterministically.
- [x] M3: Add fixed Unity runtime lifecycle and bounded web preview.
- [x] M4: Add the Ant Design Manager workflow for configure, run, inspect, and promote.
- [x] M5: Run migrations, focused/full tests, live browser verification, and record evidence.

## Decisions log
- 2026-08-31: A simulation PASS is not a physical or safety approval; promotion creates only a commissioning candidate.
- 2026-08-31: Profile versions and validation reports are immutable evidence.
- 2026-08-31: The Processing runtime may launch only one configured Player path with fixed safe arguments.
- 2026-08-31: MJPEG is the v1 preview transport because the current stack already supports it; the API boundary remains transport-neutral for later WebRTC replacement.
- 2026-08-31: A terminal Run whose validation is still PENDING is reconciled on Run read. This safely recovers mixed-version deployments without relaxing any gate.
- 2026-08-31: Validation requires one and only one successful evidence entry for every Step ID in the exact published Workflow; partial or duplicate evidence fails.

## Current state
M1-M5 are complete. The exact Unity 6000.5.6f1 Player builds, starts through the fixed loopback Supervisor, reports output-disconnected READY/Run state, and publishes a live 1280x720 JPEG preview at 5 fps through Central. Workflow Studio created immutable Profile `iitp-scenario1-coordinated-waam-simulation-unity-validation@1`, completed Run `sim-5dd0d7d927bc4dd5811e0db7073bf73e`, required an acknowledged Reset, produced PASS, and created non-actuating candidate `commissioning-776dd51f6eca422cb22e6a7d18b72bbe`. Full control-plane regression is 291 passed; database integrity is OK and the candidate audit records `physical_command_count: 0`.
