"""Pure metric helpers for multi-sensor timestamp validation."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import math
from typing import Iterable, Sequence


NANOSECONDS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True)
class Sample:
    stream: str
    header_ns: int
    arrival_wall_ns: int
    arrival_monotonic_ns: int
    moving: bool = False


def percentile(values: Sequence[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction)


def _distribution_ms(values_ns: Iterable[int]) -> dict:
    values_ms = [value / 1_000_000.0 for value in values_ns]
    return {
        "count": len(values_ms),
        "p50_ms": percentile(values_ms, 50),
        "p95_ms": percentile(values_ms, 95),
        "max_ms": max(values_ms, default=None),
    }


def stream_metrics(samples: Sequence[Sample], *, max_gap_ms: float) -> dict:
    arrival_gaps = [
        current.arrival_monotonic_ns - previous.arrival_monotonic_ns
        for previous, current in zip(samples, samples[1:])
    ]
    header_gaps = [
        current.header_ns - previous.header_ns
        for previous, current in zip(samples, samples[1:])
        if current.header_ns > 0 and previous.header_ns > 0
    ]
    elapsed_ns = (
        samples[-1].arrival_monotonic_ns - samples[0].arrival_monotonic_ns
        if len(samples) > 1
        else 0
    )
    rate_hz = (
        (len(samples) - 1) * NANOSECONDS_PER_SECOND / elapsed_ns
        if elapsed_ns > 0
        else 0.0
    )
    latency_ns = [
        sample.arrival_wall_ns - sample.header_ns
        for sample in samples
        if sample.header_ns > 0
    ]
    return {
        "count": len(samples),
        "rate_hz": rate_hz,
        "arrival_gap": _distribution_ms(arrival_gaps),
        "header_gap": _distribution_ms([gap for gap in header_gaps if gap >= 0]),
        "gaps_over_limit": sum(
            gap > max_gap_ms * 1_000_000 for gap in arrival_gaps
        ),
        "timestamp_regressions": sum(gap < 0 for gap in header_gaps),
        "zero_header_timestamps": sum(sample.header_ns <= 0 for sample in samples),
        "capture_to_arrival": _distribution_ms(latency_ns),
    }


def over_limit_gap_events(
    samples: Sequence[Sample],
    *,
    max_gap_ms: float,
    max_events: int,
) -> dict:
    """Return a bounded list of timestamped arrival gaps over the limit."""
    threshold_ns = max_gap_ms * 1_000_000
    events = []
    total = 0
    for previous, current in zip(samples, samples[1:]):
        gap_ns = current.arrival_monotonic_ns - previous.arrival_monotonic_ns
        if gap_ns <= threshold_ns:
            continue
        total += 1
        if len(events) < max_events:
            events.append(
                {
                    "previous_arrival_wall_ns": previous.arrival_wall_ns,
                    "arrival_wall_ns": current.arrival_wall_ns,
                    "previous_header_ns": previous.header_ns,
                    "header_ns": current.header_ns,
                    "gap_ms": gap_ns / 1_000_000.0,
                }
            )
    return {
        "total": total,
        "truncated": total > len(events),
        "events": events,
    }


def nearest_skew_metrics(
    joint_samples: Sequence[Sample],
    camera_samples: Sequence[Sample],
    *,
    moving_only: bool = False,
) -> dict:
    camera_stamps = sorted(
        sample.header_ns for sample in camera_samples if sample.header_ns > 0
    )
    if not camera_stamps:
        return {**_distribution_ms([]), "eligible": 0, "excluded_outside_window": 0}

    eligible = [
        sample
        for sample in joint_samples
        if sample.header_ns > 0 and (sample.moving or not moving_only)
    ]
    skews = []
    excluded = 0
    for sample in eligible:
        if sample.header_ns < camera_stamps[0] or sample.header_ns > camera_stamps[-1]:
            excluded += 1
            continue
        index = bisect_left(camera_stamps, sample.header_ns)
        candidates = []
        if index < len(camera_stamps):
            candidates.append(camera_stamps[index])
        if index > 0:
            candidates.append(camera_stamps[index - 1])
        skews.append(min(abs(stamp - sample.header_ns) for stamp in candidates))
    return {
        **_distribution_ms(skews),
        "eligible": len(eligible),
        "excluded_outside_window": excluded,
    }


def evaluate_summary(
    *,
    streams: dict[str, dict],
    sync: dict[str, dict],
    joint_stream: str,
    camera_streams: Sequence[str],
    minimum_joint_rate_hz: float,
    minimum_camera_rate_hz: float,
    max_gap_ms: float,
    max_p95_skew_ms: float,
    max_skew_ms: float,
    clock_step_count: int,
    minimum_moving_samples: int,
) -> list[dict]:
    checks = []

    def add(name: str, passed: bool, actual, limit) -> None:
        checks.append(
            {"name": name, "passed": bool(passed), "actual": actual, "limit": limit}
        )

    for name, metrics in streams.items():
        minimum_rate = (
            minimum_joint_rate_hz if name == joint_stream else minimum_camera_rate_hz
        )
        add(f"{name}.rate", metrics["rate_hz"] >= minimum_rate, metrics["rate_hz"], minimum_rate)
        max_gap = metrics["arrival_gap"]["max_ms"]
        add(f"{name}.max_gap", max_gap is not None and max_gap <= max_gap_ms, max_gap, max_gap_ms)
        add(f"{name}.timestamp_regressions", metrics["timestamp_regressions"] == 0, metrics["timestamp_regressions"], 0)
        add(f"{name}.zero_header_timestamps", metrics["zero_header_timestamps"] == 0, metrics["zero_header_timestamps"], 0)

    for camera in camera_streams:
        all_metrics = sync[camera]["all"]
        add(f"{camera}.p95_skew", all_metrics["p95_ms"] is not None and all_metrics["p95_ms"] <= max_p95_skew_ms, all_metrics["p95_ms"], max_p95_skew_ms)
        add(f"{camera}.max_skew", all_metrics["max_ms"] is not None and all_metrics["max_ms"] <= max_skew_ms, all_metrics["max_ms"], max_skew_ms)
        if minimum_moving_samples > 0:
            moving_metrics = sync[camera]["moving"]
            add(f"{camera}.moving_samples", moving_metrics["count"] >= minimum_moving_samples, moving_metrics["count"], minimum_moving_samples)
            add(f"{camera}.moving_p95_skew", moving_metrics["p95_ms"] is not None and moving_metrics["p95_ms"] <= max_p95_skew_ms, moving_metrics["p95_ms"], max_p95_skew_ms)

    add("system_clock_steps", clock_step_count == 0, clock_step_count, 0)
    return checks
