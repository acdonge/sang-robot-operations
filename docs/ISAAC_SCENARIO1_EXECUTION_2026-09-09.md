# Isaac Scenario 1 Execution and Unity Observation

## Scope

Extend the single-RB5 pilot to the existing Scenario 1 sequence, without physical
robot, vendor socket, welder, feeder, ROS command, or XR actuation outputs.
The Central UI queues versioned intent; the dedicated Processing worker owns
short-lived authority; Isaac alone applies virtual joint targets. Unity only
renders named measurements received through the isolated ROS 2 domain 98.

| Step | Virtual asset | Completion evidence |
|---|---|---|
| mobile_delivery | Carrier | Applied scene position at dock; plate on carrier |
| omy_pre_pick_place | OMY | Measured seven joints at endpoint; plate attached then released on table |
| rb5_waam | RB5 | Measured six joints; 25 TCP points within 12 mm and tool direction within 5 degrees; home restored |
| omy_post_pick_place | OMY | Measured seven joints; plate returned to carrier |
| mobile_return | Carrier | Applied scene position at home; plate on carrier |

These are **simulation fixture** criteria, not physical safety criteria. No arc
or welding process is simulated. The carrier follows a kinematic fixture, not a
validated mobile base controller. The plate is a flat, kinematic attachment
whose ownership changes only after measured gripper/location predicates pass.
It does not model contact forces, friction, slip, dropped loads or grasp quality.

## Geometry and Recorded Paths

The original Unity project and vendor USD files are unchanged. `layout.json`
retains their source frames, mesh dependencies, hashes and named joint axes.
Runtime carrier home/dock differ from the carrier's saved scene pose; these are
separately recorded rather than silently using the saved transform.

ROS cell position maps to Unity as `(-y,z,x)`; quaternion maps to
`(qy,-qz,-qx,qw)`. Both arms retain the source robot base placements.

The source joint recordings were not directly portable:

- OMY recordings use collider-derived grasp centers. The new visual-only
  observer and vendor USD do not share those refined collider centers. The
  offline preparation therefore bounds endpoint corrections to 0.25 rad per
  joint and blends them into the recorded path. The declared attachment center
  is 40 mm above the plate; the runtime still measures the actual geometry.
- The saved RB5 angles put the tool on the opposite side of the current cell.
  The original Unity controller also computes the path from world targets.
  We preserve its 25 world-space points, solve bounded vendor-model joint poses
  offline, and retime the path. Actual Isaac TCP measurements, not the offline
  solution or timer alone, must confirm every point.
- Table top extents/heights match source visual bounds; slab thickness and legs
  are proxies. Only the four finger-to-table-top contact pairs are excluded for
  the explicit kinematic grasp fixture. Other arm/environment contacts remain.
  This exclusion must not be carried into a physical collision-safety claim.
- Joint drives use an explicit virtual acceleration response, not calibrated
  manufacturer motor dynamics. Retiming is not weld-video synchronization.

The prepared sequence is approximately 202.06 simulation seconds. Worker and
authority deadlines use wall/monotonic time, not simulation time. The manifest
pins source files, generated paths, geometry/model extraction, and execution
code. Source changes require preparation, a new adapter digest, an observer
rebuild, and a new validation profile version. Old evidence is retained.

## Components

- `sim/isaac_scenario1`: pinned paths, cell composition, guarded Isaac executor,
  publisher-only telemetry, and a receiver with no actuation outputs.
- `contracts/tasks/isaac-scenario1-*.v1.json`: exact five-stage contract.
- `config/isaac_scenario1_simulation.yaml`: fixed role/asset/agent bindings.
- `docker/compose.isaac-scenario1.yaml`: isolated Scenario 1 worker and additional
  Central adapter profile; existing Unity and one-RB5 profiles remain available.
- `unity/IsaacScenario1Observer`: separate passive build with no physics,
  controller, vendor, or executor assemblies. It pins session/epoch at startup.
