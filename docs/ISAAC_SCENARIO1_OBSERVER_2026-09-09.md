# Isaac Scenario1 Passive Observer

## Planner
### Goal
Build a separate passive multi-asset Unity observer and export the original Scenario1 layout.
### Scope for this iteration
Only `unity/IsaacScenario1Observer/**`, `sim/isaac_scenario1/layout.json`, and this report.
No backend, controller, Isaac engine, existing observer, or original Unity project changes.
### Tasks
1. Extract source transforms, mesh dependencies, joint axes, carrier endpoints, and hashes.
2. Construct and audit an isolated visual-only scene without importing execution code.
3. Build with Editor 6000.5.6f1 and test isolation/layout/pinning; no live Player launch.
### Acceptance Criteria
- Source scene and consumed assets unchanged; layout independently reproducible.
- Actual source coordinates retained, including camera, carrier, tables, plate, both robot bases.
- No retained physics/body/controller/vendor/executor components or application assemblies.
- Real Linux build and bounded-launch tests pass.
- Snapshot parser waits for main's shared telemetry contract; no guessed protocol.
### Risks / Notes
- Saved carrier x=-1.283 is not runtime dock x=-1.25. Both are recorded distinctly.
- World-coordinate conversion is a defined mapping, not proof of registration to existing USD assets.
### Developer Instruction
Read source as data, clone only allowlisted visual components, and keep runtime telemetry gated.

## Milestones
- [x] M1: reproducible layout JSON with source hashes.
- [x] M2: isolated multi-asset scene and strict Linux build.
- [x] M3: launch/isolation tests and shared schema-2 telemetry implementation.

## Current State
Offline observer implementation is built and tested. Main supplied schema 2 during this task.
Main supplied final candidate manifest SHA-256
`c988f5ecdd0d331ee338619af369efcafdc098df400488a97463855b1a69821d`.
The digest and all 136 listed source hashes were independently verified before pinning it in
`build-profile.json.scene_sha256`. The pinned StrictMode Linux rebuild passed with 174 Editor checks,
a 113-assembly isolation audit, and 14 launcher tests. The same digest was independently found in
the generated scene and the built `IsaacScenario1Observer_Data/level0` scene data. All 136 manifest
sources still matched after the build. The launcher verified the pinned profile and observer DLL
against the fresh build evidence without reading telemetry or starting a Player.
No live Player or Isaac was launched by the observer task.
Do not change the frozen layout after main pins the manifest without notification.

### Manifest Pin Follow-Up
The final candidate supersedes
`93190d3275ca9b26849332ff5734c76d92377a7ed56429621b34d053f6fbb9fb`
after main reported four state/finite-value fixes from final review and a fresh geometry-preflight
PASS. The previous camera correction to `camera.set_focal_length(1.8)` remains main-owned.
This observer follow-up changes no camera, runtime, or layout logic.

Main reported candidate 1 completed all five steps in 231 seconds, including a 28-second HOLD,
with Reset PASS; its mirror reported 1,145 samples and maximum joint error 2.97e-7 rad.
Main stopped the old observer and owns candidate 2 plus repeat UI validation after register gate v3.
These are main-supplied observations, not live tests performed by this observer task or proof of
the replacement build's live result. Any further manifest change requires another explicit pin
and rebuild.

This iteration changes only the observer build profile, regenerated observer scene/build evidence,
and this report. Runtime parser, four asset identities, four measured root poses, `frame_id=cell`,
named joints, and session/epoch/freshness rules remain unchanged. A four-asset test snapshot using
the real pinned digest passed the launch validator; the compiled scene contains that exact pin.

The independently checked manifest declares `gripper_table_contacts=excluded_for_kinematic_fixture`.
Main reports physics geometry preflight PASS after explicit source-path retargeting and the declared
gripper/table-pair exclusion. The observer task did not rerun physics and makes no contact,
grasp-dynamics, or physical qualification claim. Main alone will launch after the receiver is ready.
Build profile SHA-256: `7e63c9c0d71c116cbe6e0867da8be390652a71efa044edb1b1b6bbe6ce6adf64`.
Pinned build log: `unity/IsaacScenario1Observer/Builds/Evidence/pinned-build.log`.

