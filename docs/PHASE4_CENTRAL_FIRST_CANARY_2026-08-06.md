# Phase 4 Central-First v2/v3 Canary Result

Date: 2026-08-06  
Scope: read-only HTTPS heartbeat rollout and one OMY Isaac identity canary  
Result: PASS for this guarded canary slice

## Delivered

- `provision-agent` issues one HTTPS mutual TLS (mTLS) client identity per
  Agent from the existing Central certificate authority (CA).
- Runtime files are isolated under
  `roles/agents/<agent_id>/{central,edge}`. The Edge receives only its own
  client chain, private key and CA/certificate revocation list (CRL) bundle.
- Provisioning is serialized by a Central-side file lock, rejects partial
  exports, atomically activates a complete role directory and is idempotent.
- `scripts/enroll_phase4_agent.sh` registers the public certificate fingerprint
  and one heartbeat-only connection profile in the Central SQLite registry.
- `docker/compose.phase4-agent.yaml` runs one manifest per Agent with explicit
  heartbeat schema v3, mTLS and no preview or robot command endpoint.

## Automated Verification

- SANG security package: 11 tests passed.
- Fleet Control Plane package: 78 tests passed.
- Robot Agent package: 108 tests passed.
- Repeated temporary and live enrollment retained exactly one registration,
  credential and connection profile.
- Compose configuration, Python compilation and shell syntax checks passed.

Two Claude Opus read-only review attempts were made. Both remained in an API
wait state without returning findings and were stopped after bounded waits.
This is recorded as a reviewer-tool availability limitation, not a review
PASS. The rollout proceeded on automated tests, integration evidence and Codex
review. Fable was not used because Phase 4 is not complete.

## Live Central-First Rollout

Before restart, `processing-lab1` was online on heartbeat v2 at sequence 2239.
An online SQLite backup passed `PRAGMA integrity_check` and is stored at:

`runtime/phase3-lab1/archive/fleet-before-phase4-central-first-20260806T221702Z.db`

After only the Central Admin and mTLS ingress were recreated:

- Central advertised supported heartbeat schemas `[1, 2, 3]`;
- `processing-lab1` remained v2 and advanced to sequence 2244;
- no Processing Agent configuration or certificate changed.

The OMY canary then connected as `sim-isaac-omy-01` with heartbeat v3 and
reported `omy_sim_01.runtime_kind=simulation`. At the first joint check,
`processing-lab1` was v2/online at sequence 2265 and the canary was v3/online
at sequence 13.

## Failure Isolation

Stopping only the canary produced the expected stale/offline transitions. At
the offline check, `processing-lab1` remained v2/online and advanced to
sequence 2273. Restarting the canary created a new boot ID, restored it to
online at sequence 1 and left `processing-lab1` online at sequence 2277. No
`runtime_kind_drift` event was recorded.

After the bounded isolation gate had completed, `processing-lab1` stopped
sending at `2026-08-06T22:22:14.871Z`. Central recorded the expected stale and
offline transitions, while the canary continued normally. The Central host
could no longer ping or SSH to the Processing PC at `10.0.0.9`; therefore this
post-gate state is classified as external Lab1 host/network unavailability,
not a Central migration, certificate or canary isolation failure. The current
live topology is not claimed to have both Agents online until Lab1 reconnects.

The canary robot is currently `DEGRADED` because Isaac Sim is not publishing
the required joint and camera ROS 2 topics. This does not indicate an mTLS,
heartbeat or runtime identity failure. The older loopback `sang-sim-agent-omy`
container on port 8090 is a separate legacy deployment that was already
receiving HTTP 422 responses before this canary and was not modified.

No physical robot command, simulation exercise command, writable OPC UA
Variable or actuator method was introduced or executed.

## Remaining Phase 4 Gates

- OMY Isaac live observation is complete; see
  `PHASE4_OMY_ISAAC_LIVE_OBSERVATION_2026-08-07.md`.
- Implement mapping schema v2 before OMY/RB5 OPC UA Gateway conformance.
- Add independent HTTPS and OPC UA material for the remaining four runtimes.
- Add bounded parallel Central shadows and validate all five runtimes.
- Run the five-runtime short gate, one-Edge isolation gate and uninterrupted
  two-hour final gate.
