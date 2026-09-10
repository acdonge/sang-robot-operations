# Simulation Validation and Promotion v1 Evidence

Date: 2026-08-31

## Result

PASS. A Manager can run an immutable published Task in the output-disconnected Unity Scenario 1 Player, view the live scene and evidence in Workflow Studio, acknowledge Reset, and create only a non-actuating commissioning candidate.

This result is simulation evidence. It is not physical commissioning, a safety-rated approval, or permission to command a robot.

## Pinned identities

- Published Task: `iitp-scenario1-coordinated-waam-simulation@2`
- Published Workflow: `iitp-scenario1-orchestrated-simulation@2`
- Validation Profile: `iitp-scenario1-coordinated-waam-simulation-unity-validation@1`
- Simulation Run: `sim-5dd0d7d927bc4dd5811e0db7073bf73e`
- Commissioning candidate: `commissioning-776dd51f6eca422cb22e6a7d18b72bbe`
- Candidate state: `SIMULATION_APPROVED`

## Unity runtime

- Editor/build version: Unity `6000.5.6f1`
- Player: `output/unity-simulation-validation-v1/SangScenario1Simulation.x86_64`
- Build role: `simulation_executor`
- Compile boundary: `SANG_SIMULATION_PLAYER`
- Physical output declaration: `false`
- Preview: MJPEG, `1280x720`, target `5 fps`
- Final runtime state: `READY`, phase `ReadyDelivery`
- Runtime and proxy bind only to loopback; the web API cannot launch an arbitrary executable.

## Validation evidence

The Run reached `COMPLETE` in `193.915 s`, below the Profile limit of `600 s`. Evidence contains exactly the five Step IDs in the published Workflow, each once and each in `SUCCEEDED` state:

1. `mobile_delivery`
2. `omy_pre_pick_place`
3. `rb5_waam`
4. `omy_post_pick_place`
5. `mobile_return`

The adapter digest was pinned, `physical_output_connected=false`, and Unity acknowledged Reset before the report changed from `AWAITING_RESET` to `PASS`.

The commissioning audit event records:

```json
{
  "evidence_run_ids": ["sim-5dd0d7d927bc4dd5811e0db7073bf73e"],
  "physical_command_count": 0,
  "profile_id": "iitp-scenario1-coordinated-waam-simulation-unity-validation",
  "profile_version": 1
}
```

## Verification

- Control-plane test suite: `291 passed`
- Workflow authoring focused suite: `20 passed`
- Admin UI TypeScript compile and Vite production build: passed
- Database `PRAGMA integrity_check`: `ok`
- Browser flow: Profile `1/1 PASS`, `SIMULATION_APPROVED`, immutable evidence count `1`, and `no physical command path`
- `git diff --check`: passed

## Recovery and negative controls

- Partial, missing, or duplicate Step evidence cannot satisfy `all-steps-succeeded`.
- A terminal Run left `PENDING` by a mixed-version deployment is re-evaluated on the next Run read without bypassing Profile checks.
- Failed or insufficient evidence cannot create a commissioning candidate.
- Profile and candidate records are immutable in SQLite.
- Unity build warnings are pre-existing deprecation/serialization warnings; the exact Player build completed successfully. They do not change this non-actuating result, but should be cleaned before treating the scene as a long-lived production simulation asset.

## Remaining boundary

The host-side Unity runtime Supervisor is running for this session but is not yet installed as a boot-persistent service. Physical commissioning, real robot command transport, safety PLC integration, calibrated collision geometry, and signed release approval remain separate future gates.