### Main-Run Acceptance Follow-Up

The main task subsequently ran this exact pinned build during two Web-started
five-stage Runs in the same Isaac session. Both completed and Reset/gate v3
passed. The observer was explicitly rebound to epoch 1 before the repeat.
Source-to-Unity probes matched 1,071 and 1,075 samples, plus a 288-sample completion
window, with zero failures. See `ISAAC_SCENARIO1_EXECUTION_2026-09-09.md` for Run
IDs, measurements, shutdown evidence and the uncorrected Isaac base-visual issue.
These are main-task live results, not additional offline reviewer tests. They do
not qualify contact-based grasp, mobile dynamics, whole-scene visual fidelity or
physical robot safety.

## Confirmed Layout
Source: `Scenario1_app/Assets/Scenes/SampleScene.unity`, SHA-256
`9a981786f21e2cc53cbcdccb58553e7ad2d6c324a528c348b7c7e107a1cb16b9`.
Frozen layout SHA-256: `a3ddf93aa3f1883b74077168d293920e0f519d92f6dc0c76dfa08b7500a1d668`.

All coordinates below are source Unity Y-up meters, not TCP poses or new placements.

| Frame | Position | Rotation XYZW |
|---|---|---|
| OMY model root | (-1.3183875, 0.90700036, -0.38640898) | (0, -0.7071068, 0, -0.7071068) |
| RB5 model root | (-0.31799996, 0.904, -0.348) | (0, -0.7071068, 0, 0.7071068) |
| Saved carrier | (-1.283, 0, -0.818) | (0, 0, 0, 1) |
| Runtime carrier start | (-2.5, 0, -0.818) | (0, 0, 0, 1) |
| Runtime carrier dock | (-1.25, 0, -0.818) | (0, 0, 0, 1) |
| Plate table target | (-0.819, 0.91, -0.240) | (0, 0, 0, 1) |
| Original main camera | (0, 1.45, -3.2) | (0.08715574, 0, 0, 0.9961947) |

Source plate size is (0.07, 0.01, 0.07). Saved attach height is 0.90999997; source runtime
arithmetic describes 0.91. Carrier path is the straight source X segment from start to dock,
with the reverse endpoints for return. The observer does not implement its timing or interpolate it.

The imported mesh origins are not table centers. Copied-mesh world renderer bounds:

| Table | Center | Size | Top Y |
|---|---|---|---|
| OMY | (-1.14899993, 0.452499896, -0.262393534) | (1.01000023, 0.904999912, 0.610000014) | ~0.905 |
| RB5 | (-0.323606402, 0.451499909, -0.143999994) | (0.609999835, 0.904999793, 1.00999999) | ~0.904 |

These are full mesh AABBs, not measured solid tabletop thicknesses or collision qualification.
Main may use explicitly labeled Isaac proxy boxes matched to the top surfaces; this observer retains
the original visual meshes. `Builds/Evidence/table-bounds.json` contains machine-readable values.

ROS cell to Unity: position `(-y,z,x)`, quaternion `(qy,-qz,-qx,qw)`.
Inverse: position `(z,-x,y)`, quaternion `(-qz,qx,-qy,qw)`.
Rotation matrices obey `R_ros = C R_unity C^T`; do not rearrange Euler angles.
The mapping defines a source-aligned frame, not automatic registration of an arbitrary USD root.
Both arms have local axes `[-Y,+X,+X,+X,-Y,+X]`. OMY measured `rh_r1_joint` has local `-Z`;
the source URDF's `rh_l1`, `rh_l2`, and `rh_r2` mimic it with multiplier 1 and offset 0,
using local axes `+Z,-Z,+Z`, respectively. Source zero transforms are retained.

## Gripper Evidence
`Builds/Evidence/gripper-geometry.json` contains the four exact source body frames, zero local
poses/scales, own-visual centers, saved-home subtree renderer centers, and child/parent joint anchors.
Own-visual centers in each body's Unity-local coordinates, rounded:

