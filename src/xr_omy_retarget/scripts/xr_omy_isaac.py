#!/usr/bin/env python3
"""Run the Galaxy XR OMY target adapter against the supplied Isaac stage."""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import sys
import time


SCRIPT_PATH = Path(__file__).resolve()
PACKAGE_ROOT = SCRIPT_PATH.parents[1]
PROJECT_ROOT = SCRIPT_PATH.parents[4]
sys.path.insert(0, str(PACKAGE_ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        default=str(
            PROJECT_ROOT
            / "issacsim_setting_usd"
            / "OMY_ai3m"
            / "usd"
            / "omy_f3m.usd"
        ),
    )
    parser.add_argument(
        "--articulation-prim", default="/World/OMY/OMY/world"
    )
    parser.add_argument(
        "--tcp-prim",
        default=(
            "/World/OMY/OMY/link6/end_effector_flange_link/"
            "end_effector_link"
        ),
    )
    parser.add_argument("--action-graph-prim", default="/World/ActionGraph")
    parser.add_argument("--endpoint", default="tcp://127.0.0.1:5559")
    parser.add_argument("--stale-timeout", type=float, default=0.25)
    parser.add_argument("--max-arm-speed", type=float, default=1.2)
    parser.add_argument("--max-gripper-speed", type=float, default=2.0)
    parser.add_argument("--arm-stiffness", type=float, default=5000.0)
    parser.add_argument("--arm-damping", type=float, default=100.0)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--self-test-seconds", type=float, default=4.5)
    return parser.parse_args()


ARGS = parse_args()

from isaacsim import SimulationApp


simulation_app = SimulationApp({"headless": ARGS.headless})

import numpy as np
from isaacsim.core.api import World
from isaacsim.core.prims import SingleArticulation
from isaacsim.core.utils.stage import get_current_stage, open_stage
from isaacsim.core.utils.types import ArticulationAction
from pxr import UsdGeom
import zmq

from xr_omy_retarget.isaac_protocol import TargetStream
from xr_omy_retarget.omy_kinematics import (
    ARM_JOINT_NAMES,
    READY_JOINTS,
    forward_kinematics,
    quaternion_xyzw_from_rotation,
    rotation_from_quaternion_xyzw,
    rotation_vector,
    solve_ik,
)


DRIVEN_GRIPPER_JOINT = "rh_r1_joint"
ROBOTIS_GRIPPER_CLOSED_RAD = 1.13514578304
IK_RETRY_PERIOD_S = 0.05


class ZmqTargetSubscriber:
    def __init__(self, endpoint: str) -> None:
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.SUB)
        self._socket.setsockopt(zmq.LINGER, 0)
        self._socket.setsockopt(zmq.RCVHWM, 1)
        self._socket.setsockopt(zmq.CONFLATE, 1)
        self._socket.setsockopt(zmq.SUBSCRIBE, b"")
        self._socket.connect(endpoint)

    def receive_latest(self):
        latest = None
        while self._socket.poll(timeout=0):
            latest = self._socket.recv_json(flags=zmq.NOBLOCK)
        return latest

    def close(self) -> None:
        self._socket.close(linger=0)
        self._context.term()


def synthetic_document(sequence: int, position, orientation) -> dict:
    return {
        "protocol": "xr_omy_target_v1",
        "sequence": sequence,
        "ros_stamp_ns": time.time_ns(),
        "target_link": "end_effector_link",
        "target": {
            "frame_id": "link0",
            "position": np.asarray(position, dtype=float).tolist(),
            "orientation_xyzw": np.asarray(orientation, dtype=float).tolist(),
        },
        "gripper": 0.65,
        "enabled": True,
        "inhibit_reason": "enabled",
        "clutch_epoch": 1,
    }


def local_prim_pose(base_prim: str, target_prim: str):
    stage = get_current_stage()
    cache = UsdGeom.XformCache()
    base_world = cache.GetLocalToWorldTransform(stage.GetPrimAtPath(base_prim))
    target_world = cache.GetLocalToWorldTransform(stage.GetPrimAtPath(target_prim))
    local = target_world * base_world.GetInverse()
    translation = local.ExtractTranslation()
    quaternion = local.ExtractRotationQuat()
    imaginary = quaternion.GetImaginary()
    return (
        np.array(translation, dtype=float),
        np.array(
            [imaginary[0], imaginary[1], imaginary[2], quaternion.GetReal()],
            dtype=float,
        ),
    )


