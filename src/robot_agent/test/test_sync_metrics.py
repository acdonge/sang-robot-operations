import pytest

from robot_agent.sync_metrics import (
    Sample,
    evaluate_summary,
    nearest_skew_metrics,
    over_limit_gap_events,
    stream_metrics,
)


def sample(stream, header_ms, arrival_ms, *, moving=False):
    return Sample(
        stream=stream,
        header_ns=int(header_ms * 1_000_000),
        arrival_wall_ns=int((1_000 + arrival_ms) * 1_000_000),
        arrival_monotonic_ns=int(arrival_ms * 1_000_000),
        moving=moving,
    )


def test_stream_metrics_reports_rate_gap_and_timestamp_regression():
    samples = [
        sample("camera", 100, 0),
        sample("camera", 110, 10),
        sample("camera", 105, 30),
    ]

    metrics = stream_metrics(samples, max_gap_ms=15)

    assert metrics["count"] == 3
    assert metrics["rate_hz"] == pytest.approx(66.6666667)
    assert metrics["arrival_gap"]["max_ms"] == 20.0
    assert metrics["gaps_over_limit"] == 1
    assert metrics["timestamp_regressions"] == 1


def test_nearest_skew_uses_only_overlapping_camera_window_and_motion_subset():
    joints = [
        sample("joint", 90, 0),
        sample("joint", 102, 10, moving=True),
        sample("joint", 118, 20, moving=False),
        sample("joint", 140, 30, moving=True),
    ]
    cameras = [sample("camera", 100, 0), sample("camera", 120, 20)]

    all_metrics = nearest_skew_metrics(joints, cameras)
    moving_metrics = nearest_skew_metrics(joints, cameras, moving_only=True)

    assert all_metrics["count"] == 2
    assert all_metrics["excluded_outside_window"] == 2
    assert all_metrics["max_ms"] == 2.0
    assert moving_metrics["count"] == 1
    assert moving_metrics["max_ms"] == 2.0


def test_over_limit_gap_events_are_timestamped_and_bounded():
    samples = [
        sample("camera", 100, 0),
        sample("camera", 110, 20),
        sample("camera", 120, 45),
        sample("camera", 130, 75),
    ]

    report = over_limit_gap_events(samples, max_gap_ms=15, max_events=2)

    assert report["total"] == 3
    assert report["truncated"] is True
    assert len(report["events"]) == 2
    assert report["events"][0]["gap_ms"] == 20.0
    assert report["events"][0]["arrival_wall_ns"] == 1_020_000_000


def test_evaluation_requires_rates_gaps_skew_and_motion_when_requested():
    streams = {
        "rb5_joint": {
            "rate_hz": 30.0,
            "arrival_gap": {"max_ms": 40.0},
            "timestamp_regressions": 0,
            "zero_header_timestamps": 0,
        },
        "otos": {
            "rate_hz": 29.0,
            "arrival_gap": {"max_ms": 45.0},
            "timestamp_regressions": 0,
            "zero_header_timestamps": 0,
        },
    }
    sync = {
        "otos": {
            "all": {"p95_ms": 12.0, "max_ms": 25.0},
            "moving": {"count": 40, "p95_ms": 15.0},
        }
    }

    checks = evaluate_summary(
        streams=streams,
        sync=sync,
        joint_stream="rb5_joint",
        camera_streams=["otos"],
        minimum_joint_rate_hz=20,
        minimum_camera_rate_hz=25,
        max_gap_ms=250,
        max_p95_skew_ms=50,
        max_skew_ms=150,
        clock_step_count=0,
        minimum_moving_samples=30,
    )

    assert checks
    assert all(check["passed"] for check in checks)
