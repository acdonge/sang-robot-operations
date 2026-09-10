"""Isaac-only virtual RB5 runtime with publisher-only ROS feedback."""

import argparse
from dataclasses import replace
import os
from pathlib import Path
import time

import numpy as np

from isaac_vendor.profile import load_profiles, require_profile_files
from isaac_vendor.scene_runner import _apply_actuator_contract, _validate_stage
from .contract import (
    CAMERA_TOPIC, DOMAIN_ID, JOINTS, JOINT_TOPIC, ROOT, SCENE_SHA256, STATE_TOPIC,
    atomic_json, identity, require_isolation,
)
from .executor import PilotExecutor, utc_string


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-seconds", type=float, default=600.)
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--initialize-scene", action="store_true",
                        help="Explicitly initialize a new virtual scene at its ready pose; not runtime recovery")
    args = parser.parse_args()
    if not 0 < args.run_seconds <= 7200:
        parser.error("run-seconds must be 0..7200, bounded wall time")
    if not args.initialize_scene:
        parser.error("--initialize-scene is required; restarting creates a new virtual scene/session")
    require_isolation()
    # Refuse a second local actuator authority for this dedicated pilot.
    import fcntl
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (ROOT / "executor.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    from fleet_control_plane.workflow_authoring import load_unity_adapter_profile
    workspace = Path(__file__).resolve().parents[2]
    adapter, adapter_digest = load_unity_adapter_profile(
        workspace / "contracts/tasks/isaac-rb5-twin-adapter.v1.json"
    )
    profile = load_profiles()["rb5"]
    require_profile_files(profile)
    if profile.scene_sha256 != SCENE_SHA256 or adapter.scene_sha256 != SCENE_SHA256:
        raise RuntimeError("Pinned RB5 scene differs")
    camera = next(c for c in profile.cameras if c.key == "cam_front")
    profile = replace(profile, cameras=(replace(camera, topic=CAMERA_TOPIC),))
    app = None
    node = None
    simulation = None
    engine = None
    report = {"physical_output_connected": False, "status": "FAILED"}
    try:
        from isaaclab.app import AppLauncher
        app = AppLauncher(headless=not args.gui, enable_cameras=True).app
        import omni.usd
        from isaacsim.core.api import SimulationContext
        from isaacsim.core.prims import Articulation
        from isaacsim.core.utils.stage import is_stage_loading
        from isaac_vendor.ros_observation import (
            enable_observation_extensions, create_camera_bindings, initialize_cameras,
        )
        enable_observation_extensions(app)
        omni.usd.get_context().open_stage(str(profile.scene_path))
        app.update()
        while is_stage_loading():
            app.update()
        stage = omni.usd.get_context().get_stage()
        graphs = [prim.GetPath().pathString for prim in stage.Traverse()
                  if prim.GetTypeName() in ("OmniGraph", "OmniGraphNode")]
        for path in sorted(graphs, key=len):
            stage.GetPrimAtPath(path).SetActive(False)
        missing, _ = _validate_stage(stage, profile)
        if missing:
            raise RuntimeError(f"Missing vendor prims: {missing}")
        _apply_actuator_contract(stage, profile)
        cameras = create_camera_bindings(profile)
        simulation = SimulationContext(physics_dt=1/60., rendering_dt=1/30., stage_units_in_meters=1.)
        simulation.initialize_physics()
        articulation = Articulation(profile.articulation_prim_path)
        articulation.initialize()
        indices = [articulation.get_dof_index(name) for name in JOINTS]
        initialize_cameras(cameras)
        simulation.play()
        simulation.step(render=True)
        ready = np.zeros((1, 6), dtype=np.float32)
        articulation.set_joint_positions(ready, joint_indices=indices)
        articulation.set_joint_velocities(ready, joint_indices=indices)
        for _ in range(60):
            articulation.set_joint_position_targets(ready, joint_indices=indices)
            simulation.step(render=True)

        import rclpy
        from rclpy.node import Node
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import JointState, CompressedImage
        from std_msgs.msg import String
        from rosgraph_msgs.msg import Clock
        import cv2
        import json
        rclpy.init(args=[])
        node = Node("sang_isaac_twin_rb5", use_global_arguments=False,
                    enable_rosout=False, start_parameter_services=False)
        joints_pub = node.create_publisher(JointState, JOINT_TOPIC, qos_profile_sensor_data)
        image_pub = node.create_publisher(CompressedImage, CAMERA_TOPIC, qos_profile_sensor_data)
        state_pub = node.create_publisher(String, STATE_TOPIC, qos_profile_sensor_data)
        clock_pub = node.create_publisher(Clock, "/clock", qos_profile_sensor_data)
        engine = PilotExecutor(ROOT, adapter_digest)
        started = time.monotonic()
        epoch_origin = simulation.current_time
        last_epoch, sequence, image_count = engine.epoch, 0, 0
        next_tick = started
        atomic_json(ROOT / "launch-evidence.json", {
            "runtime_kind": "isaac-sim-executor", "ros_domain_id": DOMAIN_ID,
            "scene_sha256": SCENE_SHA256, "disabled_graphs": graphs,
            "joint_names": list(JOINTS), "physical_output_connected": False,
            "command_topics": [], "ros_subscriptions": 0,
        })
        while app.is_running() and time.monotonic() - started < args.run_seconds:
            measured = [float(x) for x in np.asarray(articulation.get_joint_positions())[0, indices]]
            target = engine.tick(measured, simulation.current_time)
            articulation.set_joint_position_targets(np.asarray([target], dtype=np.float32), joint_indices=indices)
            simulation.step(render=True)
            if engine.epoch != last_epoch:
                last_epoch, epoch_origin = engine.epoch, simulation.current_time
                sequence = 0
            sim_ns = round((simulation.current_time - epoch_origin) * 1e9)
            sequence += 1
            clock = Clock()
            clock.clock.sec, clock.clock.nanosec = divmod(sim_ns, 1000000000)
            clock_pub.publish(clock)
            joint = JointState()
            joint.header.stamp = clock.clock
            joint.header.frame_id = f"isaac-twin-rb5/{engine.session_id}/{engine.epoch}"
            joint.name = list(JOINTS)
            joint.position = [float(x) for x in np.asarray(articulation.get_joint_positions())[0, indices]]
            joint.velocity = [float(x) for x in np.asarray(articulation.get_joint_velocities())[0, indices]]
            joints_pub.publish(joint)
            meta = {**identity(engine.session_id), "epoch": engine.epoch, "sequence": sequence,
                    "sim_time_ns": sim_ns, "source_unix_ms": int(time.time()*1000)}
            state_pub.publish(String(data=json.dumps(meta)))
            rgba = np.asarray(cameras["cam_front"].sensor.get_rgba())
            if rgba.ndim == 3 and rgba.shape[:2] == (480, 640) and rgba.shape[2] >= 3:
                ok, jpeg = cv2.imencode(".jpg", cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2BGR),
                                      [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok:
                    frame = CompressedImage()
                    frame.header.stamp = clock.clock
                    frame.header.frame_id = joint.header.frame_id
                    frame.format, frame.data = "jpeg", jpeg.tobytes()
                    image_pub.publish(frame)
                    image_count += 1
            runtime_identity = identity(engine.session_id)
            runtime_identity.pop("asset_id")
            runtime_identity["updated_at"] = utc_string(time.time())
            atomic_json(ROOT / "runtime-identity.json", runtime_identity)
            atomic_json(ROOT / "heartbeat.json", engine.heartbeat())
            if sequence == 1:
                print("SANG_ISAAC_TWIN_READY=" + json.dumps(runtime_identity), flush=True)
            next_tick += 1/30.
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -1/30.:
                next_tick = time.monotonic()
        report.update(status="STOPPED", image_count=image_count, sequence=sequence,
                      wall_seconds=time.monotonic()-started, session_id=engine.session_id)
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