def move_toward(current: np.ndarray, goal: np.ndarray, max_delta: np.ndarray):
    return current + np.clip(goal - current, -max_delta, max_delta)


def run() -> int:
    stage_path = Path(ARGS.stage).expanduser().resolve()
    if not stage_path.is_file():
        raise FileNotFoundError(f"OMY stage not found: {stage_path}")
    for name, value in (
        ("stale_timeout", ARGS.stale_timeout),
        ("max_arm_speed", ARGS.max_arm_speed),
        ("max_gripper_speed", ARGS.max_gripper_speed),
        ("arm_stiffness", ARGS.arm_stiffness),
        ("arm_damping", ARGS.arm_damping),
        ("self_test_seconds", ARGS.self_test_seconds),
    ):
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be positive and finite")

    print(f"XR_OMY loading stage={stage_path}", flush=True)
    open_stage(str(stage_path))
    stage = get_current_stage()
    if not stage.GetPrimAtPath(ARGS.articulation_prim).IsValid():
        raise RuntimeError(
            f"articulation prim not found: {ARGS.articulation_prim}"
        )
    if not stage.GetPrimAtPath(ARGS.tcp_prim).IsValid():
        raise RuntimeError(f"TCP prim not found: {ARGS.tcp_prim}")
    action_graph = stage.GetPrimAtPath(ARGS.action_graph_prim)
    if action_graph.IsValid():
        action_graph.SetActive(False)
        print(
            f"XR_OMY disabled competing graph={ARGS.action_graph_prim}",
            flush=True,
        )

    world = World(stage_units_in_meters=1.0, backend="numpy")
    robot = world.scene.add(
        SingleArticulation(ARGS.articulation_prim, name="xr_omy")
    )
    world.reset()

    dof_names = list(robot.dof_names)
    required_names = list(ARM_JOINT_NAMES) + [DRIVEN_GRIPPER_JOINT]
    missing = [name for name in required_names if name not in dof_names]
    if missing:
        raise RuntimeError(f"USD articulation is missing DOFs: {missing}")
    arm_indices = np.array([dof_names.index(name) for name in ARM_JOINT_NAMES])
    gripper_index = dof_names.index(DRIVEN_GRIPPER_JOINT)
    command_indices = np.concatenate((arm_indices, [gripper_index])).astype(int)

    properties = robot.dof_properties
    arm_limits = np.column_stack(
        (properties["lower"][arm_indices], properties["upper"][arm_indices])
    )
    gripper_open = max(0.0, float(properties["lower"][gripper_index]))
    gripper_closed = min(
        ROBOTIS_GRIPPER_CLOSED_RAD,
        float(properties["upper"][gripper_index]),
    )
    if gripper_closed <= gripper_open:
        raise RuntimeError("driven gripper DOF has invalid limits")

    articulation_controller = robot.get_articulation_controller()
    stiffnesses, dampings = articulation_controller.get_gains()
    stiffnesses[arm_indices] = ARGS.arm_stiffness
    dampings[arm_indices] = ARGS.arm_damping
    articulation_controller.set_gains(stiffnesses, dampings)

    initial = np.concatenate((READY_JOINTS, [gripper_open]))
    robot.set_joint_positions(initial, joint_indices=command_indices)
    robot.set_joint_velocities(
        np.zeros(len(command_indices)), joint_indices=command_indices
    )
    robot.apply_action(
        ArticulationAction(
            joint_positions=initial.copy(), joint_indices=command_indices
        )
    )
    for _ in range(10):
        world.step(render=not ARGS.headless)

    print(
        "XR_OMY articulation ready "
        f"dofs={dof_names} command_indices={command_indices.tolist()} "
        f"gripper_range=[{gripper_open:.4f}, {gripper_closed:.4f}] "
        f"arm_gains=[{ARGS.arm_stiffness:.1f}, {ARGS.arm_damping:.1f}]",
        flush=True,
    )

    stream = TargetStream(stale_timeout_s=ARGS.stale_timeout)
    self_test_context = None
    self_test_publisher = None
    endpoint = ARGS.endpoint
    if ARGS.self_test:
        self_test_context = zmq.Context()
        self_test_publisher = self_test_context.socket(zmq.PUB)
        self_test_publisher.setsockopt(zmq.LINGER, 0)
        self_test_publisher.setsockopt(zmq.SNDHWM, 1)
        self_test_publisher.setsockopt(zmq.CONFLATE, 1)
        port = self_test_publisher.bind_to_random_port("tcp://127.0.0.1")
        endpoint = f"tcp://127.0.0.1:{port}"
    subscriber = ZmqTargetSubscriber(endpoint)
    print(f"XR_OMY connected endpoint={endpoint}", flush=True)

    goal = initial.copy()
    commanded = initial.copy()
    previous_active = False
    processed_sequence = None
    processed_sequence_usable = False
    processed_sequence_reason = "target_not_processed"
    last_reported_reason = None
    start_s = time.monotonic()
    next_synthetic_s = start_s
    synthetic_sequence = 0
    self_test_goal_joints = READY_JOINTS + np.array(
        [0.05, 0.05, -0.08, 0.03, -0.05, 0.07]
    )
    self_test_position, self_test_rotation = forward_kinematics(
        self_test_goal_joints
    )
    self_test_orientation = quaternion_xyzw_from_rotation(self_test_rotation)
    stale_phase_start = min(3.0, ARGS.self_test_seconds * 0.65)
    hold_snapshot = None
    last_ik = None
    next_ik_attempt_s = 0.0
    step_count = 0
    received_documents = 0

    try:
        while simulation_app.is_running():
            now_s = time.monotonic()
            elapsed_s = now_s - start_s
            if ARGS.self_test:
                if elapsed_s < stale_phase_start and now_s >= next_synthetic_s:
                    assert self_test_publisher is not None
                    synthetic_sequence += 1
                    self_test_publisher.send_json(
                        synthetic_document(
                            synthetic_sequence,
                            self_test_position,
                            self_test_orientation,
                        )
                    )
                    next_synthetic_s = now_s + 1.0 / 30.0
            try:
                document = subscriber.receive_latest()
                if document is not None:
                    stream.ingest(document, now_s)
                    received_documents += 1
            except (TypeError, ValueError) as exc:
                stream.invalidate(now_s)
                print(f"XR_OMY rejected target: {exc}", flush=True)

            status = stream.status(now_s)
            control_active = status.active
            if status.active and status.command is not None:
                command = status.command
                if command.sequence != processed_sequence:
                    if now_s >= next_ik_attempt_s:
                        assert command.position is not None
                        assert command.orientation_xyzw is not None
                        last_ik = solve_ik(
                            command.position,
                            command.orientation_xyzw,
                            goal[:6],
                            joint_limits=arm_limits,
                        )
                        usable = last_ik.converged or (
                            last_ik.position_error_m <= 0.006
                            and last_ik.orientation_error_rad <= math.radians(5.0)
                        )
                        if usable:
                            goal[:6] = last_ik.joints
                            goal[6] = gripper_open + command.gripper * (
                                gripper_closed - gripper_open
                            )
                            next_ik_attempt_s = 0.0
                            processed_sequence_usable = True
                            processed_sequence_reason = "enabled"
                        else:
                            next_ik_attempt_s = now_s + IK_RETRY_PERIOD_S
                            processed_sequence_usable = False
                            processed_sequence_reason = (
                                "ik_failed:"
                                f"{last_ik.position_error_m:.4f}m/"
                                f"{math.degrees(last_ik.orientation_error_rad):.1f}deg"
                            )
                    else:
                        processed_sequence_usable = False
                        processed_sequence_reason = "ik_retry_cooldown"
                    processed_sequence = command.sequence
                control_active = processed_sequence_usable
                if not control_active:
                    status_reason = processed_sequence_reason

            if not control_active and previous_active:
                measured = np.asarray(
                    robot.get_joint_positions(joint_indices=command_indices),
                    dtype=float,
                )
                goal = measured.copy()
                commanded = measured.copy()
                if ARGS.self_test and hold_snapshot is None:
                    hold_snapshot = measured.copy()

            if control_active or not status.active:
                status_reason = status.reason
            if status_reason != last_reported_reason:
                print(
                    f"XR_OMY state={status_reason} age_s={status.age_s:.3f}",
                    flush=True,
                )
                last_reported_reason = status_reason

            dt = float(world.get_physics_dt())
            max_delta = np.array(
                [ARGS.max_arm_speed * dt] * 6
                + [ARGS.max_gripper_speed * dt]
            )
            commanded = move_toward(commanded, goal, max_delta)
            robot.apply_action(
                ArticulationAction(
                    joint_positions=commanded.copy(),
                    joint_indices=command_indices,
                )
            )
            world.step(render=not ARGS.headless)
            step_count += 1
            previous_active = control_active

            if ARGS.self_test and elapsed_s >= ARGS.self_test_seconds:
                measured = np.asarray(
                    robot.get_joint_positions(joint_indices=command_indices),
                    dtype=float,
                )
                measured_position, measured_rotation = forward_kinematics(
                    measured[:6]
                )
                position_error = float(
                    np.linalg.norm(self_test_position - measured_position)
                )
                orientation_error = float(
                    np.linalg.norm(
                        rotation_vector(
                            self_test_rotation @ measured_rotation.T
                        )
                    )
                )
                gripper_target = gripper_open + 0.65 * (
                    gripper_closed - gripper_open
                )
                gripper_error = abs(float(measured[6]) - gripper_target)
                hold_drift = (
                    0.0
                    if hold_snapshot is None
                    else float(np.max(np.abs(measured - hold_snapshot)))
                )
                usd_position, _ = local_prim_pose(
                    ARGS.articulation_prim, ARGS.tcp_prim
                )
                usd_fk_error = float(
                    np.linalg.norm(usd_position - measured_position)
                )
                if last_ik is None:
                    joint_error = math.inf
                    ik_position_error = math.inf
                    ik_orientation_error = math.inf
                else:
                    joint_error = float(
                        np.max(np.abs(measured[:6] - last_ik.joints))
                    )
                    ik_position_error = last_ik.position_error_m
                    ik_orientation_error = last_ik.orientation_error_rad
                passed = (
                    last_ik is not None
                    and received_documents > 0
                    and position_error <= 0.012
                    and orientation_error <= math.radians(5.0)
                    and gripper_error <= 0.12
                    and hold_snapshot is not None
                    and hold_drift <= 0.03
                    and usd_fk_error <= 0.003
                )
                print(
                    "XR_OMY_SELF_TEST "
                    f"{'PASS' if passed else 'FAIL'} "
                    f"position_error_m={position_error:.5f} "
                    f"orientation_error_deg={math.degrees(orientation_error):.3f} "
                    f"gripper_error_rad={gripper_error:.5f} "
                    f"hold_drift_rad={hold_drift:.5f} "
                    f"usd_fk_error_m={usd_fk_error:.5f} "
                    f"joint_error_rad={joint_error:.5f} "
                    f"ik_position_error_m={ik_position_error:.5f} "
                    f"ik_orientation_error_deg="
                    f"{math.degrees(ik_orientation_error):.3f} "
                    f"zmq_frames={received_documents} steps={step_count}",
                    flush=True,
                )
                return 0 if passed else 1
    finally:
        subscriber.close()
        if self_test_publisher is not None:
            self_test_publisher.close(linger=0)
        if self_test_context is not None:
            self_test_context.term()
        world.stop()
    return 0


if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = run()
    except KeyboardInterrupt:
        exit_code = 130
    except Exception as exc:
        print(f"XR_OMY_FATAL {type(exc).__name__}: {exc}", flush=True)
        exit_code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    if exit_code != 0:
        os._exit(exit_code)
    simulation_app.close()