| Joint | Own Visual Center |
|---|---|
| rh_l1 | (0.0300447941, 0.00176383206, -0.0000000382) |
| rh_l2 | (0.00704999641, 0.0126128420, 0.0000000075) |
| rh_r1_joint | (-0.0300448835, 0.00176387304, 0.0000000391) |
| rh_r2 | (-0.00705001131, 0.0126129165, -0.0000000149) |

Important: the current source `IitpOmyPickPlaceDemo.GetCenter()` uses combined descendant collider
bounds first, and renderer bounds only as fallback. The l1/r1 subtree includes the moving l2/r2 child,
so a saved-home subtree center is not a constant FK offset. The observer contains no colliders and
does not claim equivalence to the source's collider-based grasp qualification. Each own visual has
one rigid renderer; these own-body offsets are provided separately for main's registration checks.
The home renderer-fallback midpoint is approximately (-1.31845069, 1.66050041, -0.686846495).
The average of four own-visual centers is approximately (-1.31844285, 1.66050041, -0.677847296),
about 9 mm from that subtree fallback at home. This pose-dependent distinction matters when
interpreting main's 5 cm grasp gate; neither renderer average establishes collider equivalence.
Sampler source SHA-256 is recorded in the gripper evidence, not added to the frozen layout.

### First Physics Preflight Comparison
At main's request, `CompareGeometryPreflight` applied the 16 measured values from
`/tmp/sang-isaac-scenario1/geometry-preflight.json` to the isolated saved visual hierarchy in the
Editor only. Input SHA-256: `becf8654bb5218257a315c5bc20cd7c77b12b9b54f87c0cbcd2ab81f99468f8a`.
No Play Mode, physics, Player, Isaac operation, controller inspection, or scene save was performed.

- Source RB5 link0 is identity under the model root. All six saved local joint zero rotations are
  identity. Parent-local positions in Unity are link1 (0,0.1692,0), link2 (0,0,0), link3 (0,0.425,0),
  link4 (0,0.39199996,0), link5 (0.1107,0.11070001,0), link6 (0,0,0), TCP (0.0967,0,0).
- Zero-pose source TCP and measured Isaac TCP agree within 0.71 micrometers.
- At the first measured RB5 path pose, source TCP ROS position is
  (-0.370034337,-0.237968326,0.988229036), versus Isaac
  (-0.370034443,-0.237968413,0.988229419): difference 0.41 micrometers.
  The wrong-side WAAM location is reproduced by the source hierarchy for those measured joints;
  it is not an Isaac-versus-Unity sign/root/zero-origin discrepancy in this sample.
- OMY own-visual midpoint differences are 0.059 mm at pre-grasp, 0.050 mm at pre-release,
  1.163 mm at post-grasp, and 0.057 mm at post-return. Source FK also reproduces the pre-release
  center height near 0.97068 m. These geometry/frame differences do not explain a 5 cm gate failure.
- OMY's additional visual finger joints are derived from the one measured gripper joint, as required
  by the supplied telemetry contract. Their readback checks nominal visual mapping, not independent
  measurement of Isaac's three mimic-joint tracking errors.

Results: `Builds/Evidence/geometry-preflight-comparison.json`. Main owns investigation of recorded
path/goal semantics and physics tracking/collision issues; no correction is applied in the observer.

## Shared Telemetry
Fixed input: `/tmp/sang-isaac-scenario1/observer/telemetry.json`.
Schema 2 requires exact top-level and asset-level field closure; exactly four unique assets:
`isaac-scenario1-omy`, `isaac-scenario1-rb5`, `isaac-scenario1-carrier`, `isaac-scenario1-plate`.
OMY has `joint1`..`joint6`,`rh_r1_joint`; RB5 has base, shoulder, elbow, wrist1, wrist2, wrist3.
Carrier and plate joint arrays are empty. Names and assets resolve by name, not incoming order.
All numbers must be finite and representable as Unity floats; quaternions must have squared norm
within 0.001 of 1. Counters are nonnegative int64; receive time must be positive, at most 500 ms old,
and at most 100 ms ahead. Sequence and simulation time strictly advance; receive time cannot regress.
A monotonic local timer also freezes stale state despite wall-clock changes.

