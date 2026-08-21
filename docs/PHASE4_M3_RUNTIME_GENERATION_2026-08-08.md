# Phase 4 M3 Per-Agent Runtime Generation Result

Date: 2026-08-08

Status: **IMPLEMENTED AND LOCALLY VERIFIED**. This result completes the SO-101 runtime-generation
foundation, not the whole Phase 4 five-runtime conformance gate.

## Delivered

- strict versioned inventory for Agent, robot, Processing Node, endpoint, port, ROS domain and paths;
- pre-provision collision and existing-output drift checks;
- PKI-root generation lock and shared Central allocation registry across separate inventories;
- independent HTTPS client and OPC UA server identities under the shared Central PKI;
- Agent-scoped Central OPC UA server pin, local-health HMAC and replay DB path;
- schema v4 ConnectionProfile pin persistence with data-preserving v3 migration;
- immutable env/deployment records and an additive deployment index;
- separate read-only Agent/Gateway Compose file, leaving the heartbeat-only canary untouched.

No physical robot command, writable OPC UA Variable/Method, camera byte relay or live runtime migration
was performed.

## Verification

- focused PKI, enrollment, schema and generator tests: 40 passed;
- Sang Security, Robot Agent, OPC UA Gateway and Fleet Control Plane regression: 259 passed;
- isolated ROS 2 build: 4 packages completed;
- SO-101 TEST-NET canary generated twice with byte-identical reports;
- generated Docker Compose configuration parsed successfully;
- generated env, OPC UA server key and local-health HMAC key were all mode `0600`;
- Python syntax and shell syntax checks passed.

The final isolated canary used `/tmp/sang-phase4-m3-verified.tGIBda`. It exercised the production
wrapper, including canonical host-path handling, not just the Python module. It did not read or modify the
live Central DB, live PKI, port 18091 service or any physical robot.

Claude Opus reviewed the initial implementation, the corrective changes and the final reservation-write
ordering. The final review verdict was PASS with no High, Medium or Low findings. Fable was not used
because this closes M3, not the whole Phase 4.

## Remaining Phase 4 Work

1. Add vendor-confirmed OMY and RB5 mapping schema v2 files, then simulation variants.
2. Deploy the generated SO-101 runtime to its real Processing Node and run an attended read-only gate.
3. Add bounded parallel Central shadows and latest-only per-profile observation projection.
4. Run all five profiles together, one-Edge failure isolation, then the uninterrupted two-hour gate.
