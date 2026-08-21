# OPC UA Phase 1 Validation

Date: `2026-08-04`

## Scope

This evidence covers the physical `bi_so101_01` read-only ROS 2 to OPC UA Edge sidecar. The
endpoint is loopback-only with `SecurityPolicy=None` and anonymous read access. No robot command,
motor register write, torque change or camera frame payload was exposed or executed.

## Results

| Check | Result | Evidence |
|---|---|---|
| Frozen NodeSet | PASS | XSD and official ModelCompiler 2.8.15 validation repeated. |
| ROS package build | PASS | `fleet_control_plane`, `robot_agent`, `sang_opcua_gateway` built with colcon. |
| Focused regression | PASS | 95 tests passed; one upstream FastAPI/httpx deprecation warning. |
| Live ROS correlation | PASS | Active left/right follower and left leader values matched ROS samples by `SourceTimestamp`; zero value/status mismatch. |
| Optional idle action | PASS | Right leader reported `BadWaitingForInitialData` without degrading required health. |
| Camera projection | PASS | All five cameras became Good/Fresh; no `frame_bytes` application node exists. |
| Information model | PASS | 73 mandatory model-namespace BrowsePaths resolved, including every source `Topics` component. |
| Read-only boundary | PASS | Recursive 374-node audit found 323 Variables, 0 writable Variables, 0 Methods and 0 byte-payload Variables. A write to an existing Variable returned `BadUserAccessDenied`. A negative-control test detects `ByteString`, `ImageJPG` and `Byte[]`. |
| Monitored items | PASS | The server's actual create-subscription response returned 100 ms joint sampling and 1000 ms camera/robot-health publishing; instance attributes advertise 50/500/1000 ms minimum sampling. |
| Failure isolation | PASS | Gateway stopped during a 30 second strict Agent soak; 31 samples remained healthy. |
| Performance gate | PASS | Three 5 minute OFF and three 5 minute ON runs passed. |
| Clean Dockerfile build | BLOCKED EXTERNALLY | Ubuntu mirror stopped making progress during `apt-get`; equivalent cached Jazzy layers plus pinned `asyncua==2.0.1` were used for runtime validation. |

## Live correlation

Report: `output/validation/opcua-phase1-live-final.json`

- follower left: 96 unique matched source timestamps, 576 matched Variable reads
- follower right: 96 unique matched source timestamps, 576 matched Variable reads
- leader left: 96 unique matched source timestamps, 576 matched Variable reads
- leader right: 96 verified idle reads with `BadWaitingForInitialData`
- five camera sources: all observed Good/Fresh
- value mismatch: 0
- StatusCode/timestamp mismatch: 0
- unmatched active-source reads: 0
- mandatory model BrowsePaths verified: 73
- application write rejected: `BadUserAccessDenied (0x801F0000)`
- application Variable count: 323; writable count: 0
- application Method count: 0
- camera frame bytes absent: true

The validator correlates every Variable with the ROS sample identified by that Variable's own
`SourceTimestamp`. Separate OPC UA Variable reads are not treated as one atomic multi-node snapshot.
The first check immediately after a gateway restart saw delayed DDS discovery for `right_wrist` and
failed rather than hiding the missing source. The same bounded check passed after discovery settled;
the camera itself remained near 29.5 Hz in the Agent and direct ROS rate probe.

## Failure isolation

Report: `output/validation/opcua-gateway-stop-isolation-final.json`

The gateway was stopped five seconds into a 30 second strict Central/Agent soak. All 31 samples were
healthy, with no Central fetch error or required-source degradation. Both followers remained near
20 Hz and all five cameras remained near 29.5 Hz. The gateway was then restarted without restarting
the Agent or adapters.

## Performance

Reports: `output/validation/opcua-off-{1,2,3}.json`,
`output/validation/opcua-on-{1,2,3}.json`, and
`output/validation/opcua-phase1-performance.json`

Each condition ran for 300 seconds three times. Every run contained 300 healthy samples, zero
unhealthy samples and zero fetch errors. CPU and RAM values are host-wide Agent heartbeat metrics;
the heartbeat computes five-second CPU averages, while the soak polls the latest heartbeat at one
second intervals. They are resource-regression indicators, not process-level profiler samples.
The OFF/ON labels were controlled by the operator and are not independently attested by a
gateway-liveness field in these artifacts.

| Metric | Gateway OFF median | Gateway ON median | Delta | Budget |
|---|---:|---:|---:|---:|
| Host CPU p95 | 4.177% | 5.175% | +0.998 percentage points | <= +10 pp |
| Minimum free RAM | 52980.736 MiB | 52902.912 MiB | -77.824 MiB | <= -512 MiB |
| Maximum required-source rate regression | - | - | 0.005% | <= 5% |

DDS wire traffic was not measured and is not claimed. Camera frame bytes remain on the existing
on-demand MJPEG path and are not included in OPC UA.

## Independent review

Claude CLI Opus reviewed the working tree read-only after its initial four blocking findings were
fixed. It derived 326 Mandatory instance checks from `SANG.NodeSet2.xml` rather than reusing the
shipped validator's curated paths; all 326 resolved. It independently counted 374 application nodes,
323 Variables, 0 Methods, 0 writable Variables and 0 byte-payload Variables, then returned **PASS**
for Phase 0/1. It also reproduced the Agent/gateway health differences that are now the first Phase 2
task and agreed to retain the separate sidecar and one-manifest-per-gateway boundaries.

After that review, the revised-publishing response source and byte-payload negative controls were
strengthened as described above. The 95-test suite and physical live report were repeated. A final
targeted Claude follow-up could not run because the CLI account reached its monthly usage limit; it
does not replace or weaken the completed independent PASS review.

## Remaining boundary

- Phase 1 supports one gateway process for one manifest and does not aggregate multiple robots.
- Agent and gateway subscribe independently and are known to disagree after an undecodable message
  and when a required source's rate becomes unmeasurable. Phase 2 must move freshness/rate/FSM
  semantics into one shared health module before judging heartbeat/OPC UA divergence.
- Remote binding, certificates, trust lists and authorization are Phase 3 work.
- Central OPC UA shadow subscription, divergence comparison and restart recovery are Phase 2 work.
- AAS, OPC UA control Methods and writable application Variables remain out of scope.
