import pytest

from robot_agent.sync_metrics import Sample
from robot_agent.sync_probe import _gap_evidence, parse_args


def _sample(arrival_ms: float) -> Sample:
    return Sample(
        stream="camera",
        header_ns=int((1_000 + arrival_ms) * 1_000_000),
        arrival_wall_ns=int((2_000 + arrival_ms) * 1_000_000),
        arrival_monotonic_ns=int(arrival_ms * 1_000_000),
    )


def test_gap_evidence_adds_utc_time_and_nearest_host_sample():
    host_samples = [
        {"sampled_at_wall_ns": 2_000_000_000, "cpu_percent": 10.0},
        {"sampled_at_wall_ns": 2_400_000_000, "cpu_percent": 90.0},
    ]

    report = _gap_evidence(
        {"camera": [_sample(0), _sample(300)]},
        host_samples,
        max_gap_ms=250,
        max_events=10,
    )["camera"]

    assert report["total"] == 1
    event = report["events"][0]
    assert event["arrival_utc"].endswith("+00:00")
    assert event["nearest_host_distance_ms"] == 100.0
    assert event["nearest_host_sample"]["cpu_percent"] == 90.0


def test_parse_args_rejects_busy_loop_host_sample_period(tmp_path):
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--joint-topic",
                "/joint_states",
                "--camera",
                "cam=/camera/compressed",
                "--output-dir",
                str(tmp_path),
                "--host-sample-period-s",
                "0.01",
            ]
        )