The trusted scene digest is compiled into the generated scene from the checked-in build profile,
never accepted from telemetry or environment. Launcher pins session and epoch once through
`SANG_SCENARIO1_SESSION_ID` and `SANG_SCENARIO1_EPOCH`; no automatic rebind is implemented.
Malformed, stale, mismatched, reset, or reordered samples do not advance the last valid watermark
or move visuals. One invalid asset rejects the whole sample. Phase and owner are display labels only.
Plate is detached once while preserving its saved world pose; no carrier/OMY parent can animate it.

Output: `/tmp/sang-isaac-scenario1/observer/mirror-status.json`, schema 2. Each asset reports measured
and rendered ROS world position/quaternion, position and orientation error, named measured/rendered
joints, joint orientation error, and all visual/mimic joint errors. Rendered joint angles are signed
principal angles; orientation errors are modulo a full revolution, not multi-turn encoder errors.
Readback uses actual Transform
values, not cached input or drive targets; off-axis perturbations are detected. Reports include
session, epoch, scene digest, accepted sequence/time, phase/owner, receive/report timestamps and
freshness. Consumers must check report timestamp freshness, not only the last `fresh` flag.

## Developer
### Summary
Created a separate project with 52 pinned visual source assets and no legacy application dependencies.
Original source is parsed as data, never opened in an Editor. Scene retains exact source transforms;
the original camera remains a disabled reference, and a separate source-world overview camera frames
the scene and carrier start. Carrier and plate use saved source colors; robot/table materials use
the built-in renderer. Optional JPEG display is omitted.
### Files Changed
- `sim/isaac_scenario1/layout.json`: 30 source frames, 23 fixed/revolute joint entries, two tables,
  carrier endpoints and plate target, conversion notes, mesh dependencies and source hashes.
- `unity/IsaacScenario1Observer/`: new isolated runtime/Editor assemblies, extraction utility,
  package/settings, generated scene/visual assets, build profile, bounded launcher and tests.
- `docs/ISAAC_SCENARIO1_OBSERVER_2026-09-09.md`: this report.
### Commands Run
```bash
python3 unity/IsaacScenario1Observer/extract_layout.py --check

/home/sa08050/Unity/Hub/Editor/6000.5.6f1/Editor/Unity -batchmode -nographics \
  -projectPath '/home/sa08050/app developer/v2_ws/unity/IsaacScenario1Observer' \
  -executeMethod Sang.IsaacScenario1Observer.Editor.IsaacScenario1ObserverBuild.BuildLinux \
  -quit -logFile /tmp/sang-isaac-scenario1-observer-build.log

# From the new project directory:
python3 -m unittest -v test_launcher
python3 launch_observer.py --check-only

xvfb-run -a /home/sa08050/Unity/Hub/Editor/6000.5.6f1/Editor/Unity -batchmode -force-glcore \
  -projectPath '/home/sa08050/app developer/v2_ws/unity/IsaacScenario1Observer' \
  -executeMethod Sang.IsaacScenario1Observer.Editor.IsaacScenario1ObserverBuild.RenderPreview \
  -quit -logFile /tmp/sang-isaac-scenario1-observer-render.log
```
### Test / Build Results
- Exact Editor 6000.5.6f1 compiled and completed a real StrictMode Linux build; 174 Editor checks passed.
  Observer DLL SHA-256: `cfef353bfe4f982fefd1e7600bf144cfbf00c0f035bc050a29a6c29ca43715c6`.
- Source layout is independently reproducible, including all hashes; original source hashes are
  rechecked after build. The old RB5 pilot and original Scenario1 source received no edits.
- Scene allowlist: Transform, MeshFilter, MeshRenderer, Camera, Light, Scenario1Mirror only.
- All 113 Player DLLs scanned. Only application assembly: `Sang.IsaacScenario1Observer.dll`.
  No Assembly-CSharp, vendor/URDF-controller, scenario executor, or legacy sender assemblies/types.
  Observer IL has no network, process-start, reflection, physics-body, or vendor/executor references.
