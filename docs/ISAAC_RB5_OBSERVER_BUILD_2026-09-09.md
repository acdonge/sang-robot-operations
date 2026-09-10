# Isaac RB5 Observer-Only Pilot

## Planner
### Goal
Build a separate Unity 6000.5.6f1 Linux observer that mirrors only measured Isaac RB5 state.
### Scope for this iteration
New project `unity/IsaacRb5Observer`, new observer/runtime and Editor build code, and this report.
No legacy project edits, existing Player lifecycle changes, Isaac/backend changes, or external packages.
### Tasks
1. Read scene and contracts; copy RB5 transform/mesh hierarchy into a new clean scene.
2. Validate the confirmed file snapshot; apply named measured joints and read back mirror errors.
3. Run rejection/mapping tests and actual Editor Linux build; inspect scene and Player assemblies.
### Acceptance Criteria
- Only observer application code ships; no Assembly-CSharp, legacy sender, executor, or URDF controller.
- Exact schema/source/session/epoch checks; stale, malformed, reordered and reset data cannot move visuals.
- No autonomous motion, extrapolation, articulation drives, command protocol, or network transport.
- Six named joints use radians-to-degrees rotation and independent transform angle readback.
- Actual Editor 6000.5.6f1 compiles and builds; tests and evidence are reported honestly.
### Risks / Notes
- Local pilot, not signed production attestation. Legacy source is not a pinned Git repository.
- Only joint-space mirroring is claimed; this does not prove Isaac/Unity world-frame or TCP registration.
- Session is launch-pinned with `SANG_TWIN_SESSION_ID` (32 lowercase hex digits); `SANG_TWIN_EPOCH` defaults to 0.
- Epoch/session changes require an explicit new observer launch. No automatic reset/rebinding.
- The receiver owns domain 98, LOCALHOST, `/sang/twin/rb5/joint_states`, optional camera, and `/clock`.
- Unity reads only `/tmp/sang-isaac-rb5-twin/observer/telemetry.json`; it never reads Isaac files at runtime.
### Developer Instruction
Implement only the new isolated project, generate a whitelist-only visual scene, and verify the built artifact.

## Inspection Findings
- `config/build_roles/unity_observer.json` forbids `Assembly-CSharp`.
- Legacy `Assets/Scripts` has no asmdef; sender guards use `!SANG_SIMULATION_PLAYER`.
- That define excludes two physical senders, not the scenario executor/agent and runtime bootstraps.
- Adding an observer scene or disabling behaviours in that project would not establish assembly isolation.
- The new project therefore does not reference the existing `Sang.Observer` package or legacy source assemblies.
- Exact installed Editor: `/home/sa08050/Unity/Hub/Editor/6000.5.6f1/Editor/Unity`; `-version` returned 6000.5.6f1.
- Source `rb5_850e/link0/link1/.../link6` has joint names base, shoulder, elbow, wrist1, wrist2, wrist3.
- Source URDF importer maps ROS vector `(x,y,z)` to Unity `(-y,z,x)` and negates the revolute axis.
- Thus base/wrist2 rotate about local -Y, and shoulder/elbow/wrist1/wrist3 about local +X.
- RB5 is preferred over OMY: direct six-axis name matching without OMY gripper/mimic-joint dependencies.

## Confirmed Telemetry
Required fields: `schema_version=1`, `runtime_kind=isaac-sim-executor`, `profile_id=rb5`,
`asset_id=isaac-twin-rb5`, `scene_sha256=5837e8eb41c67024a4d461f07e3d63dc53bde338209fd64c6c238ceb187972ce`,
`session_id`, `epoch`, `sequence`, `sim_time_ns`, `received_at_unix_ms`,
`physical_output_connected=false`, `joint_names`, `joint_positions_rad`.
Names resolve by name, not incoming array order. Exactly all six names are required once.
All timestamps/counters are nonnegative int64; receive time is positive. Joint values must be finite within +/-3.14 rad.
Maximum receive age: 500 ms; maximum future timestamp tolerance: 100 ms. Sequence and simulation time must advance.
Monotonic local elapsed time also enforces staleness when wall time jumps. Rejected inputs never advance the watermark.
The ROS receiver, not Unity, obtains source identity/session/epoch from separate ROS state metadata.
The optional JPEG is not displayed in this iteration.

## Developer
### Summary
Implemented and verified the separate observer. Only the new project and this report were changed.
The legacy project was read as text/mesh input, never opened in an Editor or altered.
Its scene and 14 mesh/meta source hashes remained unchanged across the build.
### Files Changed
- `unity/IsaacRb5Observer/Assets/Observer/Runtime/IsaacRb5Mirror.cs`: strict snapshot gate, named transform mirror, measured readback and status.
- `unity/IsaacRb5Observer/Assets/Observer/Editor/IsaacRb5ObserverBuild.cs`: visual-only extraction, 44 tests, strict Linux build and compiled-type/IL audit.
- Separate runtime and Editor asmdefs, new `build-profile.json`, built-in-only package manifest/lock and project settings.
- Generated `Assets/Observer/Scenes/Rb5Observer.unity`, seven copied DAE meshes and their metadata, and a built-in-renderer material.
- `launch_observer.py` and `test_launcher.py`: bounded explicit rebind launcher and standard-library validation tests.
### Commands Run
```bash
"/home/sa08050/Unity/Hub/Editor/6000.5.6f1/Editor/Unity" -batchmode -nographics \
  -projectPath "/home/sa08050/app developer/v2_ws/unity/IsaacRb5Observer" \
  -executeMethod Sang.IsaacRb5Observer.Editor.IsaacRb5ObserverBuild.BuildLinux -quit \
  -logFile /tmp/sang-isaac-rb5-observer-build-final.log

cd "/home/sa08050/app developer/v2_ws/unity/IsaacRb5Observer"
python3 -m unittest -v test_launcher
python3 launch_observer.py --check-only
python3 launch_observer.py --duration-seconds 12
```
### Test / Build Results
- Exact Editor 6000.5.6f1: final process exit 0; Linux StrictMode build PASS; no C# warnings/errors in final log.
- 44 C# acceptance checks PASS, including truncated/trailing/duplicate/unknown JSON, required fields/types, identity, six named joints,
  range/nonfinite values, timestamp/sequence rollback, timeout, reset epoch, incoming name reordering, signed axes and transform perturbation.