- `scripts/register_isaac_scenario1_task.py`: idempotent registration through
  normal authoring APIs, not direct database mutation or implicit execution.

The current scope is a fixed Scenario 1 adapter, not an arbitrary-script runner
or a general collision-free multi-robot motion planner.

## Operation

Use the existing `workflow.html` page and the published **Isaac Scenario 1 +
Unity observer** Task. Select the Isaac adapter explicitly if more than one
engine is compatible. A configured gate and fresh executor/worker are required.
Hold freezes progress without extending the deadline; Resume is explicit.
Abort prevents later steps. Reset clears terminal execution state but never
moves the robot or snaps the plate. An interrupted partial pose cannot silently
restart from home; creating a fresh virtual scene is an explicit operation.

Local launch entry points, after deploying the overlay and registering intent:

```bash
bash scripts/run_isaac_scenario1.sh --initialize-scene --run-seconds 1200
bash scripts/run_isaac_scenario1_receiver.sh --run-seconds 1200
python unity/IsaacScenario1Observer/launch_observer.py --duration-seconds 600
```

The launchers force localhost-only ROS isolation. A new scene or Reset changes
the observation identity/epoch; the observer deliberately freezes until the
user explicitly relaunches it. It never automatically adopts a new authority.

## Evidence and Remaining Limits

Geometry preflight passed with maximum settled joint error about 0.00113 rad
and first RB5 TCP error about 0.000117 m. Ten path-geometry unit checks pass.
The passive observer has an actual Unity 6000.5.6f1 Linux build and 174 Editor
checks. These checks alone are not an end-to-end Workflow acceptance result.

Live full-sequence, Reset/repeat, fault checks and final regression results are
recorded below. Runtime evidence is under `/tmp/sang-isaac-scenario1`; durable
acceptance copies are under `var/`.

Remaining outside this iteration: measured mobile dynamics, contact-based grasp,
qualified collision geometry, physical stop/safety validation, arbitrary new
scene/action deployment, and recording this trial into the data catalog.

## Independent Review and Regression

Three scoped Codex reviewers covered the passive observer, Workflow integration,
and executor/receiver. No Claude review is claimed for this iteration.
Final executor review identified four issues, all repaired before the final
candidate was run:

1. HOLD could reach an attachment transition. Ownership changes now require
   an active RUNNING command; held/terminal states only track an existing attachment.
2. A post-physics failure could hold the older pre-physics target. Failure now
   takes the fresh finite measurement, or retains the last valid measured pose.
3. Abort/Reset could leave an attached plate behind or snap it. Passive attachment
   tracking continues without changing ownership; Reset does not home or reattach.
4. NaN geometry could pass distance comparisons. Non-finite geometry and measured
   joints now reject before ownership or path-checkpoint acceptance.

The reviewer added 89 regression cases and found no further actionable issue in
these four paths. Final source suite: 400 simulation tests PASS, including the
unchanged 45 pilot tests; 267 backend/UI-boundary tests PASS. The latter reports
two existing dependency deprecation warnings. The frontend has 13 adapter-selection
tests plus successful typecheck/build. The actual Unity Linux build passed 174
Editor checks, a 113-assembly isolation audit, and 14 launcher tests.

The Workflow page now explicitly selects the simulation adapter when multiple
engines are compatible and evaluates the gate against that adapter's current
digest. Its entry filename is content-hashed so the deployed page does not retain
the older cached Workflow implementation. The legacy Start Unity controls remain
clearly separate from the Isaac executor and passive observer.

### Remaining Visual Limitation

The Isaac RGB overview still shows part of the OMY base visual below its table.
Fresh preflight with transform rewriting disabled did not resolve that artifact.
Measured fixed-root pose, arm joints and tool geometry pass their scoped checks,
but this is not proof that every rendered vendor mesh is correctly registered.
The vendor RB5 USD also reports an unresolved TCP visual reference; the measured
TCP uses link6 plus its recorded local offset, not that missing mesh. Repair and
recheck these visual references before claiming whole-scene visual correspondence.
Neither warning is a physical safety or collision qualification.

