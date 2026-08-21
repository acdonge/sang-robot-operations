"""Read-only ROS 2 probe for camera and robot-state synchronization."""

from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import defaultdict, deque
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sys
import time

from robot_agent.host_metrics import HostMetricsSampler, summarize_host_samples
from robot_agent.sync_metrics import (
    Sample,
    evaluate_summary,
    nearest_skew_metrics,
    over_limit_gap_events,
    stream_metrics,
)


def _parse_camera(value: str) -> tuple[str, str]:
    key, separator, topic = value.partition("=")
    if not separator or not key or not topic.startswith("/"):
        raise argparse.ArgumentTypeError("camera must use KEY=/absolute/topic")
    return key, topic


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure ROS sensor synchronization")
    parser.add_argument("--joint-topic", required=True)
    parser.add_argument("--camera", action="append", type=_parse_camera, required=True)
    parser.add_argument("--duration-s", type=float, default=600.0)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--minimum-joint-rate-hz", type=float, default=20.0)
    parser.add_argument("--minimum-camera-rate-hz", type=float, default=25.0)
    parser.add_argument("--max-gap-ms", type=float, default=250.0)
    parser.add_argument("--max-p95-skew-ms", type=float, default=50.0)
    parser.add_argument("--max-skew-ms", type=float, default=150.0)
    parser.add_argument("--clock-step-ms", type=float, default=20.0)
    parser.add_argument("--motion-epsilon-rad", type=float, default=1e-5)
    parser.add_argument("--minimum-moving-samples", type=int, default=0)
    parser.add_argument("--host-sample-period-s", type=float, default=1.0)
    parser.add_argument("--max-host-samples", type=int, default=10_000)
    parser.add_argument("--max-gap-events-per-stream", type=int, default=100)
    options = parser.parse_args(args)
    if options.duration_s <= 0:
        parser.error("duration-s must be positive")
    if len({key for key, _ in options.camera}) != len(options.camera):
        parser.error("camera keys must be unique")
    if options.host_sample_period_s < 0.1:
        parser.error("host-sample-period-s must be at least 0.1")
    if options.max_host_samples <= 0 or options.max_gap_events_per_stream <= 0:
        parser.error("host and gap event limits must be positive")
    return options


def _stamp_ns(message) -> int:
    return int(message.header.stamp.sec) * 1_000_000_000 + int(
        message.header.stamp.nanosec
    )


def _utc_from_ns(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc).isoformat()


def _gap_evidence(
    streams: dict[str, list[Sample]],
    host_samples: list[dict],
    *,
    max_gap_ms: float,
    max_events: int,
) -> dict:
    host_stamps = [sample["sampled_at_wall_ns"] for sample in host_samples]
    evidence = {}
    for name, samples in streams.items():
        report = over_limit_gap_events(
            samples,
            max_gap_ms=max_gap_ms,
            max_events=max_events,
        )
        for event in report["events"]:
            event["arrival_utc"] = _utc_from_ns(event["arrival_wall_ns"])
            if not host_stamps:
                event["nearest_host_sample"] = None
                continue
            index = bisect_left(host_stamps, event["arrival_wall_ns"])
            candidates = []
            if index < len(host_samples):
                candidates.append(host_samples[index])
            if index > 0:
                candidates.append(host_samples[index - 1])
            nearest = min(
                candidates,
                key=lambda sample: abs(
                    sample["sampled_at_wall_ns"] - event["arrival_wall_ns"]
                ),
            )
            event["nearest_host_distance_ms"] = abs(
                nearest["sampled_at_wall_ns"] - event["arrival_wall_ns"]
            ) / 1_000_000.0
            event["nearest_host_sample"] = nearest
        evidence[name] = report
    return evidence


