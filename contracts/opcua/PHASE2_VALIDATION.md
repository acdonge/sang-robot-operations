# OPC UA Phase 2 Validation

## Status

**Hardened implementation and automated validation: PASS (2026-08-04).**

The required two-hour physical shadow soak is a separate final gate. Its report must exist and pass
before Phase 2 is marked complete. Until then, Phase 2 remains `VALIDATING` rather than `COMPLETE`.
The first final-gate attempt was stopped after review found fail-open conditions in the validator.
The hardened final gate started in `sang-opcua-shadow-soak` at `2026-08-04T20:39:18Z` and was
intentionally stopped by the operator at `2026-08-04T20:47:15Z` to begin Phase 3 work. No final-gate
report was produced, so this run is cancelled evidence rather than a pass or product failure.
Phase 2 remains `VALIDATING`; a future secure distributed soak does not retroactively satisfy this
loopback gate unless the validation contract explicitly replaces it.

## Authority decision

The initial design used two independent best-effort ROS subscriptions and moved their calculations
to one shared `HealthTracker`. Physical testing disproved the assumption that this alone guarantees
equal health: Agent and Gateway can receive different event sets under callback and camera load.
Threshold tolerance or debounce would hide the disagreement rather than remove it.

Phase 2 therefore uses one explicit health authority:

- Robot Agent calculates transport freshness, rate, FSM and fault once.
- The exact snapshot used for the heartbeat is exposed over the existing loopback Agent HTTP server.
- The envelope contains schema and algorithm versions, Agent/robot identity, boot UUID, monotonic
  sequence, UTC snapshot time, manifest digest and canonical payload digest.
- Gateway atomically validates identity, topology, manifest, digest, sequence and health invariants.
- Gateway projects the validated Agent health but keeps independent ROS subscriptions for latest OPC
  values and their data-quality `StatusCode`.
- Duplicate snapshots do not refresh link age. Sequence rollback, retired boot replay, digest change
  at the same sequence and malformed payloads are rejected.
- Snapshot UTC age is checked on receipt, so a stale pre-restart snapshot cannot be replayed after
  the Gateway process loses its in-memory boot and sequence high-water marks. A future timestamp
  never extends the 12-second transport-silence timeout.
- After 12 seconds without a new accepted snapshot, Gateway reports the authority unavailable and
  does not silently fall back to its independent health calculation.

This is intentionally read-only. It adds no ROS command publisher, actuator API, OPC UA Method or
writable application Variable.

## Central comparison

The Central shadow remains a separate Client-only process. It reads
`/api/v1/shadow/agents`, which uses only `list_snapshots()` and presentation logic and never runs
status reconciliation or mutates Fleet application tables/events. It subscribes to the Edge and performs a
batched read before comparison. A heartbeat is compared only when all 37 projected health fields
carry the exact authoritative snapshot time within 1 ms. Alignment must occur inside the frozen
6-second comparison window.

The report fails on any post-grace field divergence, missing field, timestamp miss, alignment
timeout, Agent non-online/stale/frozen heartbeat, unrecovered disconnect or connection epoch without
a strict comparison. Recovery is counted only after the reconnected epoch completes a fully matched
strict comparison. Startup and reconnect grace are both the frozen 40 seconds. The final gate also
enforces the frozen 12-second heartbeat gap. Its 7,200-second clock starts only after the first fully
matched post-grace strict comparison, and any OPC UA disconnect fails it immediately. Restart
recovery is intentionally a separate gate so reconnect grace cannot reduce the two-hour observation.
At the deadline, the Client performs a final server-state, heartbeat-liveness, alignment, value and
exact StatusCode probe; the last polling sleep is therefore part of the verified interval.

## Automated verification

Inside the ROS 2 Jazzy/asyncua container:

```bash
PYTHONPATH=/ws/src/fleet_control_plane:/ws/src/robot_agent:/ws/src/sang_opcua_gateway:$PYTHONPATH \
pytest -q \
  src/robot_agent/test \
  src/sang_opcua_gateway/test \
  src/fleet_control_plane/test

colcon build --packages-select \
  robot_agent sang_opcua_gateway fleet_control_plane \
  --symlink-install
```

