# Phase 4 M4 Live Read-Only Rollout Result

Date: 2026-08-21
Scope: baseline preservation, runtime repair, physical SO-101 M3 deployment and multi-robot mappings
Result: PASS for the requested M1-M4 slice

## Source Baseline

- Preserved the Fleet/OPC UA implementation in Git commit
  `0ea2b85 feat: establish SANG fleet control plane baseline`.
- Added repository ignores for runtime databases, PKI, captures and browser artifacts before the
  commit. No private key, token, runtime DB or capture was committed.
- Baseline core regression: `259 passed`.

## Runtime Repair

- Added `sang_security` to Agent runtime/test Python paths.
- Corrected the RB5 helper's Central default from the retired `8090` service to `18091` and made the
  URL an explicit environment variable.
- Added the source-controlled `sang-phase3-lab-ntp` Chrony service. It tracks independent Ubuntu NTP
  pool sources, serves the isolated `10.0.0.0/24` lab network and never adjusts the host clock.
- Standardized the active Admin, Agent, shadow and soak CLI defaults on loopback port `18091`.
  Preview CORS accepts `18091` while retaining `8090` only for legacy local deployments.
- Disabled obsolete restart-loop Agent containers while preserving the actual observation adapters.

NTP validation uses a separate host-network client against `10.0.0.8`. The container is healthy only
after Chrony selects an upstream and reports normal leap status; there is no unconditional local-clock
fallback that can advertise synchronization after all upstream sources are lost.
Because the container runs with `chronyd -x` and without `CAP_SYS_TIME`, host time synchronization is
a separate prerequisite. The rollout host independently reported `NTP=yes` and
`NTPSynchronized=yes`; deployments must recheck that state. Host port `123`/`323` ownership must also
be checked before starting this host-network service.

## Physical SO-101 Deployment

- Generated a dedicated runtime for Agent `workcell-soarm-01` and robot `bi_so101_01` with unique
  HTTPS client, OPC UA server, local-health HMAC, replay DB, data path and port allocations.
- Deployed one physical Agent and one read-only OPC UA Gateway. No robot command publisher, writable
  OPC UA Variable or OPC UA Method was added.
- Live signed local-health combined manifest digest exactly matched the digest calculated from the
  source manifest and Mapping Schema v2 contract.

Live Central/UI state:

- Agent: `ONLINE`.
- Robot: `READY`, `sources_fresh=true`, `rates_ok=true`, no fault.
- Follower left/right state: approximately `20 Hz` each with an `18 Hz` floor.
- Five cameras: approximately `27-30 Hz` with a `25 Hz` floor.
- All five local JPEG preview endpoints returned HTTP 200, `image/jpeg` and valid JPEG files.
- Leader-right observed action had no sample during validation. It is optional and correctly did not
  degrade robot health.

The distributed secure runtime intentionally suppresses an Edge loopback preview URL in its Central
heartbeat. Therefore the Admin page shows camera health but not the local image, even though the five
Agent-local JPEG endpoints are healthy. A remote authenticated preview relay remains a separate slice;
an Edge `127.0.0.1` URL must not be advertised as if it were Central-accessible.

## OPC UA Shadow

The secure 60-second Central shadow completed with:

- `SignAndEncrypt` and one connection epoch;
- 59 comparisons and 703 fields compared;
- 703 fields matched, 0 divergent and 0 missing;
- 19 strict comparisons;
- 0 timing misses, alignment timeouts or disconnects;
- connected at finish;
- maximum heartbeat age `1.0 s` and maximum observed gap `1.659 s`.

This was a smoke shadow with `requested_strict_duration_s=0`; the 19 strict comparisons were observed
after startup grace but were not a non-zero strict-duration acceptance requirement. It is not the
deferred final gate.

Evidence is retained under the ignored runtime path
`runtime/phase4/agents/workcell-soarm-01/data/so101-live-shadow-60s.json`.

## Multi-Robot Mapping Schema v2

- Added exact physical/simulation contracts for OMY and RB5-850E.
- Added explicit camera FPS to all four matching manifests.
- Pinned per-feature radian units and manufacturer-backed finite ranges.
- Added digest, coverage, physical/simulation parity and camera-profile regression tests.

Detailed provenance and coverage are recorded in
`PHASE4_M4_MULTI_ROBOT_MAPPING_V2_2026-08-21.md`.

## Verification

- Focused manifest/mapping suite: `37 passed`; final CLI/CORS/security regression subset:
  `49 passed`.
- Full core/adapters/storage/security/Isaac suite: `305 passed`, 14 deprecation warnings.
- ROS 2 isolated build: `sang_security`, `robot_agent`, `sang_opcua_gateway` and
  `fleet_control_plane` all passed.
- YAML parse and `git diff --check`: passed.
- Central `GET /api/v1/healthz`: HTTP 200 with `status=ok`.
- In-app browser: physical SO-101 detail rendered `ONLINE`, `READY`, `Fresh` and `Healthy` with all
  expected source rows.
- Two independent Claude Opus read-only reviews returned `PASS` with no blockers. The focused
  follow-up identified residual `8090` CLI/CORS defaults; those defaults were then standardized and
  covered by regression tests.

The first full test attempt omitted the external Isaac asset mount and produced one expected asset
hash failure while 303 tests passed. The missing read-only mount was added, the isolated asset test
passed, and the then-complete 304-test suite passed. After the independent review, one CLI-default
regression test was added and the new complete suite passed with 305 tests.

## Known Non-Blocking State

- The Isaac OMY canary Agent is online but its robot is `DEGRADED` while Isaac Sim observation
  publishers are stopped.
- The historical Lab1 Processing Agent is offline because that laptop test runtime is not running.
- SO-101 right-wrist camera rate has briefly crossed the `25 Hz` floor and recovered, creating noisy
  low/recovered events. It was healthy at final validation; threshold hysteresis is a future monitoring
  improvement, not a reason to weaken the source contract in this slice.
- RB5 control-box connectivity and the physical OMY sources were not activated for this mapping-only
  rollout.

## Explicitly Deferred

The user excluded the five-runtime concurrent shadow, one-Edge failure isolation and two-hour final
gate from this iteration. This result does not claim those final Phase 4 gates.
