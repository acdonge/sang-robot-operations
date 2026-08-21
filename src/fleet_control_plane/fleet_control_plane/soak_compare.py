"""Compare three gateway-OFF and three gateway-ON soak reports."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
from typing import Any, Iterable


class ComparisonError(ValueError):
    """Raised when reports cannot form a trustworthy Phase 1 comparison."""


def _finite(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ComparisonError(f"{label} is missing or non-finite")
    return float(value)


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ComparisonError(f"failed to read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ComparisonError(f"{path} must contain a JSON object")
    return value


def _median_metric(reports: Iterable[dict[str, Any]], *path: str) -> float:
    values = []
    for index, report in enumerate(reports):
        current: Any = report
        for component in path:
            if not isinstance(current, dict):
                raise ComparisonError(f"report {index} is missing {'.'.join(path)}")
            current = current.get(component)
        values.append(_finite(current, f"report {index} {'.'.join(path)}"))
    return float(statistics.median(values))


def compare_report_sets(
    baseline_reports: list[dict[str, Any]],
    gateway_reports: list[dict[str, Any]],
    *,
    cpu_p95_max_increase_percentage_points: float = 10.0,
    ram_minimum_max_decrease_mib: float = 512.0,
    source_rate_max_regression_percent: float = 5.0,
) -> dict[str, Any]:
    if len(baseline_reports) != 3 or len(gateway_reports) != 3:
        raise ComparisonError("Phase 1 requires exactly three OFF and three ON reports")
    reports = baseline_reports + gateway_reports
    if any(report.get("passed") is not True for report in reports):
        raise ComparisonError("all input soak reports must pass")
    identities = {(report.get("agent_id"), report.get("robot_id")) for report in reports}
    if len(identities) != 1:
        raise ComparisonError("all reports must target the same Agent and robot")

    baseline_cpu = _median_metric(baseline_reports, "system_stats", "cpu_pct", "p95")
    gateway_cpu = _median_metric(gateway_reports, "system_stats", "cpu_pct", "p95")
    baseline_ram = _median_metric(
        baseline_reports, "system_stats", "ram_free_mib", "minimum"
    )
    gateway_ram = _median_metric(
        gateway_reports, "system_stats", "ram_free_mib", "minimum"
    )
    cpu_increase = gateway_cpu - baseline_cpu
    ram_decrease = baseline_ram - gateway_ram
    failures = []
    if cpu_increase > cpu_p95_max_increase_percentage_points:
        failures.append(
            f"CPU p95 median increased by {cpu_increase:.3f} percentage points"
        )
    if ram_decrease > ram_minimum_max_decrease_mib:
        failures.append(f"minimum free RAM median decreased by {ram_decrease:.3f} MiB")

    baseline_topics = set(baseline_reports[0].get("topic_stats", {}))
    gateway_topics = set(gateway_reports[0].get("topic_stats", {}))
    if baseline_topics != gateway_topics:
        raise ComparisonError("OFF and ON report topic sets differ")
    topic_deltas = {}
    for topic in sorted(baseline_topics):
        if not baseline_reports[0]["topic_stats"][topic].get("required"):
            continue
        baseline_rate = _median_metric(
            baseline_reports,
            "topic_stats",
            topic,
            "median_observed_rate_hz",
        )
        gateway_rate = _median_metric(
            gateway_reports,
            "topic_stats",
            topic,
            "median_observed_rate_hz",
        )
        if baseline_rate <= 0.0:
            raise ComparisonError(f"baseline topic rate is not positive for {topic}")
        regression = max(0.0, (baseline_rate - gateway_rate) / baseline_rate * 100.0)
        topic_deltas[topic] = {
            "baseline_median_hz": round(baseline_rate, 3),
            "gateway_median_hz": round(gateway_rate, 3),
            "regression_percent": round(regression, 3),
        }
        if regression > source_rate_max_regression_percent:
            failures.append(f"source rate regressed by {regression:.3f}% for {topic}")

    agent_id, robot_id = next(iter(identities))
    return {
        "schema_version": 1,
        "mode": "opcua_gateway_off_on_comparison",
        "passed": not failures,
        "agent_id": agent_id,
        "robot_id": robot_id,
        "runs_per_condition": 3,
        "budgets": {
            "cpu_p95_max_increase_percentage_points": cpu_p95_max_increase_percentage_points,
            "ram_minimum_max_decrease_mib": ram_minimum_max_decrease_mib,
            "source_rate_max_regression_percent": source_rate_max_regression_percent,
        },
        "cpu_pct_p95_median": {
            "baseline": round(baseline_cpu, 3),
            "gateway": round(gateway_cpu, 3),
            "increase_percentage_points": round(cpu_increase, 3),
        },
        "minimum_free_ram_mib_median": {
            "baseline": round(baseline_ram, 3),
            "gateway": round(gateway_ram, 3),
            "decrease_mib": round(ram_decrease, 3),
        },
        "topic_rate_deltas": topic_deltas,
        "dds_wire_traffic_measured": False,
        "failures": failures,
    }


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare OPC UA gateway OFF/ON soak reports")
    parser.add_argument("--baseline", nargs=3, required=True, type=Path)
    parser.add_argument("--gateway", nargs=3, required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    try:
        result = compare_report_sets(
            [_load(path) for path in options.baseline],
            [_load(path) for path in options.gateway],
        )
    except ComparisonError as exc:
        result = {
            "schema_version": 1,
            "mode": "opcua_gateway_off_on_comparison",
            "passed": False,
            "failures": [str(exc)],
        }
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    options.report.parent.mkdir(parents=True, exist_ok=True)
    options.report.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