Result: **157 tests passed** and all three packages built. Tests cover invalid samples, unknown
required rates, source-time rate parity, multi-topic feature contracts, snapshot digest/tamper and
sequence rejection, wall-clock replay rejection, authority staleness, local-value quality
separation, exact Central alignment, Agent death/stale heartbeat injection, alignment timeout,
initial BadWaiting values and real asyncua restart recovery with per-epoch strict checks.
Health comparison also checks exact `Good` versus `BadWaitingForInitialData`; evaluator loss is
projected as `BadNoCommunication` and cannot pass merely because both wire values normalize to null.

## Physical SO-101 evidence

The Agent, five MJPEG cameras, two follower state topics and loopback OPC UA Gateway stayed read-only
throughout both runs.

| Report | Duration | Strict snapshots | Fields matched | Disconnect/recovery | Divergence | Timing miss | Alignment timeout |
|---|---:|---:|---:|---:|---:|---:|---:|
| `output/validation/opcua-phase2-short-hardened.json` | 70 s | 29 | 1,073 / 1,073 | 0 / 0 | 0 | 0 | 0 |
| `output/validation/opcua-phase2-reconnect-hardened.json` | 120 s | 36 | 1,332 / 1,332 | 1 / 1 | 0 | 0 | 0 |

Both hardened reports enforce Agent liveness and heartbeat advancement. The short run observed a
maximum heartbeat age of 1.000 seconds and a maximum advancement gap of 1.696 seconds. The reconnect
run observed 1.000 seconds and 2.005 seconds respectively, with strict comparison counts `[18, 18]`
for its two connection epochs. The reconnect shadow ran in a separate container. Only
`sang-opcua-gateway` was restarted. The SO-101 Agent remained `online` with a sub-second heartbeat
age, the Edge reconnected in about two
seconds, and recovery was recorded only after the full 40-second reconnect grace and a matched
strict comparison. After the shadow process exited, Agent and Gateway continued running.

## Two-hour final gate

Run the shadow in a separate container so Gateway restart or failure cannot terminate the validator:

```bash
docker run --name sang-opcua-shadow-soak --network host \
  -v "/home/sa08050/app developer/v2_ws:/ws" \
  -w /ws \
  -e PYTHONPATH=/ws/src/fleet_control_plane:/ws/src/robot_agent:/ws/src/sang_opcua_gateway \
  sang-fleet:opcua-phase1 \
  python3 -m fleet_control_plane.opcua_shadow \
    --agent-id workcell-soarm-01 \
    --robot-id bi_so101_01 \
    --duration-s 7200 \
    --final-gate \
    --model-contract /ws/contracts/opcua/model-contract.yaml \
    --report /ws/output/validation/opcua-phase2-soak-2h.json
```

Final acceptance requires `passed=true`, one uninterrupted OPC UA connection epoch, a
`strict_observation_s` of at least 7,200 seconds after the first matched strict comparison, and zero
divergence, missing fields, timing misses, alignment timeouts or heartbeat-gap violations. Total wall
time is expected to exceed 7,240 seconds because startup grace is not counted as strict observation.

## Residual boundary

- Loopback HTTP and OPC UA `SecurityPolicy=None` remain Phase 2-only. Remote Edge security is Phase 3.
- The authority guarantee assumes the Gateway can reach the local Agent endpoint. Link loss is
  explicit degradation, not fallback.
- Phase 2 snapshots are unsigned and the boot/sequence high-water mark is process-local. The UTC
  age bound blocks stale replay after restart, but an on-host replay inside the 12-second window is
  part of the loopback trust boundary until Phase 3 authentication and persistence.
- Raw value equality, SourceTimestamp and StatusCode remain covered by Phase 1 evidence; Phase 2
  compares the authoritative health projection.
- OMY, RB5 and Isaac multi-edge conformance remain Phase 4 work.
