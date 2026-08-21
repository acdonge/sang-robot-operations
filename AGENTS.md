# Repository Instructions

Read [`docs/requirements.md`](docs/requirements.md) and the current `README.md` before changing code. The parent workspace instructions also apply.

## Scope

This ROS 2 Jazzy workspace observes SO-101, OMY, RB5-850E, and simulation workcells through a central fleet/admin service. It also develops Galaxy XR input and preview-only retargeting, plus isolated Isaac Sim vendor-scene observation runners.

## Repository map

- `src/fleet_control_plane/`: agent registry, heartbeat/state transition logic, SQLite event history, API, and soak CLI.
- `src/robot_agent/`: manifest-driven ROS topic freshness monitor and heartbeat client.
- `src/sang_security/`: Phase 3 PKI, secret references, TLS and OPC UA security helpers.
- `src/sang_opcua_gateway/`: read-only ROS-to-OPC UA Edge projection.
- `src/so101_readonly_adapter/`: follower/leader joint and camera observation; no actuator write API.
- `src/rb5_readonly_adapter/`: RB5 vendor data-port observation; no command-channel client.
- `src/xr_teleop_interfaces/`, `xr_teleop_bridge/`, `xr_teleop_retarget/`, `xr_omy_retarget/`: Galaxy XR transport and target preview.
- `manifests/`: robot/topic/rate contracts.
- `ui-admin/`: read-only fleet console.
- `sim/isaac_vendor/` and `scripts/run_vendor_isaac_scene.sh`: OMY/RB5 asset validation and bounded simulation observation.
- `docker/`: ROS 2 Jazzy development/runtime environment.

## Non-negotiable safety contract

- `robot_agent` subscribes and reports; it does not command actuators.
- `so101_readonly_adapter` must not gain motor register writes, torque changes, action clients, or `send_action` behavior.
- `rb5_readonly_adapter` must use only `rbpodo.CobotData` on port 5001. It must not construct
  `rbpodo.Cobot`, change operation mode or speed, expose command interfaces, or call move/task APIs.
- Admin remains read-only and must not expose raw robot command endpoints.
- XR remains input/preview-only until a separately reviewed control-mode manager, command mux, TTL/deadman, and local safety gate exist.
- Remote Central/Agent traffic may use only the Phase 3 mTLS ingress and secure OPC UA profiles in `contracts/opcua/PHASE3_SECURITY.md`; Admin HTTP and camera preview remain loopback-only.

## Working rules

- ROS 2 Jazzy in `docker/run.sh` is the canonical workspace environment. Preserve package boundaries and message/interface compatibility.
- Manifests are operational contracts. Update tests and documentation when topic names, types, minimum rates, grace periods, or robot IDs change.
- Keep hardware observation and simulator observation distinguishable by manifest and `ROS_DOMAIN_ID`.
- Preserve user validation outputs; do not rewrite or delete `output/validation/` evidence.

## Claude CLI model policy

- Use the regular-plan `opus` model for ordinary design discussion, focused review, and implementation follow-up when a Claude cross-check is useful.
- Use `fable` (`claude-fable-5`) exactly once per Phase, after implementation and local verification, as the final broad review gate.
- Give the Phase-end Fable call the relevant diff, contracts, test evidence, unresolved risks, and next-Phase proposal in one high-density read-only prompt.
- Do not spend Fable calls on routine questions, repeated unchanged reviews, or work that has not passed local tests. A failed Fable gate may receive one focused Opus follow-up after fixes; reserve the next Fable call for the next Phase.
- Record the model, verdict, material findings, fixes, and remaining attended evidence in that Phase's validation document.

## MCP policy

- ROS MCP graph/topic/parameter reads may run automatically after the user connects a rosbridge endpoint. Publishing, service/action calls, parameter writes, or action cancellation require approval even if a target appears read-only.
- This trusted project enables restricted Isaac Sim control for vendor scene inspection. Start with scene/prim/state reads. Loading scenes, changing joints or physics, editing graphs, and play/pause/step require approval.
- Arbitrary Isaac script execution/reload, clear/delete, and generative tools are excluded. Never bridge the simulation control domain to physical hardware command topics.

## Verification

Use the focused command in `README.md` first, then build:

```bash
PYTHONPATH="$PWD/src/sang_security:$PWD/src/fleet_control_plane:$PWD/src/robot_agent:$PWD/src/so101_readonly_adapter:$PWD/src/rb5_readonly_adapter:$PWD/src/sang_opcua_gateway:$PWD/sim:${PYTHONPATH:-}" \
/usr/bin/python3 -m pytest -p no:cacheprovider \
  src/sang_security/test src/fleet_control_plane/test src/robot_agent/test src/so101_readonly_adapter/test \
  src/rb5_readonly_adapter/test src/sang_opcua_gateway/test \
  sim/isaac_vendor/test_profile.py sim/isaac_vendor/test_scene_runner.py -q

colcon build --symlink-install
```

Run hardware soak/reconnect, XR deployment, `--exercise`, or `--publish-ros` only when explicitly requested and with the safety prerequisites in `README.md`.
