# Phase 4 Multi-Edge Contract

Status: **IMPLEMENTING / M4 COMPLETE**. The runtime identity foundation, repeatable per-Agent HTTPS
and OPC UA enrollment, generated isolated runtime bundles, one OMY Isaac heartbeat-v3 canary and the
SO-101 live read-only runtime plus OMY/RB5 physical/simulation Mapping Schema v2 contracts are
implemented. The five-runtime conformance topology and final gate have not run. The
earlier Phase 3 timing gate remains
a recorded failure; on 2026-08-06 the user accepted its Lab1-only CPU contention as non-production
hardware debt and authorized Phase 4 implementation to proceed without converting that result to PASS.

## Goal

Observe physical SO-101, OMY, RB5-850E and Isaac Sim runtimes concurrently without confusing assets,
sharing credentials, leaking ROS discovery across computers, or allowing one failed Edge to damage the
other Edges or Central UI. Phase 4 remains read-only.

The minimum conformance topology is five robot runtimes:

1. physical SO-101;
2. physical OMY;
3. physical RB5-850E;
4. Isaac Sim OMY;
5. Isaac Sim RB5-850E.

## Deployment Unit

The secure multi-edge deployment convention is:

```text
ProcessingNode 1 ---- N AgentRuntime
AgentRuntime   1 ---- 1 robot manifest
AgentRuntime   1 ---- 1 OPC UA Gateway
AgentRuntime   1 ---- 1 Central ConnectionProfile
```

`robot_agent` can monitor multiple manifests in other local deployments, but Phase 4 conformance uses
one manifest per Agent runtime. This preserves the implemented one-manifest Gateway boundary and gives
each robot an independent identity, certificate, endpoint, replay state and restart boundary. Multiple
Agent runtimes may still run on one Processing Node, including a Central-owned PC.

Each runtime must have unique values for `agent_id`, `robot_id`, OPC UA ApplicationUri, OPC UA port,
preview port, data directory, local-health key/replay database and leaf certificates. A Gateway is still
a sidecar of its Agent and is not registered as another Fleet Agent.

## Physical and Simulation Identity

Before connecting the Phase 4 topology, Registry and heartbeat contracts must carry an explicit
`runtime_kind` with values `physical` or `simulation`. Physical and simulation manifests use distinct
`robot_id` values even when they represent the same model. Binding reconciliation must reject a
physical Asset observed from a simulation runtime and the inverse.

M1 implements the rollout-safe wire and Registry foundation:

- local loopback heartbeat remains schema v1;
- existing secure Agents remain schema v2 and cannot include `runtime_kind`;
- schema v3 requires secure boot identity and `runtime_kind` on every reported robot;
- v3 emission is an explicit Agent option, never inferred from payload contents;
- Central accepts secure v2 and v3 so it can be upgraded before any Edge;
- the current manifest v0.1 derives the canonical runtime kind from `real_or_sim` without changing the
  deployed manifest digest;
- Registry schema v3 stores nullable Asset `runtime_kind`. `NULL` means unknown, not physical;
- Asset create/correction rejects a known physical/simulation mismatch, and later heartbeat identity
  drift creates a critical transition event without rejecting unrelated robots on that Agent.

An optional digital-twin relation may later link two stable `asset_id` values. It must never collapse
their runtime identity, health, reservations or data provenance into one record.

## ROS 2 and Host Isolation

- Every adapter, fake/simulation source, Agent and Gateway in an Edge observation group uses the same
  `ROS_DOMAIN_ID` and `ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST`.
- `ROS_DOMAIN_ID` is configuration, not identity, and must be a canonical integer in `0..232`.
- Separate robot runtimes on one host use separate ROS domains unless their topic namespaces are proven
  collision-free by a deployment test.
- Central never relies on DDS discovery across robot PCs. Only mTLS heartbeat and secured OPC UA cross
  the host boundary.
- Every OPC UA and preview listener on the same host has a unique port. The generated deployment must
  reject duplicate port or runtime-directory assignments before startup.

## Enrollment and Trust

The current Phase 3 bootstrap enrolls one Agent and must not be copied as a shared role directory.
Phase 4 first separates Central CA initialization from repeatable per-Agent enrollment:

- one Central-managed offline Root and protocol issuing CAs;
- one HTTPS client and one OPC UA server identity per Agent runtime;
- one local-health HMAC key and replay DB per Agent runtime;
- one role export directory and certificate rotation/revocation record per Agent runtime;
- no CA private key on an Edge and no leaf private key shared between Edges.

ConnectionProfiles and trust pins are keyed by the concrete Agent/runtime endpoint. Parallel enrollment
must be idempotent before rotation and must not overwrite another Agent's material.

