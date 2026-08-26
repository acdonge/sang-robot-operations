# Robot/Camera Operations v1 Completion Plan

Date: 2026-08-21
Status: Complete (software release gate)

## Goal

Complete the observation-only SANG Robot/Camera Operations v1 without adding robot, welding,
feeder, EEF, or simulator command paths. The release must support multiple isolated Edge Agents,
durable asset/task operations, authenticated human and service access, on-demand camera viewing,
bounded observation capture, central artifact cataloguing, and a read-only central OPC UA view.

## Fixed Safety Boundary

- Central and both UIs remain observation/operations-management systems, not actuator controllers.
- No robot command, ROS publish/service/action, vendor command channel, welding I/O, or writable OPC UA
  node is introduced.
- Welding assets and their I/O contracts remain documented but unregistered and unimplemented.
- AAS content/server, LeRobot conversion, VLA training, XR actuation, historian, vector search, and
  retrospective pre-roll clipping are later phases.

## Storage Contract

- Versioned manifests and mapping contracts are the source of truth for static interfaces.
- SQLite stores stable registry records, Task Run lifecycle, identities, authorization, audit,
  latest snapshots, health events, recording sessions, and artifact metadata.
- Selected high-rate observation intervals are immutable MCAP artifacts. SQLite never stores raw
  frames or MCAP payloads.
- A central Object Store owns MCAP, metadata, and later derived MP4/JPEG objects. v1 ships a secure,
  atomic filesystem backend behind an Object Store interface; S3/MinIO can be added without changing
  the catalog contract.
- A later vector database may index derived embeddings and references only; it never replaces the
  authoritative object or metadata stores.

## Milestones

- [x] **M1: Baseline and multi-edge gate**
   - Preserve the existing Phase 4 observation stack and prove the full regression suite.
   - Add a bounded multi-edge fault-isolation regression where needed.
- [x] **M2: Registry and Task Run lifecycle**
   - Add revision-guarded updates and decommissioning for Processing Nodes, Robot/Camera assets, and
     Workcells.
   - Implement `DRAFT -> READY -> ACTIVE -> COMPLETE|FAILED`, reservation release, transition
     validation, timestamps, reason, and audit.
- [x] **M3: Identity and RBAC**
   - Add local human users, service API tokens, short-lived browser sessions, CSRF-protected writes,
     roles (`viewer`, `operator`, `admin`), and immutable security audit records.
   - Keep Agent mTLS identity separate from human/service identity.
- [x] **M4: Selected-camera relay**
   - Proxy only a selected assigned camera through Central after authorization.
   - Enforce URL allowlisting from observed bindings, bounded reads/timeouts, content type and size,
     no arbitrary URL proxying, and no background all-camera fan-out.
- [x] **M5: Selected-segment Object Storage and catalog**
   - Ingest an already-finalized local session manifest and its artifacts into an immutable central
     object namespace with path confinement, checksum verification, atomic writes, idempotency, and
     provenance.
   - Expose authorized session/artifact list and retrieval metadata APIs; Task Runs link to sessions.
- [x] **M6: Central read-only OPC UA aggregate**
   - Project Central registry, latest health, Task Run, and recording summary into a deterministic,
     read-only OPC UA namespace.
   - Provide a conformance check ensuring no writable application Variables or Methods exist.
- [x] **M7: Release hardening**
   - Add migration, backup/restore, install/upgrade/rollback documentation and an offline E2E release
     gate.
   - Update requirements/README and perform focused, full, build, and Phase-end Claude review.

## Acceptance Criteria

- Existing observation/adapters/security/MCAP/Isaac tests still pass.
- Every state-changing Central endpoint authenticates, authorizes, validates revision/state, and
  writes an audit event; anonymous access is limited to health and explicit login/bootstrap flows.
- A viewer cannot mutate configuration, an operator can manage Task Runs/capture metadata but not
  identities/registry, and an admin can manage configuration and identities.
- Completing or failing a run releases reservations exactly once and terminal runs cannot reactivate.
- Camera relay can fetch only a camera assigned to the authorized scope and never accepts a client
  supplied upstream URL.
- Object ingestion rejects symlinks, path traversal, changed size/hash, duplicate conflicting IDs,
  and partial sessions; successful retry is idempotent.
- Central OPC UA exposes deterministic read-only nodes and remains independently stoppable.
- Backup/restore reproduces catalog rows and object hashes in a clean directory.
- No actuator command surface is added.

## Execution Notes

- Implement the smallest coherent vertical slice per milestone and run focused tests before moving on.
- Do not modify or delete existing runtime databases or validation evidence.
- Use regular Claude Opus only for focused review when needed. Use `claude-fable-5` once, after all
  local v1 verification passes, and record its verdict in the final validation document.

## Decisions Log

- 2026-08-21: Kept static manifests, SQLite metadata, and immutable media objects as separate
  authorities; a vector index and AAS remain derived/future layers.
- 2026-08-21: Kept Central observation-only and excluded all robot/vendor/welding actuation.
- 2026-08-21: Added a dedicated remote mTLS Agent ingress to the v1 Compose profile instead of
  reusing the legacy auth-disabled Phase 3 stack.
- 2026-08-21: Disabled camera-preview redirects and made preview lease cleanup independent of body
  iteration.

## Current State

Software release gate passed. See `docs/ROBOT_CAMERA_OPERATIONS_V1_VALIDATION_2026-08-21.md` for the
test matrix, independent review disposition, and site-specific acceptance still required.
