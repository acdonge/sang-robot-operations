# SANG OPC UA Contracts

## Status

- Contract version: `1.0.0`
- Publication date: `2026-08-04`
- First embodiment: `bi_so101_01` (`Sang Dual SO-101`)
- Runtime implementation: Phase 1 complete; Phase 2 hardened short/reconnect pass; Phase 3 local
  distributed software gate pass; Phase 4 Mapping Schema v2 foundation complete
- Control methods and writable application nodes: forbidden
- AAS mapping: deferred

This directory freezes the v1 model contract and records each implementation phase. The ROS 2 manifest remains the
authority for local topics, units, ordering and health thresholds. The Asset Registry remains the
authority for stable `asset_id` and assignment. OPC UA is a read-only projection of those contracts
and the latest runtime observation.

## Files

| File | Purpose |
|---|---|
| `nodesets.lock.yaml` | Pins the official OPC Foundation files and ModelCompiler version. |
| `SANG.NodeSet2.xml` | Defines the read-only SANG model namespace and reusable ObjectTypes. |
| `model-contract.yaml` | Freezes namespaces, NodeIds, time, sampling and performance rules. |
| `status-code-mapping.yaml` | Freezes data-quality precedence and OPC UA StatusCodes. |
| `bi_so101_01.mapping.v2.yaml` | Default SO-101 per-feature mapping with digest binding. |
| `bi_so101_01.mapping.yaml` | Legacy v1 rollback mapping with unchanged NodeIds. |
| `native-opcua-endpoint.example.yaml` | Inactive, secret-free example for the 3+1 native path. |
| `PHASE0_VALIDATION.md` | Reproducible coverage and official validator evidence. |
| `PHASE1_VALIDATION.md` | Runtime, physical correlation, isolation and performance evidence. |
| `PHASE2_VALIDATION.md` | Shared health authority, Central shadow and restart-recovery evidence. |
| `PHASE3_SECURITY.md` | Distributed Edge security, deployment and attended acceptance contract. |
| `PHASE3_VALIDATION.md` | Local distributed security/build/smoke evidence and remaining laptop gate. |
| `PHASE4_CONTRACT.md` | Draft multi-edge contract for SO-101, OMY, RB5 and Isaac Sim. |
| `../../docs/PHASE4_M2_MAPPING_SCHEMA_V2_2026-08-07.md` | Mapping Schema v2 implementation evidence. |

## Model decision

The first profile is **Robotics-aligned**, not Robotics-conformant. OPC UA Robotics 1.02 requires
model elements that the current observation-only SO-101 adapter cannot truthfully provide,
including a complete motion-device-system representation. SANG therefore uses its own read-only
observation types while pinning Robotics 1.02 as the semantic alignment reference. OMY and RB5 may
later add a separately tested Robotics conformance profile without changing these v1 NodeIds.

The five OpenCV cameras are raw MJPEG observation sources, not complete machine-vision systems with
recipe, configuration and result management. Machine Vision 1.00 is pinned as a future alignment
reference but is not a required model for `SANG.NodeSet2.xml`.

No conformance or certification claim is made for either Companion Specification.

## Frozen identities

- Model namespace: `urn:sang:fleet:model:1`
- Edge instance namespace: `urn:sang:fleet:edge:1`
- Edge ApplicationUri: `urn:sang:agent:<agent_id>`
- Model version: `1.0.0`
- NodeId identifier type: OPC UA String
- Maximum identifier size: 2048 UTF-8 bytes

The model requires the official Core `1.05.07 / ModelVersion 1.5.7` publication from
`2026-05-01` or newer. The newer `2026-07-30` publication remains pinned as the semantic
validation reference. This separates the minimum runtime dependency from the validator input and
allows `asyncua 2.0.1` to import the unchanged SANG model without weakening its type requirements.

Namespace indexes are runtime-local and are never persisted as identity. A node is identified
across servers by `(ApplicationUri, NamespaceUri, identifier)`.

Dynamic NodeId components use canonical RFC 3986 percent encoding over UTF-8 bytes. Only
`A-Z a-z 0-9 - . _ ~` remain unescaped, hex digits are uppercase, and input is encoded exactly once.
The `/`, space, `%` and non-ASCII test vectors in `model-contract.yaml` are normative.

## Value and time rules