- Source hierarchy checks PASS: six mapped joints, correct imported axes/zero rotations, eight mesh renderers, plausible dimensions.
- Clean scene contains only Transform, MeshFilter, MeshRenderer, Camera, Light and the new IsaacRb5Mirror component.
- Compiled audit PASS: 113 framework/engine/application DLLs scanned; only one application assembly, `Sang.IsaacRb5Observer.dll`.
- Application assembly references only netstandard, bundled System.Text.Json 8.0.0, Unity Core, JSONSerialize and IMGUI.
- No Assembly-CSharp, legacy sender, scenario agent/controller, executor, or URDF-controller assembly/type in the build.
- The new observer method IL has no network, process-start, reflection, physics-body or legacy-executor reference.
- Observer DLL SHA-256: `1909f1166f8e93cfa1b4c7f3564280786b9811ba092698516f9cd9c88914b751`.
- The four-minute windowed live Player accepted real ROS-receiver telemetry, not direct Isaac state files.
- Independent 50-status/10-second sample captured base movement up to 0.09998510988 rad and back near zero;
  every sample was fresh, maximum transform-readback mirror error 1.02498258076e-8 rad.
- Reset proof: fresh receiver epoch 1 was rejected by epoch-0 Player; last accepted sequence stayed 12286,
  state was stale-frozen, reason session-or-epoch-reset, and rendered pose stayed frozen.
- Runtime socket-descriptor check found zero matching TCP/UDP/IPv6 internet sockets; desktop/GPU/audio local sockets remain.
- Explicit launcher rebind at epoch 1 restored mirroring: sampled sequence 8047, 248 accepted, 0 rejected,
  maximum mirror error 1.13686274445e-13 rad. This second launch was bounded to 12 seconds.
- Launcher validation tests cover fresh pinning, ten invalid input cases, and duplicate fields.
- Window capture visually confirms the standalone RB5 mesh and observation-only status, including reset-frozen state.
### Open Issues
- Main agent owns Isaac execution, UI Run evidence, ROS domain/metadata provenance and real-time-factor corrections.
- Mono Player includes generic Unity/.NET physics/network libraries. This is **application code/scene isolation**, not absence of all
  generic engine APIs, an OS network sandbox, or a signed production artifact. No robot physics bodies are created or retained.
- Joint-space error does not validate global coordinate registration, TCP accuracy, physical behavior or task safety.
- Optional ROS JPEG display is not implemented.

## Launch And Reset
From the project directory, run `python3 launch_observer.py --duration-seconds 240`.
The launcher reads only the fixed local ROS-receiver snapshot, validates source identity and a <=500 ms receive age,
and pins that snapshot's uuidhex session and epoch in the child environment. The existing private mode-0700 receiver root
is required; files must be regular, user-owned and not world-writable. Group bits below that private root are allowed.
No file path or executable is accepted from the snapshot. No package installation or existing Player/process management occurs.
The launcher serializes its own launches with a lock and terminates only its own child after the requested 1..3600 seconds.
Ctrl-C/SIGTERM also clean up that child. `--check-only` validates without starting anything.

After Isaac Reset or session restart, the existing observer deliberately freezes. Close/wait for that observer to exit,
then explicitly run the launcher again after fresh receiver telemetry is available. There is no in-Player reset/rebind control,
no automatic new-session/epoch adoption and no action-command path. A stale final status file is not evidence of a running Player;
consumers must check `reported_at_unix_ms` freshness as well as `fresh`.

## Evidence Paths
All paths below are relative to `unity/IsaacRb5Observer/Builds/Evidence/`:
- `build-result.json`, `tests-passed.txt`, `player-assembly-audit.txt`, `source-sha256.txt`, `scene-components.txt`.
- `sang-isaac-rb5-observer-build-final.log`, `sang-isaac-rb5-observer-live.log`.
- `live-observer-sample.json`, `live-network-audit.json`, `reset-epoch-rejection.json`, `explicit-rebind-status.json`.
- `sang-isaac-rb5-observer-window.png`, `sang-isaac-rb5-observer-reset-window.png`.
Built executable: `unity/IsaacRb5Observer/Builds/Linux/IsaacRb5Observer.x86_64`.
Observer report: `/tmp/sang-isaac-rb5-twin/observer/mirror-status.json`.

## Evaluator
### Verdict
PASS
### Summary
Observer scope passes real Editor compilation, isolated scene/build checks, live measured-motion mirroring, reset freeze,
and explicit startup-only rebind verification. This does not promote the pilot to a signed production/safety-qualified build.
### Critical Issues
- None within the observer-only pilot scope.
### Non-Critical Issues
- Generic engine APIs remain; no OS sandbox or signed production attestation. Optional camera display omitted.
### Revision Instruction
No observer revision required for this iteration. Any new receiver identity/schema or automatic epoch adoption requires explicit review.
