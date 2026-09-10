# Isaac Executor / Unity Digital Twin Pilot

## Scope and decision

The accepted next step is to substitute Isaac Sim for one physical robot while
retaining SANG's Web UI, immutable Tasks and Processing-side orchestration.
Isaac owns all motion and physics; Unity is an observation-only digital twin.
The existing OPC UA information/management plane is unchanged. This pilot does
not add an OPC UA servo stream or a new OPC UA Method implementation.

Implemented topology:

```text
Workflow Studio -> durable simulation queue -> Processing simulation Worker
    -> existing CellOrchestrator -> pinned local Isaac command adapter
    -> Isaac RB5 physics / measured joints / front RGB camera
    -> ROS 2, isolated domain 98 -> subscriber receiver -> Unity observer
```

The command/result file transport is a local pilot boundary, not a new distributed
transport. Central stores task intent and evidence; it does not run vendor scripts.
The ROS receiver only publishes local snapshot files, not application ROS commands.
ROS may create its own parameter-event publisher. Existing read-only robot agents,
OPC UA services and recorders are not replaced or given command capability.

## Pinned contract

| Item | Value |
| --- | --- |
| Task | `isaac-rb5-twin-pilot@1`, Isaac RB5 + Unity digital twin |
| Asset / Agent | `isaac-twin-rb5` / `isaac-twin-rb5-agent` |
| Adapter | `isaac-rb5-twin-pilot-v1`, schema 3 |
| Runtime | Isaac Sim 5.1.0.0, RTX 5080, ROS 2 Jazzy |
| Observer | separate Unity 6000.5.6f1 Linux Player |
| Actions | `move_offset` then `return_ready`; fixed `simulate-bounded-joint@1` |
| Motion | base 0 -> 0.1 -> 0 rad; other five targets 0; 3 simulated seconds per action |
| Goal acceptance | maximum measured joint error 0.02 rad |
| Wall deadline | at most 15 seconds per accepted action, not extended by Hold/Resume |
| Authority | worker permit refreshed at most every 0.5 s; expiry at most 2 s |
| Admission | command age at most 2 s, future tolerance 0.1 s; signed goal window at most 900 s |
| Runtime files | `/tmp/sang-isaac-rb5-twin`, private local pilot root |
| Isolation | ROS domain 98, localhost only; no vendor command SDK, physical devices or ROS command subscriptions |
| Camera | one front RGB, 640x480 JPEG quality 85, nominal 30 Hz |

Scene SHA-256: `5837e8eb41c67024a4d461f07e3d63dc53bde338209fd64c6c238ceb187972ce`.
Adapter SHA-256: `2e15d4d9805e42e57d61c059454c04f72e59135ff533e79c748897d4adb6ebdf`.
Workflow SHA-256: `de94c7fc6d6da6fbad9a9138f3bcde3c31d359b8751ac222e5cd77d20b61af16`.
The original vendor USD is unchanged; inherited OmniGraph nodes are disabled in
the loaded runtime stage so there is no second scene command authority.

ROS topics: `/sang/twin/rb5/joint_states`, `/sang/twin/rb5/state`,
`/sang/twin/rb5/camera/front/compressed`, `/clock`. Joint and camera samples carry
session/epoch/simulation-time identity. The receiver joins that metadata with
actual ROS samples; Unity never reads the executor's measured-state files.
Source and buffer freshness limits are 250 ms at the receiver, 500 ms in Unity.

## Implementation map

- `sim/isaac_twin`: fixed contract, durable executor, Isaac runtime, ROS receiver and regressions.
- `isaac_simulation_backend.py` and `workflow_simulation_worker.py`: pinned identity,
  durable claim fencing, independent short permit refresh, controls and result reconciliation.
- `contracts/tasks/isaac-rb5-twin-*.v1.json`, `config/isaac_rb5_twin_simulation.yaml`:
  immutable pilot workflow/profile; existing Unity profile digests are preserved.
