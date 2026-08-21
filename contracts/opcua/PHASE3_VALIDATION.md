# Phase 3 Validation Evidence

Date: 2026-08-04

Status: **LOCAL SOFTWARE GATE PASS / ATTENDED EDGE LAPTOP PENDING**

## Scope

This evidence covers an isolated Central plus Edge deployment on one host using deterministic read-only
SO-101 ROS sources. It validates the distributed process boundary, security profiles and persistence,
but does not replace the required physical SO-101 laptop run, test-LAN measurements or final soak.

## Build and Regression

- Image: `sang-fleet:jazzy` with `asyncua 2.0.1`, `cryptography 50.0.0`, `pyOpenSSL 26.4.0`.
- Phase 3 test set: `193 passed`, `14 warnings`, `43.56s`.
- ROS build: `sang_security`, `robot_agent`, `sang_opcua_gateway`, and `fleet_control_plane` all built
  successfully in a clean temporary `colcon` build/install tree.
- Focused secure rejection tests use copied Edge CRLs and assert the intended asyncua status classes,
  including `BadCertificateUntrusted`, `BadCertificateRevoked`, and `BadUserAccessDenied`.

## Distributed Smoke

The smoke deployment used separate projects and ports so the existing lab services were not stopped:

- Central Admin: loopback `18090`.
- Central Agent ingress: `8443`, mutual TLS.
- Edge OPC UA: loopback `14840`, `Basic256Sha256` `SignAndEncrypt`.
- Edge Agent preview/health: loopback `18101`.

Observed Agent state was `ONLINE`, robot state was `READY`, both follower state topics were approximately
20 Hz, and all five MJPEG camera topics were approximately 25-29.5 Hz. The recheck forced
`ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST` in the fake source, Agent and Gateway while preserving
same-host ROS discovery. Every camera `preview_url` received by Central was `null`; the Edge-local
preview/health listener remained available to the Gateway.

The secure Central shadow completed after the frozen 40-second grace. The latest isolated recheck
recorded:

- 60.001 seconds elapsed.
- 5 strict heartbeat comparisons.
- 185 compared and 185 matched fields.
- 0 missing, timing-miss, alignment-timeout, or divergent fields.
- 1 connected epoch and 0 disconnects.

Central loaded the OPC UA endpoint and secret references from the enabled SQLite ConnectionProfile. The
resulting observation row contains configured endpoint `opc.tcp://127.0.0.1:14840/sang/`, expected
ApplicationUri, four discovery-measured namespace URIs, configured peer address, pinned Edge certificate
fingerprint, and success timestamp.

## Restart and Persistence

Only the Edge Gateway was restarted. The Agent remained online and retained the same boot UUID. The
Edge-local HMAC replay high-water mark advanced from sequence 37 to 42 in the same persistent SQLite
file after restart. A subsequent secure shadow completed with no divergence.

One real-run timing issue was found during this work: a heartbeat arriving at the very end of a bounded
non-final shadow could start a comparison window that the requested duration could not finish, producing
a false alignment timeout. A regression test now distinguishes that truncated final window from a real
full-window alignment failure. The restart test harness also received one extra second of scheduling
headroom without relaxing any reconnect assertions. The focused Gateway restart regression then passed
10 consecutive runs.

## PKI Operations

- Missing, invalidly signed, or expired OPC UA CRLs fail closed; a valid empty CRL remains valid.
- The copied-Edge-CRL integration proves revocation is not visible before distribution and is rejected
  after the refreshed CRL reaches Edge.
- `sync_phase3_edge_crls.sh` passed syntax and seven-day validity dry-run checks. End-to-end SSH transfer
  remains part of the attended laptop run because this host has no SSH listener.
- The CRL sync transport now allocates its archive with mode `0600` using `mktemp` on Edge, validates the
  returned path, and performs same-filesystem atomic role-file replacements. Automated tests verify the
  transfer command order and prove dry-run never contacts SSH or SCP.
- The previously interrupted repository-local PKI was resumed without deleting its key material. Five
  leaf certificates, role exports, Central registration DB and env files were completed. An identical
  second pre-rotation bootstrap completed idempotently. After credential rotation, initial bootstrap
  intentionally refuses to reuse the revoked fixed output and the rotation workflow remains authoritative.
- Make-before-break Agent mTLS rotation and old-credential revocation pass automated tests. Physical
  Agent restart and fingerprint handoff remain to be rehearsed on the laptop.

## Independent Review

Claude Opus performed three read-only security reviews. The second review identified copied-CRL freshness,
Edge UID mismatch, missing rotation tooling, interrupted-bootstrap recovery, production-profile drift,
and overly broad rejection assertions. These findings were reproduced where applicable and addressed in
code and tests. Its final broad verdict was `READY WITH CONDITIONS`: all four prior code blockers were
closed, with clock synchronization and a predictable remote CRL archive path remaining. The archive path
was replaced by Edge-side `mktemp` and reviewed again by Claude, which returned `READY` with no remaining
Critical or High code finding. Clock synchronization remains required attended evidence rather than a
code change because heartbeat v2 already rejects timestamps over 12 seconds old or 2 seconds in the future.

A final high-density read-only review used Claude Fable 5 with the current Phase 3 implementation and
Phase 4 roadmap in one pass. Its verdict was `READY WITH CONDITIONS` for the attended read-only SO-101
laptop test. Two pre-laptop deployment gaps were reproduced and fixed: all Edge ROS participants now
share the configured domain with host-local discovery, and the two-hour Compose path is a separate
`--final-gate` service rather than the short validation service. The review also caused the distributed
Agent to stop advertising an unusable Edge loopback preview URL while retaining its local health server.
Regression tests freeze all three deployment contracts.

The same review identified Phase 4 design constraints rather than immediate code blockers: per-runtime
port/domain isolation, per-Agent enrollment, explicit physical/simulation identity, per-feature unit/range
mapping, bounded parallel shadows and one-Edge failure isolation. These are captured in
`PHASE4_CONTRACT.md`; AAS, a Central aggregate OPC UA Server, historian, PubSub and camera relay remain
deliberately unimplemented.

## Remaining Attended Evidence

1. Record `id -u`, `id -g`, `timedatectl` or `chronyc tracking`, hostname, IP and firewall state on both
   Central and the Edge laptop.
2. Transfer only the Edge role, repository/image and generated `edge.env`; verify private-file owner and
   mode before starting containers.
3. Run fake sources first, then the physical read-only SO-101 adapters. Do not start command publishers or
   change torque state. Stop the Edge-local source once and prove same-domain publishers on another host
   cannot keep the Edge robot healthy.
4. Run secure shadow, Gateway restart/reconnect, CRL SSH distribution, missing/untrusted/revoked negative
   tests, and one attended Agent certificate rotation.
5. Run `shadow-final-gate` from the `final-validation` profile. Save its JSON and Central/Edge NIC counters;
   require `final_gate=true`, `passed=true`, at least 7,200 strict seconds and zero disconnects. Phase 3
   remains `IMPLEMENTING` until these artifacts pass.