- Joint source messages replace the latest value; no history or lossless queue is provided.
- SO-101 joint values are OPC UA `Float` in `lerobot_normalized` units.
- Mapping v2 declares each feature independently. The current SO-101 non-gripper ranges are
  `[-100, 100]` and gripper ranges are `[0, 100]`; the Gateway does not infer ranges from names.
- Camera frame bytes are never OPC UA Variables. Only identity, configuration, health, sequence,
  source time and on-demand stream URI are exposed.
- A valid ROS header stamp becomes `SourceTimestamp`.
- A zero or unavailable header uses gateway receive UTC with provenance
  `gateway_receive_fallback` and at least `Uncertain` quality.
- `ServerTimestamp` is the Edge gateway update time.
- `CentralReceivedAt` exists only in Phase 2 shadow evidence and never overwrites source time.

Consumers must inspect StatusCode before Value. For a Bad StatusCode, OPC UA ignores the supplied
value and the server exposes a Null wire value; this is not retained last data. Unsupported clock
offset/skew values use typed `NaN` and epoch sentinels with Uncertain quality because an Uncertain
DataValue still carries a typed value.

Health Variables report the result of the health calculation. For example, a valid `Fresh=false`
Variable has `Good` quality because the Boolean itself is a valid observation. The associated sample
value carries `UncertainLastUsableValue` or a Bad code according to
`status-code-mapping.yaml`.

## Sampling profile

The gateway ingests each ROS message, retains only the latest validated sample and uses queue size
one with discard-oldest behavior for OPC UA monitored items.

| Class | Requested sampling | Publishing | Queue |
|---|---:|---:|---:|
| Joint state/action values | 50 ms | 100 ms | 1 |
| Camera metadata and source health | 500 ms | 1000 ms | 1 |
| Robot and clock health | 1000 ms | 1000 ms | 1 |

The Server may revise intervals. Phase 1 records the revised values and fails if joint sampling is
revised above 100 ms or health publishing above 2000 ms.

## Phase 0 validation

### 1. Structural XML validation

Download `Schema/UANodeSet.xsd` from the commit in `nodesets.lock.yaml`, then run:

```bash
xmllint --noout \
  --schema /tmp/ua-nodesets/Schema/UANodeSet.xsd \
  contracts/opcua/SANG.NodeSet2.xml
```

### 2. Official ModelCompiler validation

Install the pinned tool outside the repository and place the pinned official NodeSets plus
`SANG.NodeSet2.xml` in one input directory:

```bash
dotnet tool install \
  --tool-path /tmp/opcua-tools \
  OPCFoundation.Opc.Ua.ModelCompiler.Tool \
  --version 2.8.15

/tmp/opcua-tools/Opc.Ua.ModelCompiler compile-nodesets \
  -input /tmp/ua-nodesets \
  -o2 output/validation/opcua-modelcompiler \
  -uri urn:sang:fleet:model:1
```

The official tool is the semantic validation gate. Generated C#/constants are validation output,
not product source, and stay under `output/validation/`.

### 3. Mapping coverage

Coverage validation must compare `bi_so101_01.mapping.v2.yaml` with
`manifests/bi_so101_01.yaml` and fail on any of these conditions:

- state or action feature order differs;
- source key, topic, required flag, freshness or minimum rate differs;
- camera key, topic, encoding, width, height or FPS differs;
- an encoded NodeId exceeds 2048 bytes or collides;
- the mapping does not contain exactly 12 state features, 12 action features and 5 cameras;
- a mapped application Variable is writable or a SANG application Method exists.

The v2 validator also rejects undeclared engineering units, non-finite or non-increasing ranges,
unsupported OPC UA data types, selector/name mismatch and non-contiguous feature order. Agent and
Gateway compute the same canonical mapping SHA-256 and combine it with the authenticated local-health
manifest digest. A v2 mismatch is never downgraded to v1.

## Phase 0 acceptance

- [x] Twelve state features mapped.
- [x] Twelve observed-action features mapped.
- [x] Five cameras mapped without frame bytes.
- [x] Seven source-health and nine topic-health inputs covered by templates.
- [x] Namespace, NodeId, timestamp, StatusCode and sampling contracts frozen.
- [x] Official NodeSet and ModelCompiler versions pinned.
- [x] Structural and semantic validation procedures documented.
- [x] Device 3+1 classes and one inactive native OPC UA endpoint example documented.
- [x] SANG writable application Variable count is zero.
- [x] SANG application Method count is zero.