def run_probe(options: argparse.Namespace) -> tuple[dict, Path]:
    import rclpy
    from rclpy.executors import ExternalShutdownException
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import CompressedImage, JointState

    options.output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)
    probe_id = started_at.strftime("%Y%m%dT%H%M%S.%fZ")
    samples_path = options.output_dir / f"sync-probe-{probe_id}.jsonl"
    host_samples_path = options.output_dir / f"sync-probe-{probe_id}.host.jsonl"
    summary_path = options.output_dir / f"sync-probe-{probe_id}.summary.json"
    samples: dict[str, list[Sample]] = defaultdict(list)
    host_samples = deque(maxlen=options.max_host_samples)
    host_sample_count = 0
    host_sampler = HostMetricsSampler()
    previous_positions = None
    previous_clock_offset_ns = None
    clock_steps = []
    stop_requested = False

    def request_stop(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    previous_sigint = signal.signal(signal.SIGINT, request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    rclpy.init(args=None)
    node = rclpy.create_node("sang_sensor_sync_probe")

    with (
        samples_path.open("w", encoding="utf-8", buffering=1) as output,
        host_samples_path.open("w", encoding="utf-8", buffering=1) as host_output,
    ):
        def record(stream: str, message, *, positions=None) -> None:
            nonlocal previous_positions, previous_clock_offset_ns
            arrival_wall_ns = time.time_ns()
            arrival_monotonic_ns = time.monotonic_ns()
            offset_ns = arrival_wall_ns - arrival_monotonic_ns
            if previous_clock_offset_ns is not None:
                step_ns = offset_ns - previous_clock_offset_ns
                if abs(step_ns) > options.clock_step_ms * 1_000_000:
                    clock_steps.append(
                        {
                            "arrival_wall_ns": arrival_wall_ns,
                            "step_ms": step_ns / 1_000_000.0,
                        }
                    )
            previous_clock_offset_ns = offset_ns

            moving = False
            position_values = None
            if positions is not None:
                position_values = [float(value) for value in positions]
                if previous_positions is not None and len(previous_positions) == len(position_values):
                    moving = max(
                        abs(current - previous)
                        for current, previous in zip(position_values, previous_positions)
                    ) >= options.motion_epsilon_rad
                previous_positions = position_values

            sample = Sample(
                stream=stream,
                header_ns=_stamp_ns(message),
                arrival_wall_ns=arrival_wall_ns,
                arrival_monotonic_ns=arrival_monotonic_ns,
                moving=moving,
            )
            samples[stream].append(sample)
            row = {
                "stream": stream,
                "header_ns": sample.header_ns,
                "arrival_wall_ns": arrival_wall_ns,
                "arrival_monotonic_ns": arrival_monotonic_ns,
                "moving": moving,
            }
            if position_values is not None:
                row["position_rad"] = position_values
            output.write(json.dumps(row, separators=(",", ":")) + "\n")

        subscriptions = [
            node.create_subscription(
                JointState,
                options.joint_topic,
                lambda message: record("rb5_joint", message, positions=message.position),
                qos_profile_sensor_data,
            )
        ]
        for key, topic in options.camera:
            subscriptions.append(
                node.create_subscription(
                    CompressedImage,
                    topic,
                    lambda message, stream=key: record(stream, message),
                    qos_profile_sensor_data,
                )
            )

        deadline = time.monotonic() + options.duration_s
        host_period_ns = int(options.host_sample_period_s * 1_000_000_000)
        next_host_sample_ns = time.monotonic_ns()
        while rclpy.ok() and not stop_requested and time.monotonic() < deadline:
            now_monotonic_ns = time.monotonic_ns()
            if now_monotonic_ns >= next_host_sample_ns:
                host_sample = host_sampler.sample(
                    scheduled_monotonic_ns=next_host_sample_ns,
                    monotonic_ns=now_monotonic_ns,
                )
                host_samples.append(host_sample)
                host_sample_count += 1
                host_output.write(
                    json.dumps(host_sample, separators=(",", ":")) + "\n"
                )
                periods_elapsed = max(
                    1,
                    (now_monotonic_ns - next_host_sample_ns) // host_period_ns + 1,
                )
                next_host_sample_ns += periods_elapsed * host_period_ns
            timeout_s = min(
                0.1,
                max(0.0, deadline - time.monotonic()),
                max(0.0, (next_host_sample_ns - time.monotonic_ns()) / 1e9),
            )
            try:
                rclpy.spin_once(node, timeout_sec=timeout_s)
            except ExternalShutdownException:
                stop_requested = True
                break

    finished_at = datetime.now(timezone.utc)
    stream_names = ["rb5_joint", *[key for key, _ in options.camera]]
    metrics = {
        name: stream_metrics(samples[name], max_gap_ms=options.max_gap_ms)
        for name in stream_names
    }
    sync = {
        key: {
            "all": nearest_skew_metrics(samples["rb5_joint"], samples[key]),
            "moving": nearest_skew_metrics(
                samples["rb5_joint"], samples[key], moving_only=True
            ),
        }
        for key, _ in options.camera
    }
    retained_host_samples = list(host_samples)
    host_summary = summarize_host_samples(retained_host_samples)
    host_summary.update(
        {
            "total_count": host_sample_count,
            "retained_count": len(retained_host_samples),
            "truncated": host_sample_count > len(retained_host_samples),
        }
    )
    gap_evidence = _gap_evidence(
        samples,
        retained_host_samples,
        max_gap_ms=options.max_gap_ms,
        max_events=options.max_gap_events_per_stream,
    )
    checks = evaluate_summary(
        streams=metrics,
        sync=sync,
        joint_stream="rb5_joint",
        camera_streams=[key for key, _ in options.camera],
        minimum_joint_rate_hz=options.minimum_joint_rate_hz,
        minimum_camera_rate_hz=options.minimum_camera_rate_hz,
        max_gap_ms=options.max_gap_ms,
        max_p95_skew_ms=options.max_p95_skew_ms,
        max_skew_ms=options.max_skew_ms,
        clock_step_count=len(clock_steps),
        minimum_moving_samples=options.minimum_moving_samples,
    )
    summary = {
        "schema_version": "0.2",
        "probe_id": probe_id,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "requested_duration_s": options.duration_s,
        "completed_duration_s": (finished_at - started_at).total_seconds(),
        "interrupted": stop_requested,
        "provenance": {
            "rb5_joint": "host timestamp immediately after vendor CobotData read",
            "camera": "host timestamp when OpenCV capture returned; not hardware exposure time",
            "arrival": "probe process CLOCK_REALTIME and CLOCK_MONOTONIC",
            "host": "Linux /proc, current process schedstat, and cgroup v2 cpu.stat",
            "image_payload_recorded": False,
        },
        "topics": {
            "rb5_joint": options.joint_topic,
            **dict(options.camera),
        },
        "thresholds": {
            "minimum_joint_rate_hz": options.minimum_joint_rate_hz,
            "minimum_camera_rate_hz": options.minimum_camera_rate_hz,
            "max_gap_ms": options.max_gap_ms,
            "max_p95_skew_ms": options.max_p95_skew_ms,
            "max_skew_ms": options.max_skew_ms,
            "clock_step_ms": options.clock_step_ms,
            "motion_epsilon_rad": options.motion_epsilon_rad,
            "minimum_moving_samples": options.minimum_moving_samples,
            "host_sample_period_s": options.host_sample_period_s,
            "max_host_samples": options.max_host_samples,
            "max_gap_events_per_stream": options.max_gap_events_per_stream,
        },
        "streams": metrics,
        "gap_events": gap_evidence,
        "nearest_camera_joint_skew": sync,
        "host_telemetry": {
            "samples_file": host_samples_path.name,
            "summary": host_summary,
        },
        "clock_steps": clock_steps,
        "checks": checks,
        "passed": not stop_requested and all(check["passed"] for check in checks),
        "samples_file": samples_path.name,
    }
    temporary_path = summary_path.with_suffix(summary_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary_path.replace(summary_path)
    node.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()
    signal.signal(signal.SIGINT, previous_sigint)
    signal.signal(signal.SIGTERM, previous_sigterm)
    return summary, summary_path


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    summary, summary_path = run_probe(options)
    print(json.dumps({"passed": summary["passed"], "summary": str(summary_path)}))
    raise SystemExit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main(sys.argv[1:])
