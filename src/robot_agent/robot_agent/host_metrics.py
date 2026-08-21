"""Bounded Linux host telemetry for synchronization diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Sequence

from robot_agent.sync_metrics import percentile


@dataclass(frozen=True)
class _CpuCounters:
    total: int
    idle: int
    iowait: int


@dataclass(frozen=True)
class _SchedCounters:
    runtime_ns: int
    wait_ns: int
    timeslices: int


def _key_values(path: Path) -> dict[str, int]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) == 2:
            values[fields[0]] = int(fields[1])
    return values


def _pressure_avg10(path: Path) -> dict[str, float]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if not fields:
            continue
        for field in fields[1:]:
            key, separator, value = field.partition("=")
            if separator and key == "avg10":
                values[fields[0]] = float(value)
    return values


class HostMetricsSampler:
    """Read host and current-process counters without external dependencies."""

    def __init__(
        self,
        *,
        proc_root: Path = Path("/proc"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
    ) -> None:
        self._proc_root = proc_root
        self._cgroup_root = cgroup_root
        self._previous_cpu: _CpuCounters | None = None
        self._previous_sched: _SchedCounters | None = None
        self._previous_cgroup: dict[str, int] | None = None
        self._previous_monotonic_ns: int | None = None

    def _cpu_counters(self) -> _CpuCounters:
        fields = (self._proc_root / "stat").read_text(encoding="utf-8").splitlines()[0].split()
        if not fields or fields[0] != "cpu" or len(fields) < 6:
            raise ValueError("/proc/stat aggregate CPU line is unavailable")
        counters = [int(value) for value in fields[1:9]]
        return _CpuCounters(
            total=sum(counters),
            idle=counters[3],
            iowait=counters[4],
        )

    def _load(self) -> dict[str, float | int]:
        fields = (self._proc_root / "loadavg").read_text(encoding="utf-8").split()
        runnable, separator, tasks = fields[3].partition("/")
        if not separator:
            raise ValueError("/proc/loadavg task counts are unavailable")
        return {
            "load_1m": float(fields[0]),
            "load_5m": float(fields[1]),
            "load_15m": float(fields[2]),
            "runnable_tasks": int(runnable),
            "total_tasks": int(tasks),
        }

    def _sched_counters(self) -> _SchedCounters:
        fields = (self._proc_root / "self" / "schedstat").read_text(encoding="utf-8").split()
        return _SchedCounters(*(int(value) for value in fields[:3]))

    def sample(
        self,
        *,
        scheduled_monotonic_ns: int,
        wall_ns: int | None = None,
        monotonic_ns: int | None = None,
    ) -> dict[str, Any]:
        wall_ns = time.time_ns() if wall_ns is None else wall_ns
        monotonic_ns = time.monotonic_ns() if monotonic_ns is None else monotonic_ns
        sample: dict[str, Any] = {
            "sampled_at_wall_ns": wall_ns,
            "sampled_at_monotonic_ns": monotonic_ns,
            "scheduled_lateness_ms": max(
                0.0,
                (monotonic_ns - scheduled_monotonic_ns) / 1_000_000.0,
            ),
            "sample_interval_ms": (
                None
                if self._previous_monotonic_ns is None
                else (monotonic_ns - self._previous_monotonic_ns) / 1_000_000.0
            ),
        }
        unavailable = []
        self._previous_monotonic_ns = monotonic_ns

        try:
            cpu = self._cpu_counters()
            previous = self._previous_cpu
            if previous is None:
                sample.update({"cpu_percent": None, "iowait_percent": None})
            else:
                total = cpu.total - previous.total
                idle = cpu.idle - previous.idle
                iowait = cpu.iowait - previous.iowait
                sample["cpu_percent"] = (
                    max(0.0, min(100.0, 100.0 * (total - idle - iowait) / total))
                    if total > 0
                    else None
                )
                sample["iowait_percent"] = (
                    max(0.0, 100.0 * iowait / total) if total > 0 else None
                )
            self._previous_cpu = cpu
        except (OSError, ValueError, IndexError) as exc:
            unavailable.append(f"cpu:{type(exc).__name__}")

        try:
            sample.update(self._load())
        except (OSError, ValueError, IndexError) as exc:
            unavailable.append(f"load:{type(exc).__name__}")

        try:
            sched = self._sched_counters()
            previous = self._previous_sched
            if previous is None:
                sample.update(
                    {
                        "process_runtime_delta_ms": None,
                        "process_wait_delta_ms": None,
                        "process_timeslices_delta": None,
                    }
                )
            else:
                sample.update(
                    {
                        "process_runtime_delta_ms": max(
                            0.0, (sched.runtime_ns - previous.runtime_ns) / 1_000_000.0
                        ),
                        "process_wait_delta_ms": max(
                            0.0, (sched.wait_ns - previous.wait_ns) / 1_000_000.0
                        ),
                        "process_timeslices_delta": max(
                            0, sched.timeslices - previous.timeslices
                        ),
                    }
                )
            self._previous_sched = sched
        except (OSError, ValueError, IndexError) as exc:
            unavailable.append(f"sched:{type(exc).__name__}")

        try:
            cgroup = _key_values(self._cgroup_root / "cpu.stat")
            previous = self._previous_cgroup
            if previous is None:
                sample.update(
                    {
                        "cgroup_nr_throttled_delta": None,
                        "cgroup_throttled_delta_ms": None,
                    }
                )
            else:
                sample["cgroup_nr_throttled_delta"] = max(
                    0,
                    cgroup.get("nr_throttled", 0) - previous.get("nr_throttled", 0),
                )
                sample["cgroup_throttled_delta_ms"] = max(
                    0.0,
                    (
                        cgroup.get("throttled_usec", 0)
                        - previous.get("throttled_usec", 0)
                    )
                    / 1_000.0,
                )
            self._previous_cgroup = cgroup
        except (OSError, ValueError) as exc:
            unavailable.append(f"cgroup:{type(exc).__name__}")

        for name in ("cpu", "io"):
            try:
                pressure = _pressure_avg10(self._proc_root / "pressure" / name)
                sample[f"{name}_pressure_some_avg10"] = pressure.get("some")
                if name == "io":
                    sample["io_pressure_full_avg10"] = pressure.get("full")
            except (OSError, ValueError) as exc:
                unavailable.append(f"{name}_pressure:{type(exc).__name__}")

        sample["unavailable"] = unavailable
        return sample


_DISTRIBUTION_FIELDS = (
    "scheduled_lateness_ms",
    "sample_interval_ms",
    "cpu_percent",
    "iowait_percent",
    "runnable_tasks",
    "process_runtime_delta_ms",
    "process_wait_delta_ms",
    "process_timeslices_delta",
    "cgroup_nr_throttled_delta",
    "cgroup_throttled_delta_ms",
    "cpu_pressure_some_avg10",
    "io_pressure_some_avg10",
    "io_pressure_full_avg10",
)


def summarize_host_samples(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    metrics = {}
    for field in _DISTRIBUTION_FIELDS:
        values = [
            float(sample[field])
            for sample in samples
            if isinstance(sample.get(field), (int, float))
        ]
        metrics[field] = {
            "count": len(values),
            "p95": percentile(values, 95),
            "max": max(values, default=None),
        }
    return {
        "count": len(samples),
        "unavailable": sorted(
            {item for sample in samples for item in sample.get("unavailable", [])}
        ),
        "metrics": metrics,
    }