The implemented runtime generator exports each identity under
`roles/agents/<agent_id>/{central,edge}`. The Edge export contains only its client chain, private key and
HTTPS trust bundle plus its own OPC UA server identity, trust/CRL, authorized Central user certificate
and local-health HMAC key; it contains no CA private key or another Agent's leaf key. Central stores one
Agent-scoped OPC UA server pin. Provisioning uses a Central-side file lock, fail-closed drift detection,
immutable output checks and atomic writes. Repeating generation does not duplicate credentials or
profiles. Runtime inventory validation rejects identity, host, port, ROS domain and directory collisions
before provisioning, and every runtime receives a distinct replay database path. A Central allocation
registry below the shared PKI root and a generator lock preserve those reservations across separate
inventory files and output roots.

## Mapping Schema v2

The SO-101 v1 mapping uses LeRobot-normalized values and cannot be reused unchanged for OMY or RB5.
Before adding those robots, mapping schema v2 must describe each numeric feature independently:

- source key, ROS topic, field or joint-name selector and feature order;
- OPC UA data type, engineering unit and valid range;
- required/freshness/minimum-rate contract;
- SourceTimestamp provenance and quality/status mapping;
- optional EEF and process-I/O groups without requiring an EEF that is not installed.

Existing SO-101 v1 NodeIds remain stable. A v2 validator must compare every mapping against its manifest
and reject missing features, duplicate NodeIds, invalid units/ranges and real/simulation identity drift.

M2 adds schema `2.0.0` while retaining v1 as a rollback parser. The default SO-101 v2 file declares
selector, dense order, OPC UA `Float`, engineering unit, finite valid range and semantic for all 24
state/action features. Gateway range enforcement now reads those feature records instead of inferring
gripper ranges from names. A canonical mapping SHA-256 is combined into the Agent/Gateway local-health
manifest digest, so mixed files fail closed. Existing SO-101 NodeIds are unchanged. M4 adds exact
manifest coverage for OMY, RB5 and their Isaac Sim counterparts. OMY bounds come from the pinned
ROBOTIS OMY F3M URDF. RB5 bounds come from the Rainbow Robotics system specification (`+/-360 deg`,
with `elbow` at `+/-165 deg`) and are converted to radians after the read-only adapter converts vendor
`jnt_ang` degrees. The bundled RB5 URDF's narrower generic `+/-3.14` values are not authoritative.

## Central Observation

- Central runs one shadow state machine per enabled `(agent_id, robot_id, connection_profile_id)` with
  bounded concurrency. Initial maximum parallel connections are eight so the five-profile conformance
  topology can run concurrently; additional profiles queue.
- A failed or restarted Edge changes only its own observation and health. It cannot stop another shadow,
  Agent, adapter or the Admin server.
- Current observation is stored as latest-only state per profile. Append-only audit and health transition
  history remain separate; high-rate values and images are not copied into SQLite.
- Aggregate status is derived from per-profile results and never replaces the individual error.
- Preview in this phase remains health/metadata only. A remote authenticated camera relay is a separate
  slice with explicit bandwidth and concurrent-viewer limits; Edge loopback URLs are never sent to a
  remote browser.

## Acceptance Gates

1. Schema migration, duplicate port/domain/runtime-directory rejection and physical/simulation binding
   rejection pass automated tests.
2. SO-101, OMY, RB5, OMY-sim and RB5-sim mappings pass exhaustive manifest coverage validation.
3. All five Agent/Gateway profiles connect concurrently with no observed-binding or identity collision.
4. A two-hour all-profile final gate reports zero unexpected disconnect, divergence, missing field or
   timing miss after startup grace.
5. In a separate failure-isolation test, stopping one Edge marks only that runtime unavailable; the
   other four continue strict comparison and the stopped Edge alone recovers after restart.
6. Stopping an Edge-local ROS source cannot be masked by same-domain publishers on another computer.
7. Central and each Edge record clock-sync evidence and NIC counters without claiming protocol-specific
   attribution that was not measured.

No physical command publisher, torque change, OPC UA Method or writable SANG application Variable is
allowed in these gates.

## Delivery Order

1. Preserve the Phase 2/3 outstanding gates as explicit debt; do not relabel partial or CPU-contended
   evidence as PASS.
2. Complete the M1 `runtime_kind` Central-first rollout and automated compatibility checks. **Done;
   live v2/v3 compatibility and one-Edge isolation verified 2026-08-06.**
3. Freeze mapping schema v2 after implementing file-driven feature metadata and digest binding.
   **Done for the SO-101 foundation; OMY/RB5 mappings remain M3 inputs.**
4. Implement repeatable per-Agent enrollment and generated per-runtime deployment validation.
   **Done for the SO-101 canary foundation; no live Edge was migrated by the generator test.**
5. Add OMY, RB5 and Isaac mapping/profiles one at a time with focused conformance tests. **OMY Isaac
   live observation and heartbeat-v3 canary done; Gateway mapping remains.**
6. Add bounded parallel shadows, latest observation projection and one-Edge failure isolation.
7. Run the five-profile short gate, then the uninterrupted final gate.

## Deferred

AAS, Central aggregate OPC UA Server, historian/Historical Access, PubSub, camera relay, Control Mode
Manager and any actuator control remain outside Phase 4. They require consumer, retention, safety and
authorization contracts that the read-only multi-edge gate does not provide.
