from pathlib import Path

import pytest

from robot_agent.host_metrics import HostMetricsSampler, summarize_host_samples


def _write_fixture(root: Path, *, second: bool) -> tuple[Path, Path]:
    proc = root / "proc"
    cgroup = root / "cgroup"
    (proc / "self").mkdir(parents=True, exist_ok=True)
    (proc / "pressure").mkdir(parents=True, exist_ok=True)
    cgroup.mkdir(parents=True, exist_ok=True)
    if second:
        (proc / "stat").write_text("cpu 150 0 70 870 10 0 0 0\n", encoding="utf-8")
        (proc / "self" / "schedstat").write_text(
            "30000000 50000000 8\n", encoding="utf-8"
        )
        (cgroup / "cpu.stat").write_text(
            "usage_usec 50000\nnr_throttled 3\nthrottled_usec 6000\n",
            encoding="utf-8",
        )
    else:
        (proc / "stat").write_text("cpu 100 0 50 850 0 0 0 0\n", encoding="utf-8")
        (proc / "self" / "schedstat").write_text(
            "10000000 20000000 3\n", encoding="utf-8"
        )
        (cgroup / "cpu.stat").write_text(
            "usage_usec 10000\nnr_throttled 1\nthrottled_usec 1000\n",
            encoding="utf-8",
        )
    (proc / "loadavg").write_text("1.0 2.0 3.0 4/100 123\n", encoding="utf-8")
    (proc / "pressure" / "cpu").write_text(
        "some avg10=1.25 avg60=0.50 avg300=0.10 total=1\n",
        encoding="utf-8",
    )
    (proc / "pressure" / "io").write_text(
        "some avg10=2.50 avg60=1.00 avg300=0.20 total=2\n"
        "full avg10=0.75 avg60=0.25 avg300=0.05 total=1\n",
        encoding="utf-8",
    )
    return proc, cgroup


def test_host_sampler_reports_counter_deltas_and_scheduling_lateness(tmp_path):
    proc, cgroup = _write_fixture(tmp_path, second=False)
    sampler = HostMetricsSampler(proc_root=proc, cgroup_root=cgroup)

    first = sampler.sample(
        scheduled_monotonic_ns=1_000_000_000,
        monotonic_ns=1_010_000_000,
        wall_ns=2_000_000_000,
    )
    _write_fixture(tmp_path, second=True)
    second = sampler.sample(
        scheduled_monotonic_ns=2_000_000_000,
        monotonic_ns=2_050_000_000,
        wall_ns=3_000_000_000,
    )

    assert first["cpu_percent"] is None
    assert second["scheduled_lateness_ms"] == 50.0
    assert second["sample_interval_ms"] == 1040.0
    assert second["cpu_percent"] == pytest.approx(70.0)
    assert second["iowait_percent"] == pytest.approx(10.0)
    assert second["runnable_tasks"] == 4
    assert second["process_runtime_delta_ms"] == 20.0
    assert second["process_wait_delta_ms"] == 30.0
    assert second["process_timeslices_delta"] == 5
    assert second["cgroup_nr_throttled_delta"] == 2
    assert second["cgroup_throttled_delta_ms"] == 5.0
    assert second["cpu_pressure_some_avg10"] == 1.25
    assert second["io_pressure_full_avg10"] == 0.75
    assert second["unavailable"] == []

    summary = summarize_host_samples([first, second])
    assert summary["count"] == 2
    assert summary["metrics"]["process_wait_delta_ms"]["max"] == 30.0


def test_host_sampler_isolates_unavailable_optional_sources(tmp_path):
    proc = tmp_path / "proc"
    cgroup = tmp_path / "cgroup"
    proc.mkdir()
    cgroup.mkdir()
    (proc / "stat").write_text("invalid\n", encoding="utf-8")

    sample = HostMetricsSampler(proc_root=proc, cgroup_root=cgroup).sample(
        scheduled_monotonic_ns=1,
        monotonic_ns=1,
        wall_ns=1,
    )

    assert sample["sampled_at_wall_ns"] == 1
    assert any(item.startswith("cpu:") for item in sample["unavailable"])