- `unity/IsaacRb5Observer`: independent visual-only build, no legacy execution assembly,
  autonomous motion, articulation controller, vendor sender or application network client.
- Workflow Studio: **Validate in Isaac**, runtime-specific evidence, state-only Reset wording.
- `docker/compose.isaac-twin.yaml`: dedicated worker without hardware mounts or host networking.
- HTML/fixed-name UI assets revalidate their cache to prevent old/new UI bundles being mixed.

## Reset, cancellation and restart

Abort latches a terminal state, holds the last measured pose and prevents automatic
resumption when the worker returns. Permit expiry uses a monotonic deadline;
alternating/replayed permit documents cannot extend it. Commands/results are
idempotent and persisted. A queued Abort also blocks a not-yet-accepted command.

Task **Reset is state-only**: clear the completed/aborted Run, increment the
observation epoch and hold the measured pose. It does not home, teleport or replay
an action. A new process launched with `--initialize-scene` explicitly initializes
a new virtual scene/session at zero. This is distinct from Reset and is not a
claim of crash-safe physical pose recovery. Active commands are finalized on normal
shutdown; unfinished persisted commands are aborted on startup rather than replayed.

Unity pins both session and epoch. Reset, stale feedback or session change freezes
the displayed pose until an explicit fresh observer launch. There is no automatic
rebind or hidden control path. Rebinding requires restarting only the observer,
not the original Scenario Player or physical adapters.

## Actual evidence

First web-triggered Run `sim-6c78ca31ac4e4cacac7378c2d1a83b57` completed both actions
and received Reset acknowledgement: validation PASS. The 40-second probe saw the
base move to about 0.1 rad and back; all 795 samples were mirroring. Maximum
transform-readback difference was about 1.03e-8 rad and receiver-to-observer age
48 ms. This first build ran simulation time about 1.5x wall time; the duplicate
physics step was removed before the final run. Do not use that first timing as
a real-time claim.

Worker-loss experiment `sim-4d620bf73e904c2ab4dfed1891bd7fac` paused only the dedicated
pilot worker for 3 seconds. Isaac latched ABORTED after 1.971 seconds; settled base
span was 0.000546 rad and reconnect did not resume motion. Central records this
deliberately interrupted Run as FAILED, which is the expected fault-test result.
Subsequent explicit Reset was acknowledged. No physical process was paused.

Final source runtime Run `sim-bdf273ce3bc14bfc8cb02158dee50f2f` completed both actions
in 6.150 wall seconds, with Isaac session `9f11f432451d4cde8909cb11ef42c610`.
Web Reset `simctl-dee0d9c157b84796afd0458bd3356a11` was ACKNOWLEDGED at
19:13:48.719 UTC; all six gate checks passed. The 50.037-second final probe had
994/994 mirroring samples, base range -0.000026 to 0.100012 rad, maximum joint
mirror readback difference 1.183e-8 rad and maximum receiver-to-observer age 46 ms.
Endpoint-derived average joint and image rates were 30.008 Hz; simulation/wall-time
ratio was 1.00028. These are sampled local results, not worst-case latency or hard
real-time guarantees. The received JPEG decoded successfully as 640x480.

After Reset, epoch-0 Unity froze with `session-or-epoch-reset`; receiver epoch was 1.
An explicit 12-second observer relaunch pinned epoch 1, accepted 250 samples and
resumed mirroring. Pilot Isaac, receiver and observer processes were then stopped;
the existing Web server, results and dedicated idle worker remain available.

Evidence files under `output/validation`:
- `isaac-twin-rb5-preflight-20260909.json`: actual vendor articulation/scene check.
- `isaac-twin-ui-roundtrip-20260909.json`: first 40-second ROS/Unity sample.
- `isaac-disconnect-sim-4d620bf73e904c2ab4dfed1891bd7fac.json`: attended local simulation fault probe.
- `isaac-twin-final-roundtrip-20260909.json`: final-source ROS/Unity sample.
- `isaac-twin-final-evidence-20260909.json`: final Run, Reset, derived rates,
  source digests and frozen observer snapshot (unsigned local evidence).
