# G1-G6 Implementation Status

Date: 2026-08-28

## Scope and safety boundary

This release implements G1 through G6a as Central software, simulation, replay, and
output-disconnected HIL infrastructure. It does not authorize or expose physical robot motion,
welding power, wire feed, digital outputs, analog outputs, or a vendor command SDK. Every readiness
report keeps `command_sink_present=false`, `physical_motion_enabled=false`,
`process_output_enabled=false`, and `g7_authorized=false`.

G6b is not software work. Physical stop measurements, hardwired safety-chain evidence, canonical
cell calibration, and named Safety Officer approval remain `UNVERIFIED` and block G7.

## Implemented gates

| Gate | Implemented result | Verification boundary |
|---|---|---|
| G1 | Immutable versioned registry; pinned Run contract; compatible shared/exclusive asset, workcell, group, zone, and process reservations; immutable runtime kind; replay mode; extended Run states; Central API | Production Unity source still needs a private pinned Git commit before an observer attestation can close the release gate |
| G2 | Safety Evidence v2 trusted ingress and loopback Edge forwarding; Central-derived provenance and receive time; measured local clock; boot/sequence ordering; confidence, calibration, zone, separation, and workflow checks; atomic assessment plus software `HELD` | It requests a task hold only and never claims or drives a physical E-stop |
| G3 | `sang_control_msgs`; lossless canonical transport codecs; G0 grant/sublease/goal verification; signed cancellation; durable Agent command ledger; idempotency; renewal/expiry; epoch/incarnation fencing; intent-before-dispatch journal; query-before-redispatch reconciliation; generic simulation DAG | ROS 2 Jazzy interface generation passes; an actual ROS process/action round-trip remains a deployment test |
| G4 | Standalone allowlisted `Sang.Observer` Unity package; output-declared Unity and Isaac in-process simulation Agents; source and built-Player artifact scanners; signed build-attestation generator and signature-gated Central registration | Current large Unity project is unpinned and no real role-specific Player artifact was supplied, so no production attestation exists |
| G5 | Immutable Ed25519 key identities with overlap/rotation/revocation; independent PolicyDemand Arbiter; sequence, rate, delta, age, frame, unit, clock, lease, and envelope fences; XR sample freshness plus network/focus/tracking/deadman latch; scoped Edge/Manager roles; atomic two-person resume | Demand transport deployment with site certificates remains a deployment task; the tested sink is deliberately disconnected |
| G6a | Device Agent skeleton with an output-disconnected controller; lease/comms/restart/cancel/ledger fault injection; fail behavior wired into runtime; digest-bound isolated timing evidence; machine-readable non-authorizing readiness report | G6b lab evidence remains unverified and cannot be inferred from simulation |

## Runtime components

- Central integrity: `orchestration_store.py`, `orchestration_api.py`, schema migrations 13-16.
- Safety v2: `safety_v2.py` and `/api/v2/safety/evidence`.
- Control contracts: `sang_control_msgs`, `control_contracts.py`, `control_signing.py`,
  `control_transport.py`.
- Processing runtime: `cell_orchestrator.py`, `agent_execution.py`.
- Simulation isolation: `simulation_adapters.py`, `simulation_build.py`, `unity/Sang.Observer`.
- Intervention authority: `control_authority.py`, `demand_arbiter.py`.
- G6 readiness: `commissioning_readiness.py`.

## Accepted behavior

1. A Run cannot enter `READY` unless its immutable workflow, binding, manifests, connections,
   capabilities, modes, and reservations agree.
2. Safety data cannot choose its source kind, Central receive time, or clock quality. Missing or
   stale trust evidence yields `UNKNOWN`, not `SAFE`.
3. Command intent is committed before an Agent call. A lost reply or process death after acceptance
   enters `RECONCILING`; restart queries the Agent ledger and increments the lease epoch before any
   new dispatch.
4. The Agent rejects stale/expired leases, same-epoch competing holder incarnations, signature
   failures, unsupported capabilities, and all pre-G7 physical modes.
5. Unity/Isaac accepted adapters require backends that explicitly declare robot and process outputs
   disconnected. Source and Player-artifact scans reject legacy sender classes, socket sends,
   command topics, vendor command SDK markers, `Assembly-CSharp`, and legacy transport assemblies.
6. XR loss, stale samples, or timestamp regression inhibit intervention. Restoring a Boolean signal
   cannot silently re-enable it. A HELD Run and both approvals change atomically only after distinct
   live Edge Operator and Manager approvals.
7. G6 readiness can report `SOFTWARE_READY`, but it always reports G6b as `UNVERIFIED` and cannot
   authorize G7.

## Remaining blockers and follow-ups

1. Put the Unity project in the agreed private repository, pin a commit, create role-specific Player
   builds, and run the implemented Player artifact scan against those real DLLs. The fixture DLL
   tests are not a production attestation or a complete third-party IL dependency audit.
2. `sang_control_msgs` and `xr_teleop_interfaces` build successfully under ROS 2 Jazzy. Run a
   generated ExecuteSkill/Query/Renew/Cancel action-service round trip once Agent ROS transport is
   deployed.
3. Deploy the Demand Arbiter on the Processing PC behind the existing site mTLS identity and prove
   certificate scope, revocation, restart, and clock-monitor wiring in that deployment.
4. Measure stop time and stop distance for each real robot, payload, tool, speed, and pose envelope.
   Witness communication loss, controller restart, lease expiry, and independent E-stop behavior.
5. Approve the canonical cell coordinate frame, calibration method, tolerance, and revalidation
   interval using measured lab evidence.
6. Name the actual Edge Operator, Manager, and Safety Officer accounts and complete the attended G7
   checklist. Only then may a separate physical controller adapter be designed and enabled.
7. Keep RB5 welding and CK WF-5 process outputs disconnected until the independent G9 process
   commissioning gate.

## Lab assumptions retained

- The first physical operating model is a fixed guarded zone with no person admitted during motion.
- ZED X human tracking and speed/separation monitoring are future safety evidence, not current
  machine-safety credit.
- Existing manufacturer E-stop hardware is independent of this software and must be witnessed at
  G6b; software `HELD` is not an E-stop.
- Current Unity scene geometry is a provisional spatial reference until the real cell calibration
  is measured and approved.

## Verification record

- ROS 2 Jazzy `colcon build`: `sang_control_msgs` and `xr_teleop_interfaces` passed.
- Existing Fleet, recording, OPC UA, security, RB5/SO-101 read-only, and G1-G6 tests:
  `481 passed`.
- XR OMY, bridge, and retarget tests: `58 passed`.
- Total Python regression: `539 passed`; only existing TestClient and OpenSSL deprecation warnings.
- AST parse: 14 new/changed G1-G6 runtime modules passed.
- Static command-boundary tests passed; physical and process output flags remain false.

## Independent review record

Claude Opus performed the pre-implementation read-only gap audit. Its three blocking findings were
addressed: missing confidence/producer clock trust, the Unity sender isolation boundary, and the
G6a/G6b split. A requested post-implementation Claude Fable audit could not start because Fable
credits were exhausted; the Opus fallback also hit its session limit before producing a report.
Therefore this document does not claim a completed post-implementation Claude review. Codex
performed the final diff audit and added atomicity, crash-window, cancellation-signature, XR
freshness, artifact-attestation, backend-isolation, and G6 fail-behavior regressions before the full
test run.
