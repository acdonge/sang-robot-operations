# Phase 4 M1 Runtime Identity Result

Date: 2026-08-06  
Scope: read-only runtime identity foundation  
Result: PASS

## Implemented

- Heartbeat contracts now explicitly support v1, v2 and v3 and reject unknown
  versions.
- Local loopback remains v1. Existing mutual TLS (mTLS) Agents remain v2 by
  default. An Edge must explicitly select v3 to publish `runtime_kind`.
- Heartbeat v3 requires boot ID, monotonic sequence and `physical` or
  `simulation` identity for every reported robot.
- Manifest v0.1 remains unchanged; its existing `real_or_sim` value is mapped
  canonically to the runtime identity used by v3.
- Central secure ingress accepts both v2 and v3 for a Central-first rollout.
- SQLite schema v3 adds nullable Asset `runtime_kind`; old rows remain unknown
  instead of being falsely labelled physical.
- Asset creation and correction reject a known observed identity mismatch.
- A later Agent identity change creates `runtime_kind_drift` and recovery
  transition events. Task-scoped observation exposes `IDENTITY_MISMATCH`.
- OPC UA mapping loading now rejects unknown mapping schema versions instead
  of silently ignoring future fields.

No robot command publisher, writable OPC UA Variable or actuator method was
added.

## Verification

- Robot Agent: 108 tests passed.
- Fleet Control Plane: 77 tests passed.
- SANG OPC UA Gateway: 30 tests passed.
- Focused cross-package contract set: 70 tests passed.
- Python compile check passed using an isolated bytecode cache. The repository
  source cache is root-owned from earlier container runs and was not modified.

## Deferred To M2

Mapping schema v2 will define file-driven unit and valid-range metadata per
numeric feature and bind that metadata into the Agent/Gateway digest. SO-101
normalized ranges can be migrated first. OMY and RB5 ranges require
vendor-confirmed representation limits; values will not be inferred from the
RB5 URDF because observed controller output has already contradicted it.

The follow-up Central-first canary added repeatable HTTPS per-Agent enrollment
and verified isolation between the existing processing-lab1 v2 Agent and one
OMY Isaac v3 Agent. See `PHASE4_CENTRAL_FIRST_CANARY_2026-08-06.md`.

The five-runtime simultaneous topology, per-Agent OPC UA Gateway material,
parallel Central shadows and the two-hour all-profile gate remain later Phase 4
work.