- `isaac-twin-final-rebind-20260909.json`: fresh epoch-1 observer acceptance.
- `isaac-twin-final-front-20260909.jpg`: actual decoded front-camera sample.

The observer build/tests and explicit epoch rejection/rebind evidence are detailed
in [`ISAAC_RB5_OBSERVER_BUILD_2026-09-09.md`](ISAAC_RB5_OBSERVER_BUILD_2026-09-09.md).

## Launch and use

From `/home/sa08050/app developer/v2_ws`, start only the dedicated worker with the
existing local deployment configuration (do not copy secrets into documentation):

```bash
docker compose --env-file runtime/phase3-lab1/config/central.env \
  -p sang-phase3-lab1-central \
  -f docker/compose.phase3-central.yaml \
  -f docker/compose.phase3-lab1-preview.yaml \
  -f docker/compose.manager-secure.yaml \
  -f docker/compose.isaac-twin.yaml up -d --no-deps isaac-workflow-worker
```

In separate terminals, in order, after each predecessor produces fresh identity/data:

```bash
bash scripts/run_isaac_twin.sh --initialize-scene --run-seconds 600
bash scripts/run_isaac_twin_receiver.sh --run-seconds 550
python3 unity/IsaacRb5Observer/launch_observer.py --duration-seconds 480
```

For a deployment without the registered pilot Task, use
`python3 scripts/register_isaac_twin_task.py` (registration only, no execution).
The validated local deployment already has its gate profile; confirm the gate is
configured and the Isaac adapter is READY before running. A new deployment also
needs that validation-gate configuration through the existing Workflow UI.

Open [Workflow Studio](http://127.0.0.1:18091/workflow.html), **Published library**,
**Isaac RB5 + Unity digital twin**, **Validate in Isaac**. **Simulation Runs** shows
the two steps. After completion use **Reset**, then explicitly relaunch the observer
to adopt the new epoch. The legacy **Start Unity** button is not the pilot launcher.
The loops are bounded and eventually stop; the Web UI alone does not start Isaac.

## Review and limits

Independent Codex review reproduced queued-cancel races, permit replay, restart
target behavior, stale commands, weak image/session freshness, shutdown/journal
recovery and inbox starvation. The fixes have dedicated regressions, including
same-epoch camera timestamp regression. Claude was not used for this iteration.

Final checks: 441 Python tests passed across the Fleet control-plane and Isaac
pilot, including 45 Isaac-specific tests. Separate Unity Editor acceptance checks:
44 passed, Linux strict build passed; observer launcher: 3 passed. Frontend
`npm run typecheck` and production build passed. Two Python dependency-deprecation
warnings remain (Starlette/httpx and AnyIO alias), not test failures.
The reviewer independently reran all 45 Isaac tests and returned scoped PASS.

Evaluator: **PASS for the fixed, local virtual RB5 pilot only.** Actual web dispatch,
measured return path, camera reception, Reset/rebind and worker-disconnect latch
were verified. No physical motion or process output was added or exercised.

This is not a complete Scenario 1, arbitrary Action Library script execution,
multi-robot/world-frame qualification, ROS/MCAP/catalog recording integration,
embedded web camera/Unity preview, signed production release or hard real-time
guarantee. Unity renders only the mirrored robot; the pilot RGB snapshot is produced
by the ROS receiver, not displayed inside this observer. TCP/cell coordinates,
camera extrinsics and real dynamics still require explicit validation.

Vendor USD emits unresolved peg/TCP visual-reference warnings. These did not stop
the measured six-axis/RGB check, but tool/contact tasks require asset cleanup and
collision validation before expansion. An independent E-stop, physical safety
assessment and commissioning are still mandatory before any real robot output.

Next: coordinate/TCP/camera registration, then OMY and carrier assets and the full
Scenario 1 sequence. Preserve one motion authority per asset; do not let Unity
and Isaac both drive the same robot.
