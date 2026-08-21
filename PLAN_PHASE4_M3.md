# PLAN - Phase 4 M3 Per-Agent OPC UA Runtime

Goal: Generate isolated, repeatable Agent/Gateway runtime bundles for a multi-robot Processing Node.

Done when:

- one inventory deterministically generates an env file and resolved deployment record per runtime;
- duplicate identities, ports, ROS domains, and runtime paths fail before PKI or database writes;
- every runtime receives an independent HTTPS client, OPC UA server, local-health key, and replay path;
- the Central ConnectionProfile stores the matching per-Agent OPC UA server pin;
- a SO-101 canary bundle passes repeat-run, Compose, PKI, and regression tests.

Out of scope:

- moving live Agents from port 8090 to 18091;
- OMY/RB5 feature mappings before vendor-confirmed limits are available;
- physical robot commands, camera relay, AAS, and writable OPC UA nodes;
- the five-runtime final soak and failure-isolation gate.

## Milestones

- [x] M1: inspect Phase 3/4 enrollment, Gateway, Compose, and database contracts.
- [x] M2: add per-Agent OPC UA role provisioning and verification.
- [x] M3: add strict runtime inventory parsing, collision validation, and deterministic generation.
- [x] M4: bind per-Agent server pins into ConnectionProfiles and generated Phase 4 Compose input.
- [x] M5: generate and validate a SO-101 canary bundle in an isolated temporary runtime.
- [x] M6: run focused/full regression, Opus review, and update Phase 4 documentation.

## Decisions Log

- 2026-08-08: keep the current 8090 and 18091 services untouched during generator development.
- 2026-08-08: reject duplicate ROS domains on the same Processing Node until an explicit
  namespace-isolation exception contract exists.
- 2026-08-08: store one OPC UA server pin per Agent profile instead of relying on the Phase 3
  shared `edge-cert.pem` path.
- 2026-08-08: derive replay DB and data paths below a unique runtime directory rather than accepting
  independently configurable paths that can drift.
- 2026-08-08: serialize generators with a PKI-root lock and keep a Central allocation registry under
  `roles/central/runtime-allocations.json`, so separate inventories/runtime roots cannot reuse one
  Processing Node's listener, ROS domain or identity.

## Current State

M1-M6 are complete. The generator provisions independent HTTPS and OPC UA identities, local-health
keys, replay paths, ConnectionProfiles and immutable runtime records from one validated inventory.
The SO-101 documentation-address canary is deterministic and its generated Compose input validates.
Independent Opus review returned PASS with no remaining High, Medium or Low findings. Phase 4 itself
remains open for the OMY/RB5 mappings, live multi-Edge deployment and final conformance gates.
