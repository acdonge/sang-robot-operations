# SANG Fleet Workspace Requirements

Document authority: this file defines mandatory product and safety requirements.
Machine-readable interfaces are versioned under `contracts/`. Internal implementation
plans and review records are maintained separately from this public source tree.

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
20. Unity Safety Phase 0 uses the versioned command-free contract under `contracts/safety/`. Central
    accepts only Task Run-assigned asset assessments, keeps one latest row plus bounded meaningful transitions,
    and exposes read APIs. `UNSAFE`, `UNKNOWN`, and `task_pause_candidate` remain Shadow evidence and
    must not publish, invoke, or imply a physical Task Pause, robot, vendor, welder, feeder, or E-stop
    command.
21. IITP Scenario 1 is registered from a versioned Task bundle with simulation assets kept separate
    from physical assets. Unity may send only `synthetic` or `replay` snapshots to a loopback Safety
    Edge; the Edge uses the reference evaluator and forwards assessments only. Its workflow is mobile
    delivery, OMY pre pick/place, RB5 WAAM, OMY post pick/place, and mobile return. Activating the
    supplied Unity UDP Sim-to-Real launchers remains outside this integration. The Unity Reporter must
    refuse to run whenever either supplied Sim-to-Real environment arm flag is active.
22. Heterogeneous robot control development and every later physical release must follow
    the authority boundaries below and the versioned contracts under `contracts/orchestration/`:
    Central owns intent, audit,
    reservations, authorization and bounded delegation grants; a Processing-PC Cell Orchestrator owns
    capability-based DAG sequencing and recovery; only a local Device Agent may hold a vendor command
    handle. G1-G6a software/disconnected-HIL implementation does not authorize physical motion.
    Scenario 1/2 remain conformance fixtures. Existing read-only packages retain zero command sinks,
    and no physical manifest may set `command_sink_present: true` before the documented G7 attended
    commissioning gate.
23. Workflow Studio authoring uses mutable, revisioned Drafts and atomically publishes an immutable
    Task Definition plus `WorkflowDefinitionV2` and SHA-256 linkage. Published versions have no update
    or delete path; edits and Task copies target a new version or new identity. Manager UI execution is
    limited to queueing one checked-in-profile, output-disconnected Unity `SIMULATION` Run for a
    separate local Worker. Central HTTP must not execute Unity, spawn scripts, accept runtime paths, or
    expose physical robot/process commands. Simulation Runs pin the exact publication and profile
    digests, reject stale/non-isolated Workers, and become `INTERRUPTED` after Worker lease expiry
    without automatic redispatch.
24. Normal Workflow editing is robot-action based. Each UI-visible action belongs to one robot type
    and pins a versioned logical action, Skill capability, fixed intent, safe editable inputs, timeout,
    allowlisted runtime, registry-owned script reference, entrypoint, and SHA-256 digest. The Manager
    chooses only Robot, Action, order, and declared safe inputs; Central derives roles, Step IDs, fixed
    operations, and a linear dependency chain. The browser cannot upload source, submit paths, execute
    arbitrary scripts, or author endpoints, ROS topics, joints, vendor calls, or physical bindings.
25. The separate Admin Action Library may accept one bounded UTF-8 `.cs` or `.py` source file into a
    mutable Action Draft. Central stores the bytes in a content-addressed data directory, returns only
    file metadata and SHA-256, and never imports, compiles, or executes uploaded source. Source Gate
    statically enforces the registered Unity C# or ROS 2 Python simulation contract and rejects file,
    process, network, native-library, reflection, dynamic-code and deployment APIs. Simulation Gate
    passes only when the exact script digest is already installed in a configured output-disconnected
    adapter and an immutable completed Simulation Run contains the matching Step, Capability and fixed
    parameters. Both Gates must pass before immutable publication; only published versions appear in
    normal Workflow authoring. Action Library v1 cannot publish physical Actions or modify Unity,
    robot, vendor, ROS topic, endpoint, joint, welder or feeder configuration.
