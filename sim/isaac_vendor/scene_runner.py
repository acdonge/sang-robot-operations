"""Load a vendor scene and optionally run a bounded articulation exercise."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import numpy as np

try:
    from .profile import (
        DEFAULT_PROFILES_PATH,
        ProfileError,
        VendorProfile,
        load_profiles,
        require_profile_files,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).parents[1]))
    from isaac_vendor.profile import (
        DEFAULT_PROFILES_PATH,
        ProfileError,
        VendorProfile,
        load_profiles,
        require_profile_files,
    )


WORKSPACE_ROOT = Path(__file__).parents[2]


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES_PATH)
    parser.add_argument("--gui", action="store_true", help="Keep an Isaac Sim window open")
    parser.add_argument(
        "--exercise",
        action="store_true",
        help="Run the profile's bounded test-joint movement",
    )
    parser.add_argument(
        "--publish-ros",
        action="store_true",
        help="Publish read-only JointState and JPEG camera observations",
    )
    parser.add_argument(
        "--run-seconds",
        type=float,
        default=0.0,
        help="Stop after this wall-clock duration; zero keeps GUI mode open",
    )
    parser.add_argument("--report", type=Path)
    options = parser.parse_args(args)
    if not math.isfinite(options.run_seconds) or options.run_seconds < 0.0:
        parser.error("run-seconds must be a non-negative finite number")
    if options.publish_ros and not options.gui and options.run_seconds <= 0.0:
        parser.error("headless ROS publishing requires a positive --run-seconds")
    return options


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unknown"


def _isaac_lab_repository() -> dict[str, str]:
    import isaaclab

    root = Path(isaaclab.__file__).resolve().parents[3]

    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    return {
        "path": str(root),
        "describe": git("describe", "--tags", "--always", "--dirty"),
        "commit": git("rev-parse", "HEAD"),
    }


def _default_report_path(profile_id: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return WORKSPACE_ROOT / "output" / "validation" / f"isaac-{profile_id}-{stamp}.json"


def _write_report(path: Path, report: MappingLike) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


MappingLike = dict[str, Any]


def _apply_actuator_contract(stage, profile: VendorProfile) -> list[dict[str, Any]]:
    from pxr import PhysxSchema, UsdPhysics

    applied = []
    for group in profile.actuator_groups:
        for joint_name in group.joints:
            prim_path = f"{profile.joint_prim_root}/{joint_name}"
            prim = stage.GetPrimAtPath(prim_path)
            if not prim.IsValid():
                raise RuntimeError(f"actuator joint prim is missing: {prim_path}")
            drive = UsdPhysics.DriveAPI.Get(prim, "angular")
            if not drive:
                drive = UsdPhysics.DriveAPI.Apply(prim, "angular")
            drive.GetTypeAttr().Set(group.drive_type)
            drive.GetStiffnessAttr().Set(group.stiffness)
            drive.GetDampingAttr().Set(group.damping)
            drive.GetMaxForceAttr().Set(group.max_force)
            physx_joint = PhysxSchema.PhysxJointAPI.Get(stage, prim_path)
            if not physx_joint:
                physx_joint = PhysxSchema.PhysxJointAPI.Apply(prim)
            physx_joint.GetMaxJointVelocityAttr().Set(
                math.degrees(group.max_velocity_rad_s)
            )
            applied.append(
                {
                    "group": group.name,
                    "joint": joint_name,
                    "drive_type": group.drive_type,
                    "stiffness": group.stiffness,
                    "damping": group.damping,
                    "max_force": group.max_force,
                    "max_velocity_rad_s": group.max_velocity_rad_s,
                }
            )
    return applied


def _validate_stage(stage, profile: VendorProfile) -> tuple[list[str], list[dict[str, Any]]]:
    missing = [
        prim_path
        for prim_path in profile.required_prims
        if not stage.GetPrimAtPath(prim_path).IsValid()
    ]
    camera_checks = []
    for camera in profile.cameras:
        present = stage.GetPrimAtPath(camera.prim_path).IsValid()
        camera_checks.append(
            {
                "key": camera.key,
                "prim_path": camera.prim_path,
                "present": present,
                "required_in_scene": camera.required_in_scene,
                "provisioned_by": camera.provisioned_by,
                "mode": camera.mode,
                "topic": camera.topic,
                "frame_id": camera.frame_id,
                "width": camera.width,
                "height": camera.height,
            }
        )
        if camera.required_in_scene and not present:
            missing.append(camera.prim_path)
    return missing, camera_checks


def _require_ros_isolation() -> None:
    if os.getenv("ROS_DISTRO") != "jazzy":
        raise RuntimeError("ROS_DISTRO must be jazzy for Isaac ROS publishing")
    domain_value = os.getenv("ROS_DOMAIN_ID", "")
    try:
        domain_id = int(domain_value)
    except ValueError as exc:
        raise RuntimeError("ROS_DOMAIN_ID must be an integer from 1 to 232") from exc
    if not 1 <= domain_id <= 232:
        raise RuntimeError(
            "ROS_DOMAIN_ID must be a dedicated simulation domain from 1 to 232"
        )
    if os.getenv("ROS_AUTOMATIC_DISCOVERY_RANGE") not in {"LOCALHOST", "SUBNET"}:
        raise RuntimeError(
            "ROS_AUTOMATIC_DISCOVERY_RANGE must be LOCALHOST or SUBNET"
        )
    if os.getenv("FASTDDS_BUILTIN_TRANSPORTS") != "UDPv4":
        raise RuntimeError(
            "FASTDDS_BUILTIN_TRANSPORTS must be UDPv4 for container-safe simulation"
        )
    if os.getenv("RMW_FASTRTPS_PUBLICATION_MODE") != "ASYNCHRONOUS":
        raise RuntimeError(
            "RMW_FASTRTPS_PUBLICATION_MODE must be ASYNCHRONOUS for camera telemetry"
        )


def _run_scene(
    app,
    profile: VendorProfile,
    *,
    gui: bool,
    exercise: bool,
    publish_ros: bool,
    run_seconds: float,
) -> MappingLike:
    import omni.usd
    from isaacsim.core.api import SimulationContext
    from isaacsim.core.prims import Articulation
    from isaacsim.core.utils.stage import is_stage_loading

    omni.usd.get_context().open_stage(str(profile.scene_path))
    app.update()
    app.update()
    while is_stage_loading():
        app.update()

    stage = omni.usd.get_context().get_stage()
    disabled_prims = []
    for prim_path in profile.disabled_prims:
        prim = stage.GetPrimAtPath(prim_path)
        if prim.IsValid() and prim.IsActive():
            prim.SetActive(False)
            disabled_prims.append(prim_path)

    missing_prims, cameras = _validate_stage(stage, profile)
    if missing_prims:
        raise RuntimeError("required USD prims are missing: " + ", ".join(missing_prims))
    actuators = _apply_actuator_contract(stage, profile)

    camera_bindings = {}
    if publish_ros:
        try:
            from .ros_observation import (
                RosObservationPublisher,
                create_camera_bindings,
                finalize_link_camera_poses,
                initialize_cameras,
            )
        except ImportError:
            from isaac_vendor.ros_observation import (
                RosObservationPublisher,
                create_camera_bindings,
                finalize_link_camera_poses,
                initialize_cameras,
            )

        camera_bindings = create_camera_bindings(profile)
        _, cameras = _validate_stage(stage, profile)

    simulation = SimulationContext(
        physics_dt=1.0 / 60.0,
        rendering_dt=1.0 / 30.0,
        stage_units_in_meters=1.0,
    )
    simulation.initialize_physics()
    articulation = Articulation(profile.articulation_prim_path)
    articulation.initialize()
    if publish_ros:
        initialize_cameras(camera_bindings)
    dof_names = list(articulation.dof_names)
    missing_joints = sorted(set(profile.controlled_joints) - set(dof_names))
    if missing_joints:
        raise RuntimeError("articulation joints are missing: " + ", ".join(missing_joints))
    joint_indices = [articulation.get_dof_index(name) for name in profile.controlled_joints]
    ready = np.asarray([profile.ready_positions], dtype=np.float32)
    zeros = np.zeros_like(ready)

    simulation.play()
    simulation.step(render=gui or publish_ros)
    articulation.set_joint_positions(ready, joint_indices=joint_indices)
    articulation.set_joint_velocities(zeros, joint_indices=joint_indices)
    for _ in range(profile.exercise.settle_steps):
        articulation.set_joint_position_targets(ready, joint_indices=joint_indices)
        simulation.step(render=gui)

    if publish_ros:
        finalize_link_camera_poses(camera_bindings)
        for _ in range(8):
            articulation.set_joint_position_targets(ready, joint_indices=joint_indices)
            simulation.step(render=False)
            simulation.step(render=True)

    measured_ready = np.asarray(articulation.get_joint_positions())[0, joint_indices]
    target = ready.copy()
    exercise_report = None
    if exercise:
        exercise_offset = profile.controlled_joints.index(profile.exercise.joint)
        target[0, exercise_offset] += profile.exercise.delta_rad
        start = float(measured_ready[exercise_offset])
        for _ in range(profile.exercise.exercise_steps):
            articulation.set_joint_position_targets(target, joint_indices=joint_indices)
            simulation.step(render=gui or publish_ros)
        measured = np.asarray(articulation.get_joint_positions())[0, joint_indices]
        final = float(measured[exercise_offset])
        target_value = float(target[0, exercise_offset])
        motion = abs(final - start)
        error = abs(final - target_value)
        exercise_report = {
            "joint": profile.exercise.joint,
            "start_rad": start,
            "target_rad": target_value,
            "final_rad": final,
            "motion_rad": motion,
            "error_rad": error,
            "minimum_motion_rad": profile.exercise.minimum_motion_rad,
            "maximum_error_rad": profile.exercise.maximum_error_rad,
            "passed": (
                motion >= profile.exercise.minimum_motion_rad
                and error <= profile.exercise.maximum_error_rad
            ),
        }

    observation_report = None
    publisher = None
    ready_announced = False
    should_loop = gui or publish_ros or run_seconds > 0.0
    if publish_ros:
        publisher = RosObservationPublisher(
            profile,
            articulation,
            joint_indices,
            camera_bindings,
        )
    if should_loop:
        started = time.monotonic()
        deadline = None if run_seconds <= 0.0 else started + run_seconds
        period_s = 1.0 / profile.observation.publish_rate_hz
        next_tick = started
        try:
            while app.is_running() and (deadline is None or time.monotonic() < deadline):
                articulation.set_joint_position_targets(target, joint_indices=joint_indices)
                if publish_ros:
                    simulation.step(render=False)
                simulation.step(render=gui or publish_ros)
                if publisher is not None:
                    publisher.publish()
                    if not ready_announced:
                        print(
                            "SANG_ISAAC_OBSERVATION_READY="
                            + json.dumps(
                                {
                                    "profile_id": profile.profile_id,
                                    "read_only": True,
                                    "topics": [
                                        profile.observation.joint_state_topic,
                                        *(camera.topic for camera in profile.cameras),
                                    ],
                                },
                                sort_keys=True,
                            ),
                            flush=True,
                        )
                        ready_announced = True
                    next_tick += period_s
                    sleep_s = next_tick - time.monotonic()
                    if sleep_s > 0.0:
                        time.sleep(sleep_s)
                    elif sleep_s < -period_s:
                        next_tick = time.monotonic()
        finally:
            if publisher is not None:
                observation_report = publisher.report()
                publisher.close()

    observation_passed = (
        not publish_ros
        or (
            observation_report is not None
            and observation_report["all_rates_ok"]
            and observation_report["images_valid"]
            and observation_report["read_only"]
            and observation_report["hardware_command_count"] == 0
        )
    )
    exercise_passed = exercise_report is None or exercise_report["passed"]

    if not gui or app.is_running():
        simulation.stop()
    return {
        "status": "passed" if exercise_passed and observation_passed else "failed",
        "scene_loaded": True,
        "articulation_prim_path": profile.articulation_prim_path,
        "dof_names": dof_names,
        "controlled_joints": list(profile.controlled_joints),
        "measured_ready_rad": {
            name: float(measured_ready[index])
            for index, name in enumerate(profile.controlled_joints)
        },
        "disabled_prims": disabled_prims,
        "actuator_contract": actuators,
        "cameras": cameras,
        "exercise": exercise_report,
        "observation": observation_report,
        "ros_bridge_enabled": publish_ros,
        "hardware_commands_enabled": False,
    }


def main(args: list[str] | None = None) -> int:
    options = parse_args(args)
    report_path = options.report or _default_report_path(options.profile)
    if not report_path.is_absolute():
        report_path = WORKSPACE_ROOT / report_path
    started_at = datetime.now(timezone.utc).isoformat()
    app = None
    try:
        profiles = load_profiles(options.profiles)
        if options.profile not in profiles:
            raise ProfileError(f"unknown profile: {options.profile}")
        profile = profiles[options.profile]
        file_checks = require_profile_files(profile)

        if options.publish_ros:
            _require_ros_isolation()

        from isaaclab.app import AppLauncher

        app = AppLauncher(
            headless=not options.gui,
            enable_cameras=options.publish_ros,
        ).app
        if options.publish_ros:
            try:
                from .ros_observation import enable_observation_extensions
            except ImportError:
                from isaac_vendor.ros_observation import enable_observation_extensions

            enable_observation_extensions(app)
        result = _run_scene(
            app,
            profile,
            gui=options.gui,
            exercise=options.exercise,
            publish_ros=options.publish_ros,
            run_seconds=options.run_seconds,
        )
        report = {
            "schema_version": "sang_isaac_scene_report_v2",
            "created_at": started_at,
            "profile_id": profile.profile_id,
            "manufacturer": profile.manufacturer,
            "model": profile.model,
            "source_url": profile.source_url,
            "source_revision": profile.source_revision,
            "known_issues": list(profile.known_issues),
            "isaac_sim_version": _package_version("isaacsim"),
            "isaac_lab_version": _package_version("isaaclab"),
            "isaac_lab_repository": _isaac_lab_repository(),
            "ros_isolation": {
                "domain_id": os.getenv("ROS_DOMAIN_ID"),
                "automatic_discovery_range": os.getenv(
                    "ROS_AUTOMATIC_DISCOVERY_RANGE"
                ),
                "fastdds_builtin_transports": os.getenv(
                    "FASTDDS_BUILTIN_TRANSPORTS"
                ),
                "publication_mode": os.getenv("RMW_FASTRTPS_PUBLICATION_MODE"),
            },
            "file_checks": file_checks,
            **result,
        }
    except Exception as exc:
        report = {
            "schema_version": "sang_isaac_scene_report_v2",
            "created_at": started_at,
            "profile_id": options.profile,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "ros_bridge_enabled": options.publish_ros,
            "hardware_commands_enabled": False,
        }
    _write_report(report_path, report)
    print("SANG_ISAAC_RESULT=" + json.dumps(report, sort_keys=True), flush=True)
    print(f"SANG_ISAAC_REPORT={report_path.resolve()}", flush=True)
    exit_code = 0 if report["status"] == "passed" else 1
    if app is not None:
        app.app.post_quit(exit_code)
        app.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