## Final Live Acceptance - 2026-09-09

Both final Runs were started using **Validate in Isaac** on the deployed Workflow
page, with profile `isaac-scenario1-validation@3`. The same Isaac process/session
`de1bcba5846142139e46eeac6823fd3a` was retained across Reset; the virtual scene was
not recreated between Runs. Manifest SHA-256:
`c988f5ecdd0d331ee338619af369efcafdc098df400488a97463855b1a69821d`.
All 136 pinned source hashes were checked again after testing.

| Final Run | Wall duration | Five steps | Web Reset | Validation |
|---|---:|---|---|---|
| `sim-18613c9242cf49898cbdfbccb96a7d5f` | 202.463 s | SUCCEEDED | epoch 0 to 1, READY | PASS |
| `sim-9ed2fc38441a40ad9273abab89d06315` | 224.164 s, including Hold/Resume | SUCCEEDED | epoch 1 to 2, READY | PASS |

Measured evidence:

- Each Run passed all 25 RB5 tool checkpoints. Maximum position error was
  0.129 mm, and maximum downward tool-direction error was 0.0215 degrees.
- Plate placement on the table was within 0.709 mm. Return-to-carrier error was
  0.0895 mm in Run 1 and 0.1789 mm in Run 2. The fixture preserves the actual
  release offset; it does not snap to an ideal target. This small accumulated
  offset means two successful cycles are not a long-duration drift qualification.
- Final joint-pose difference between cycles was about 1e-12 rad. Reset changed
  root/object position by 0 m and settled joint values by at most 3.31e-7 rad.
- The final candidate's Web Hold/Resume was exercised during the RB5 path. A
  two-second HELD measurement interval had 4.71e-11 rad maximum joint drift,
  unchanged fixture events and unchanged path-checkpoint count.
- The 225-second source-to-Unity probes matched 1,071 and 1,075 samples with zero
  failures. A supplemental 60-second probe covered the second Run's completion
  after the Hold, matching 288 samples with zero failures. Maximum measured-to-
  rendered joint difference was 3.15e-7 rad; position difference was below 1.15e-7 m.
  Missed polling correlations are counted separately, not labeled dropped ROS frames.
- Snapshot source rate was approximately 30 Hz and simulation/wall-time ratio
  approximately 1.0. One overview camera publishes 640x480 JPEG at nominal 30 Hz,
  quality 85; this is neither a hard real-time bound nor cross-PC clock validation.
- The receiver reported no rejected source samples during these probes. Unity
  was explicitly relaunched for epoch 1 before the second Run, never automatically
  rebound. Its bounded Player had exited by the final Reset; final Reset's live
  freeze behavior is therefore not claimed from that run. Rejection behavior has
  current build tests and earlier candidate live evidence.
- This executor created zero ROS command subscriptions/topics and no vendor,
  robot, welding or feeder output path. Existing physical read-only services were
  not stopped or reconfigured.

Durable result: `var/isaac-scenario1-acceptance-summary.json`. Per-run API records,
pre/post-Reset snapshots, probes and Hold evidence share the prefix
`var/isaac-scenario1-final-`. The earlier `sim-d3317155598345b0963ea5fc4c42228d`
candidate is retained separately and is not substituted for final-candidate proof.

After both Reset acknowledgments, the bounded Isaac process exited normally in
READY state at 950.0001 seconds, with 28,500 generated overview frames and no
runtime error. The receiver and Unity Player also exited; no trial simulator
process remains. Web/Central services and the idle worker remain running. The
simulators must be started explicitly before another Web Run. Shutdown evidence:
`var/isaac-scenario1-final-runtime-report.json`.

Evaluator: **PASS for this fixed simulation-only Scenario 1 scope**. Visual-base
repair, extended-cycle drift, qualified grasp/mobile dynamics, signed reusable
package deployment, automatic simulation supervision and MCAP/catalog integration
remain separate work. This does not close physical commissioning or the original
Unity IK repeatability issue.
