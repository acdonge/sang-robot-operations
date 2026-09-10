"""Local Isaac authority for the pinned Scenario 1; ROS is feedback only."""

import argparse
import fcntl
import json
import math
from pathlib import Path
import time

import numpy as np

from .contract import CAMERA_TOPIC, JOINTS, ROOT, STATE_TOPIC, atomic_json, require_isolation
from .executor import ScenarioExecutor
from .motion import MotionProgram
from .scene import ScenarioScene
from isaac_twin.executor import utc_string


def preflight(scene, simulation, program):
    """Fresh-scene calibration experiment, never a Task Run or recovery action."""
    rows = []
    for step, phase, goal in (
        ("omy_pre_pick_place", "pre_transfer_continuous", [-.818, 1.25, .91]),
        ("omy_pre_pick_place", "pre_return_home", [-.24, .819, .91]),
        ("omy_post_pick_place", "post_transfer_continuous", [-.24, .819, .91]),
        ("omy_post_pick_place", "post_return_home", [-.818, 1.25, .91]),
        ("rb5_waam", "rb5_path", None),
    ):
        elapsed = next(p["t"] for p in program.curves[step]["phases"] if p["name"] == phase)
        target = program.sample(step, elapsed)
        scene.set_targets(target, initialize=True)
        for _ in range(120):
            scene.set_targets(target)
            simulation.step(render=True)
        measured = scene.measured()
        grip, tcp = scene.tool_positions()
        tcp_goal = program.curves[step]["tcp_checks"][0]["target_m"] if step == "rb5_waam" else None
        rows.append({"step": step, "phase": phase, "measured": measured,
                     "max_joint_error_rad": max(abs(a-b) for a,b in zip(target[:13], measured[:13])),
                     "grip_position_m": grip.tolist(), "rb5_tcp_m": tcp.tolist(),
                     "grip_goal_error_m": None if goal is None else float(np.linalg.norm(grip-goal)),
                     "tcp_goal_error_m": None if tcp_goal is None else float(np.linalg.norm(tcp-tcp_goal))})
    report = {"scene_sha256": program.scene_sha256, "physical_output_connected": False,
              "kind": "fresh_scene_geometry_preflight_not_workflow", "measurements": rows,
              "joint_names": scene.all_indices, "finger_local_centers": [v.tolist() for v in scene.finger_centers]}
    report["passed"] = all(r["max_joint_error_rad"] <= .03 and
                           (r["grip_goal_error_m"] is None or r["grip_goal_error_m"] <= .05) and
                           (r["tcp_goal_error_m"] is None or r["tcp_goal_error_m"] <= .01) for r in rows)
    atomic_json(ROOT / "geometry-preflight.json", report)
    print("SANG_SCENARIO1_PREFLIGHT=" + json.dumps(report), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-seconds", type=float, default=1200.)
    parser.add_argument("--initialize-scene", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args()
    if not math.isfinite(args.run_seconds) or not 0 < args.run_seconds <= 7200:
        parser.error("run-seconds must be finite and 0..7200")
    if not args.initialize_scene:
        parser.error("--initialize-scene required; creates a new virtual scene, not automatic recovery")
    require_isolation()
    program = MotionProgram()
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (ROOT / "executor.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    adapter_digest = None
    if not args.preflight:
        from fleet_control_plane.workflow_authoring import load_unity_adapter_profile
        adapter, adapter_digest = load_unity_adapter_profile(
            Path(__file__).resolve().parents[2] / "contracts/tasks/isaac-scenario1-adapter.v1.json")
        if adapter.scene_sha256 != program.scene_sha256:
            raise RuntimeError("Installed adapter does not match the prepared scene")
    app = simulation = engine = node = None
    report = {"status": "FAILED", "physical_output_connected": False, "scene_sha256": program.scene_sha256}
    try:
        from isaaclab.app import AppLauncher
        app = AppLauncher(headless=not args.gui, enable_cameras=not args.preflight).app
        import omni.usd
        from pxr import UsdGeom
        from isaacsim.core.api import SimulationContext
        omni.usd.get_context().new_stage()
        stage = omni.usd.get_context().get_stage()
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        UsdGeom.SetStageMetersPerUnit(stage, 1.)
        scene = ScenarioScene(stage)
        simulation = SimulationContext(physics_dt=1/60., rendering_dt=1/30., stage_units_in_meters=1.)
        simulation.initialize_physics()
        scene.initialize()
        simulation.play()
        ready = program.start("mobile_delivery")
        scene.set_targets(ready, initialize=True)
        for _ in range(60):
            scene.set_targets(ready)
            simulation.step(render=True)
        if args.preflight:
            result = preflight(scene, simulation, program)
            report.update(status="PREFLIGHT", passed=result["passed"])
            return

        from isaac_vendor.ros_observation import enable_observation_extensions, _usd_look_at_quaternion
        enable_observation_extensions(app)
        from isaacsim.sensors.camera import Camera
        camera = Camera(prim_path="/World/front_camera", frequency=30, resolution=(640, 480))
        camera.set_world_pose(position=np.array([2.4, 3.2, 2.5]),
                              orientation=_usd_look_at_quaternion([2.4, 3.2, 2.5], [-.3, 1.2, .8]), camera_axes="usd")
        camera.set_focal_length(1.8)
        camera.initialize()
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import JointState, CompressedImage
        from std_msgs.msg import String
        from rosgraph_msgs.msg import Clock
        import cv2
        rclpy.init(args=[])
        node = Node("sang_isaac_scenario1", use_global_arguments=False, enable_rosout=False,
                    start_parameter_services=False)
        state_pub = node.create_publisher(String, STATE_TOPIC, qos_profile_sensor_data)
        image_pub = node.create_publisher(CompressedImage, CAMERA_TOPIC, qos_profile_sensor_data)
        joint_pubs = {name: node.create_publisher(JointState, f"/sang/scenario1/{name}/joint_states", qos_profile_sensor_data)
                      for name in ("omy", "rb5")}
        clock_pub = node.create_publisher(Clock, "/clock", qos_profile_sensor_data)
        engine = ScenarioExecutor(ROOT, adapter_digest, program.scene_sha256, program)
        started = next_tick = time.monotonic()
        origin, last_epoch, sequence, image_count = simulation.current_time, engine.epoch, 0, 0
        max_error = 0.
        identity = dict(schema_version=1, runtime_kind="isaac-sim-executor", profile_id="scenario1",
                        scene_sha256=program.scene_sha256, session_id=engine.session_id, physical_output_connected=False)
        atomic_json(ROOT / "launch-evidence.json", {**identity, "ros_domain_id": 98,
            "command_topics": [], "ros_subscriptions": 0, "carrier_dynamics": "kinematic_fixture",
            "grasp_model": "measured_tool_attachment_fixture",
            "gripper_table_contacts": "excluded_for_kinematic_fixture"})
        while app.is_running() and time.monotonic()-started < args.run_seconds:
            measured = scene.measured()
            step = engine.active.step_id if engine.active else engine.next_step
            if step:
                engine.set_world_ready(*scene.world_ready(step, engine.active is not None))
            else:
                engine.set_world_ready(True, "Five steps complete; no new command admitted")
            target = engine.tick(measured, simulation.current_time)
            scene.set_targets(target)
            # One rendering tick advances exactly 1/30 s (two physics substeps).
            simulation.step(render=True)
            measured = scene.measured()
            max_error = max(max_error, max(abs(a-b) for a,b in zip(target[:13], measured[:13])))
            try:
                running = engine.state == "RUNNING" and engine.active is not None
                scene.update_fixture(engine.phase, measured, allow_transitions=running)
                if running and engine.active.step_id == "rb5_waam":
                    scene.check_path_point(program, engine.elapsed_s)
            except RuntimeError as exc:
                engine.fail(str(exc), measured)
                scene.set_targets(engine.target)
            if engine.epoch != last_epoch:
                last_epoch, origin, sequence = engine.epoch, simulation.current_time, 0
                scene.tcp_checks.clear()
            sim_ns = round((simulation.current_time-origin)*1e9)
            sequence += 1
            stamp = Clock()
            stamp.clock.sec, stamp.clock.nanosec = divmod(sim_ns, 1000000000)
            clock_pub.publish(stamp)
            source = {**identity, "schema_version": 2, "epoch": engine.epoch, "sequence": sequence,
                      "sim_time_ns": sim_ns, "source_unix_ms": int(time.time()*1000), "frame_id": "cell",
                      "phase": engine.phase, "plate_owner": scene.owner, "assets": scene.snapshot_assets(measured)}
            state_pub.publish(String(data=json.dumps(source)))
            frame_id = f"isaac-scenario1/{engine.session_id}/{engine.epoch}"
            for name, publisher in joint_pubs.items():
                joint = JointState()
                joint.header.stamp, joint.header.frame_id = stamp.clock, frame_id
                joint.name = list(JOINTS[name])
                joint.position = measured[:7] if name == "omy" else measured[7:13]
                publisher.publish(joint)
            rgba = np.asarray(camera.get_rgba())
            if rgba.ndim == 3 and rgba.shape[:2] == (480, 640) and rgba.shape[2] >= 3:
                ok, jpeg = cv2.imencode(".jpg", cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2BGR),
                                       [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok:
                    frame = CompressedImage()
                    frame.header.stamp, frame.header.frame_id = stamp.clock, frame_id
                    frame.format, frame.data = "jpeg", jpeg.tobytes()
                    image_pub.publish(frame)
                    image_count += 1
            atomic_json(ROOT / "runtime-identity.json", {**identity, "updated_at": utc_string(time.time())})
            atomic_json(ROOT / "heartbeat.json", engine.heartbeat())
            atomic_json(ROOT / "live-evidence.json", {**source, "state": engine.state,
                "max_tracking_error_rad": max_error, "fixture_events": scene.events,
                "tcp_checks": scene.tcp_checks, "image_count": image_count})
            if sequence == 1:
                print("SANG_SCENARIO1_READY="+json.dumps(identity), flush=True)
            next_tick += 1/30.
            delay = next_tick-time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -1/30.:
                next_tick = time.monotonic()
        report.update(status="STOPPED", state=engine.state, session_id=engine.session_id,
                      sequence=sequence, image_count=image_count, max_tracking_error_rad=max_error,
                      wall_seconds=time.monotonic()-started, fixture_events=scene.events)
        report["tcp_checks"] = scene.tcp_checks
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if engine:
            engine.shutdown()
            atomic_json(ROOT / "heartbeat.json", engine.heartbeat())
        if simulation:
            simulation.stop()
        if node:
            node.destroy_node()
            rclpy.shutdown()
        atomic_json(ROOT / "runtime-report.json", report)
        if app:
            app.close()


if __name__ == "__main__":
    main()