26. AAS Asset Package Phase A0 is a command-free contract layer under `contracts/assets/`. Central
    SQLite remains the operational identity source of truth; global/AAS identity is read-only
    projection metadata, and model bytes remain immutable Object Store objects referenced by exact
    size and SHA-256. Scene Binding v1 accepts simulation assets only, requires stable Unity binding
    IDs, exact package digests, articulation/frame/controller/Reset/outcome contracts, and literal
    `physical_output_connected=false`. A PASS Simulation Qualification pins the exact Scene, package,
    Action source/build, adapter/orchestrator build, validation profile, Run, report and isolation
    evidence. A0 introduces no AAS Repository, DB migration, Unity loader, ROS 2 transport, OPC UA
    write, browser path, vendor endpoint, high-rate payload, or physical command sink.
27. The frozen SANG OPC UA v1 tree and Central aggregate are compatibility projections, not an OPC UA
    Robotics 1.02 conformance claim. Any official projection must import pinned Core/DI/IA/Robotics
    NodeSets, preserve the official types and NodeIds, provide truthful mandatory controller, axis,
    power-train, safety, identification, unit and range data, declare a per-robot profile/evidence
    state, and preserve the existing v1 tree during migration.
28. Raw D405, D455, OTOS, MJPEG and render-camera streams are not modeled as OPC UA Machine Vision
    systems. That Companion Specification may be adopted only for a complete processing system with
    declared modes, jobs/recipes or configuration, results, events and lifecycle. Raw frame/video
    payloads continue to use the preview/ROS 2/MCAP paths.
29. Before implementing an AAS projection, AASX export or Repository, the project must pin exact
    current IDTA metamodel, API, serialization and package revisions and define one-way ownership from
    SQLite/Object Store to each Submodel. A0 terminology and identifiers remain internal AAS-ready
    metadata until that mapping passes conformance-oriented tests.

## Operational contracts

- Default Central endpoint remains loopback and authenticated. Remote Central requires HTTPS, an
  explicit Host allowlist and authentication. Remote Agent ingress/preview use mTLS, replay defense,
  allowlisting and certificate pinning.
- SO-101 manifests require the documented joint/camera rates and startup grace. Changes require synchronized manifest, agent, soak, and documentation updates.
- `rb5_readonly_adapter` may construct only `rbpodo.CobotData`. The command channel, operation-mode
  changes, speed changes, move/task APIs, and ROS command interfaces are forbidden.
- Hardware reconnect tests preserve torque-off physical safety and validation evidence.
- Isaac runners disable scene ROS action graphs, default to `ROS_DOMAIN_ID=99`, create no hardware-command subscribers, and bound any exercise motion to the documented simulation limit.
- The separate `sim/isaac_twin` pilot uses domain 98 with LOCALHOST discovery and
  publisher-only ROS feedback. Its checked-in schema-3 Workflow adapter reuses the
  durable simulation queue, not the hardware observer. The RB5 pilot permits only
  base +0.1 rad and return-to-zero. The separate `sim/isaac_scenario1` extension
  permits only its pinned five-stage recorded-path workflow and explicit virtual
  carrier/grasp fixtures; it does not qualify real mobile/grasp/welding dynamics.
  Both require actual measured completion predicates. Worker authority expires within two wall
  seconds, regardless of simulation time. Reset clears run state without moving
  joints. Unity consumes ROS-derived measurements in a separate observer build and
  never executes motion in this topology. Pilot success does not authorize physical
  control, prove hard real-time behavior, or qualify collision/safety assessment.
- Changes to the published SANG model namespace or v1 NodeId rules require a new contract major
  version; namespace indexes are never persisted as identity.
- Stable configuration belongs in versioned manifests and the SQLite registry; high-rate selected
  observations belong in immutable MCAP objects. A future vector database may contain derived
  embeddings/references only, and a future AAS layer maps durable asset semantics without replacing
  either authoritative store.
- Workflow authoring payloads are viewer-readable contracts, not a secret store. Bodies, nesting and
  collections are bounded, and credentials plus deployment-specific connection details are rejected.

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
