# Phase 4 M4 Multi-Robot Mapping Schema v2 Result

Date: 2026-08-21
Scope: OMY and RB5-850E physical/Isaac Sim read-only OPC UA mappings
Result: PASS (focused validation)

## Implemented

- Added Mapping Schema v2 contracts for `omy_01`, `omy_sim_01`, `rb5_850e_01` and
  `rb5_850e_sim_01`.
- Bound every state and observed-action feature to its exact manifest source, ROS topic, joint-name
  selector, dense order, OPC UA `Float`, radian unit, finite range and semantic.
- Preserved distinct physical/simulation `robot_id`, `real_or_sim`, ApplicationUri and NodeId roots.
- Added explicit camera FPS to all four manifests. Physical OMY and RB5 retain their attended USB
  profile of `848x480 @ 10 fps` with an `8 Hz` health floor. Isaac profiles retain their configured
  `30 fps` target and `25 Hz` health floor.
- Kept camera bytes out of OPC UA. The mapping exposes configuration, health, sequence, timestamps
  and on-demand stream URI only.

No command subscriber, actuator API, OPC UA Method or writable application Variable was added.

## Joint-Bound Provenance

### OMY F3M

- Primary source: ROBOTIS `open_manipulator` repository,
  `open_manipulator_description/urdf/omy_f3m/omy_f3m.urdf`.
- Pinned revision: `5adfd9cc91240c7cb6bedad6d5ebbd29188aa59d`.
- Pinned file SHA-256: `a1000b876724ff79943a2f2f16362b02cfdb8fa7eb4b8ba39fdd5ac300811420`.
- `joint1` through `joint6`: `[-2*pi, 2*pi]` rad.
- `rh_r1_joint`: `[0, 1.13514578304]` rad.

Source repository: <https://github.com/ROBOTIS-GIT/open_manipulator>

### RB5-850E

- Primary source: Rainbow Robotics RB Series online manual, Appendix A system specifications.
- Manufacturer range: `+/-360 deg` for all joints except elbow, which is `+/-165 deg`.
- Mapping conversion: `+/-2*pi` rad and `+/-2.8797932657906435` rad for `elbow`.
- The adapter reads measured `jnt_ang` values from the vendor data-only port and converts degrees to
  radians before publishing `/rbpodo/joint_states`.

Source manual: <https://rainbowrobotics.github.io/rb_cobot_docs/ko/manual/appendix/system_specifications>

The RB5 bounds are telemetry quality bounds, not motion commands or a replacement for the controller's
safety, collision and joint-limit enforcement. The bundled scene URDF's generic `+/-3.14` limits are
not used as the physical RB5 authority.

## Coverage

| Profile | State | Observed action | Cameras | Sources | Topics |
|---|---:|---:|---:|---:|---:|
| `omy_01` | 7 | 7 | 2 | 4 | 4 |
| `omy_sim_01` | 7 | 0 | 2 | 3 | 3 |
| `rb5_850e_01` | 6 | 0 | 1 | 2 | 2 |
| `rb5_850e_sim_01` | 6 | 0 | 2 | 3 | 3 |

Physical and simulated versions of each robot share the same state feature names, order, unit and
bounds. Camera contracts remain runtime-specific because the physical USB and Isaac render pipelines
have different resolutions and rates.

The physical OMY `848x480 @ 10 fps` camera contract is the user-approved USB 2.0 deployment profile,
not a manufacturer default. It must be checked against both live OMY camera publishers before that
physical Agent is enabled. Likewise, the RB5 elbow bound must be correlated once with live `jnt_ang`;
any value outside the manufacturer-published `+/-165 deg` range is treated as a telemetry contract
fault rather than silently widening the safety-backed bound.

## Verification

- All four contracts loaded through the production `load_gateway_contract` fail-closed validator.
- Canonical Agent/Gateway mapping digest binding succeeded for each robot identity.
- Focused manifest, mapping-v2 parser and multi-robot coverage suite: `37 passed`.
- These four mappings are contract artifacts in this slice; no OMY/RB5 launcher was switched to them
  and no physical OMY/RB5 Gateway rollout is claimed.
- Full regression, ROS package build, live Central/API/UI checks and independent review are the M5
  completion gate and are recorded separately.

## Deferred Gate

The five-runtime concurrent shadow, one-Edge failure isolation and two-hour final soak are explicitly
outside this iteration. M4 proves static contract correctness; it does not claim the final concurrent
Phase 4 conformance gate.
