# SANG Fleet Workspace Requirements

## Purpose

Provide a ROS 2 Jazzy, observation-first operations system for multiple robot workcells, with durable
asset/task state, authenticated Admin and read-only Operator views, selected-camera preview, selected
MCAP retention, read-only OPC UA aggregation, preview-only Galaxy XR input, and isolated Isaac Sim
observation profiles.

## Functional requirements

1. `robot_agent` evaluates manifest-declared ROS topic presence, type, freshness, and minimum rate, then sends versioned heartbeats to the central control plane.
2. `fleet_control_plane` maintains agent/workcell state, persists meaningful transitions in SQLite, avoids duplicate events, exposes read APIs, and supports bounded soak reports.
3. `so101_readonly_adapter` reads four SO-101 buses and five MJPG cameras without motor writes, torque changes, action clients, decode/re-encode, or `/dev/videoN` identity assumptions.
4. OMY and RB5 use the same agent backend with separate manifests. RB5 joint observation uses only
   the vendor data channel on port 5001. Its D405 wrist camera is a separate read-only ROS publisher
   on an external Robot Agent PC; command, EEF, and control-box software modification remain absent.
5. `ui-admin` displays fleet health, manages revisioned registry/Task Run metadata, and reads the
   recording catalog, but has no robot command endpoint. `ui-operator` may create or stop only a
   bounded Task Run Capture Job backed by a fixed observation profile; it has no actuator, policy,
   robot-vendor, welding or feeder command endpoint.
6. Galaxy XR packages parse and preview targets only; no actuator command is published.
7. Isaac vendor profiles validate source assets without rewriting them, isolate runtime differences, and publish observation topics only on a simulation-specific domain.
8. OPC UA Phase 1 implementations must consume the pinned read-only model, NodeId, timestamp,
   StatusCode and SO-101 mapping contracts under `contracts/opcua/`; they may not invent writable
   application Variables, Methods or camera-frame Variables.
9. `sang_opcua_gateway` binds only to loopback in Phase 1, retains latest validated values only,
   preserves valid ROS source timestamps, marks fallback timestamps uncertain, and must remain
   independently stoppable without stopping the Agent or observation adapters.
10. `sang_mcap_recorder` records only profile-allowlisted observation topics to MCAP, preserves
    compressed image payloads without decode or re-encode, retains failed captures as `.partial`,
    publishes no application or actuator message, and exposes no robot or vendor command interface.
11. `robot_agent` reports a bounded, metadata-only local recording inventory. It must not follow
    symlinks or transfer MCAP, image, video, checksum, recorder-log, or local-path data to Central.
12. OPC UA Mapping Schema v2 declares each numeric feature's selector, order, data type, unit,
    semantic and finite valid range. The Agent and Gateway must bind the same canonical mapping digest
    into local health; a mismatch is rejected rather than projected.
13. Central requires local users or service tokens, role-based authorization, browser CSRF protection,
    and immutable security audit for writes. Remote HTTP requires TLS; Agent mTLS identity remains a
    separate trust boundary.
14. Central relays at most one explicitly selected, Task Run-assigned camera. It accepts no browser
    upstream URL, bounds time/size, and requires the Edge to pin the exact Central preview-client
    certificate in remote mode.
15. A finalized selected recording may be uploaded into the Central Object Store only after manifest,
    path, size, SHA-256, Agent, Task Run and assigned-asset validation. SQLite stores catalog metadata,
    never media payloads.
16. Central's independent OPC UA aggregator projects registry, health, Task Run and recording summaries
    with deterministic NodeIds, no application Methods and no writable application Variables.
17. Backup/restore must use SQLite online backup, copy only complete referenced objects, verify every
    hash, reject staging sessions, and restore only to new paths.
18. Capture Jobs must be durably linked to one ACTIVE Task Run, one mTLS-authenticated fixed-profile
    worker, one recording session and the exact assigned assets. Client request replay is idempotent,
    only one non-terminal job may exist per worker, UI refresh and Robot Agent restart must preserve
    state, and only Central artifact verification may advance a job to `COMPLETE`.
19. Central web playback exposes only SHA-256-verified, complete MCAP objects and indexed ROS 2
    `sensor_msgs/msg/CompressedImage` JPEG topics. The Catalog requests at most four selected camera
    frames on one record-time timeline, displays each nearest frame's actual skew, never downloads the
    full MCAP for playback, and adds no robot, vendor, policy, welder or feeder command route.

## Operational contracts

- Default Central endpoint remains loopback and authenticated. Remote Central requires HTTPS, an
  explicit Host allowlist and authentication. Remote Agent ingress/preview use mTLS, replay defense,
  allowlisting and certificate pinning.
- SO-101 manifests require the documented joint/camera rates and startup grace. Changes require synchronized manifest, agent, soak, and documentation updates.
- `rb5_readonly_adapter` may construct only `rbpodo.CobotData`. The command channel, operation-mode
  changes, speed changes, move/task APIs, and ROS command interfaces are forbidden.
- Hardware reconnect tests preserve torque-off physical safety and validation evidence.
- Isaac runners disable scene ROS action graphs, default to `ROS_DOMAIN_ID=99`, create no hardware-command subscribers, and bound any exercise motion to the documented simulation limit.
- Changes to the published SANG model namespace or v1 NodeId rules require a new contract major
  version; namespace indexes are never persisted as identity.
- Stable configuration belongs in versioned manifests and the SQLite registry; high-rate selected
  observations belong in immutable MCAP objects. A future vector database may contain derived
  embeddings/references only, and a future AAS layer maps durable asset semantics without replacing
  either authoritative store.

## MCP requirements

- ROS read tools are auto-approved for an explicitly connected rosbridge runtime.
- ROS publish, service/action, parameter mutation, and cancellation require user approval.
- Isaac documentation lookup is read-only. Direct Isaac control is enabled only in this project and `leisaac`; read tools are auto-approved, while every scene, joint, graph, physics, or simulation-state mutation requires approval.
- Direct-control tools for arbitrary script execution, reload, scene clear, deletion, generative 3D, and library refresh are not available.

## Acceptance checks

```bash
source /opt/ros/jazzy/setup.bash
colcon build --symlink-install
source install/setup.bash

PYTHONPATH="$PWD/src/sang_security:$PWD/src/fleet_control_plane:$PWD/src/robot_agent:$PWD/src/so101_readonly_adapter:$PWD/src/rb5_readonly_adapter:$PWD/src/sang_opcua_gateway:$PWD/src/sang_mcap_recorder:$PWD/sim:${PYTHONPATH:-}" \
/usr/bin/python3 -m pytest -p no:cacheprovider \
  src/fleet_control_plane/test src/robot_agent/test src/so101_readonly_adapter/test \
  src/rb5_readonly_adapter/test src/sang_opcua_gateway/test src/sang_mcap_recorder/test \
  sim/isaac_vendor/test_profile.py sim/isaac_vendor/test_scene_runner.py -q

PYTHONPATH="$PWD/sim" python3 -m isaac_vendor.validate
```

Physical SO-101 soak/reconnect and live OMY/RB5/Isaac runs are separate acceptance stages. Record the selected manifest, domain ID, hardware or asset hashes, duration, rates/events, safety state, and output report path. Never substitute simulator success for physical validation.