- Gate/mapping tests cover closed schemas, wrong identities, missing/duplicate/reordered assets and
  joints, nonfinite/invalid vectors, quaternion validity, session/epoch reset, freshness boundaries,
  monotonic timeout and clock/sequence rollback. Transform tests independently perturb world poses,
  off-axis joints and mimic joints; the readback detects each error.
- Fourteen launcher test methods pass, including malformed inputs, private-file checks, check-only,
  startup pinning, bounded timeout, interruption, and own-child-only cleanup. Process tests use mocks;
  no Player is launched by the tests.
- The initial unconfigured `--check-only` failed closed as intended. After pinning, the launcher's
  read-only `trusted_hash()` check passed against the new profile and compiled observer DLL.
  Actual receiver/session readiness remains main-owned; no live launch was attempted.
- Offscreen Editor render succeeded and was visually inspected. It does not enter Play Mode, run
  the runtime observer, or provide live Isaac/ROS evidence.
### Open Issues
- The supplied candidate manifest is pinned and rebuilt. Main must establish fresh receiver state
  and explicitly coordinate launch. Any manifest change requires a new reviewed pin/rebuild.
  No observer-owned live session, motion, or end-to-end Scenario1 claim.
- Generic Unity/.NET engine libraries remain in the Mono build. Isolation is application-code and
  scene-component isolation, not absence of every generic physics/network API, an OS sandbox, or a
  signed production attestation.
- Collider-based grasp behavior and main's Isaac FK/registration qualification remain main-owned.

## Launch Handoff
The supplied candidate digest is pinned and the rebuild/tests have passed. Main may perform a
coordinated launch only after receiver readiness. If the manifest changes, update this project's
trusted build profile explicitly, rebuild, and rerun tests. The launcher compares the exact profile hash and observer DLL hash with
audited build evidence, then validates fresh telemetry and pins session/epoch. A private, user-owned
receiver root is required. Symlink/nonregular/world-writable telemetry is rejected.
`--check-only` validates without launch. A coordinated `--duration-seconds 240` launch is bounded
to 1..3600 seconds, locks only this observer, and terminates only its own child on timeout or interrupt.
Reset/session change requires an explicit new launch after old observer exit. No arbitrary path,
executable, source script, endpoint, action, or reset command is accepted from telemetry or CLI.

## Evidence Paths
Within `unity/IsaacScenario1Observer/Builds/Evidence/`:
- `build-result.json`, `tests-passed.txt`, `player-assembly-audit.txt`, `scene-components.txt`.
- `pinned-build.log`: successful Editor 6000.5.6f1 build after the verified candidate-manifest pin.
- `source-sha256.txt`, `scene-world-inventory.json`, `table-bounds.json`, `gripper-geometry.json`.
- `geometry-preflight-comparison.json`: first main-owned physics-preflight/source-FK comparison.
- `editor-layout-preview.png`: static offscreen Editor view, not a live Player capture.

Built executable: `unity/IsaacScenario1Observer/Builds/Linux/IsaacScenario1Observer.x86_64`.

## Evaluator
### Verdict
PASS for the offline observer build/layout scope; live integration is not evaluated.
### Summary
Source-coordinate visual scene, schema-2 atomic mirroring gate, independent readback, isolation audit,
and bounded launch tests pass. The verified candidate manifest is pinned in the build profile and
built scene; source hashes and four-asset pose contract were rechecked. No Player was launched.
### Critical Issues
- No critical issue in the offline scope. Receiver readiness and coordinated launch remain main-owned.
### Non-Critical Issues
- No live Player/Isaac validation, optional camera stream omitted, no signed attestation.
- Renderer-center exports do not establish collider-based grasp equivalence.
### Revision Instruction
No further observer revision is required for this candidate. Coordinate live observation with main;
do not modify the pinned source layout or introduce motion logic. A changed manifest needs explicit
repinning and rebuild rather than automatic adoption from telemetry.
