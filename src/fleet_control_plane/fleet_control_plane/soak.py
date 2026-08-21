"""Finite read-only soak and reconnect verification against Central HTTP APIs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import time
from typing import Any, Callable
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class SoakError(RuntimeError):
    """Raised when the verifier cannot establish its starting conditions."""


def require_loopback_http(url: str) -> str:
    value = url.rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError(
            "Control Plane must use loopback HTTP until Agent authentication and TLS exist"
        )
    return value


def fetch_json(url: str, *, timeout_s: float) -> Any:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlopen(request, timeout=timeout_s) as response:
        if response.status != 200:
            raise RuntimeError(f"GET {url} returned HTTP {response.status}")
        return json.loads(response.read())


def find_target(
    agents: list[dict[str, Any]],
    *,
    agent_id: str,
    robot_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    agent = next(
        (item for item in agents if item.get("agent_id") == agent_id),
        None,
    )
    if agent is None:
        raise SoakError(f"Agent {agent_id!r} is not registered")
    robot = next(
        (item for item in agent.get("robots", []) if item.get("robot_id") == robot_id),
        None,
    )
    if robot is None:
        raise SoakError(f"Robot {robot_id!r} is not attached to Agent {agent_id!r}")
    return agent, robot


def health_issues(agent: dict[str, Any], robot: dict[str, Any]) -> list[str]:
    issues = []
    if agent.get("status") != "online":
        issues.append(f"agent status is {agent.get('status', 'unknown')}")
    if robot.get("fault"):
        issues.append(str(robot["fault"]))
    for source in robot.get("sources", []):
        if not source.get("required"):
            continue
        if not source.get("fresh", False):
            issues.append(f"required source {source['key']} is stale")
        if source.get("rate_ok") is False:
            issues.append(f"required source {source['key']} is below minimum rate")
    return issues


def required_rates_measured(robot: dict[str, Any]) -> bool:
    for source in robot.get("sources", []):
        if not source.get("required"):
            continue
        for topic in source.get("topics", []):
            if (
                topic.get("minimum_rate_hz") is not None
                and topic.get("rate_hz") is None
            ):
                return False
    return True


def _topic_stats(
    stats: dict[str, dict[str, Any]],
    robot: dict[str, Any],
) -> None:
    for source in robot.get("sources", []):
        for topic in source.get("topics", []):
            name = topic["topic"]
            item = stats.setdefault(
                name,
                {
                    "source_key": source["key"],
                    "required": source["required"],
                    "minimum_rate_hz": topic.get("minimum_rate_hz"),
                    "rate_samples": 0,
                    "rate_values_hz": [],
                    "min_observed_rate_hz": None,
                    "max_observed_age_ms": None,
                    "stale_samples": 0,
                    "low_rate_samples": 0,
                },
            )
            rate_hz = topic.get("rate_hz")
            if isinstance(rate_hz, (int, float)) and math.isfinite(rate_hz):
                item["rate_samples"] += 1
                item["rate_values_hz"].append(float(rate_hz))
                previous_rate = item["min_observed_rate_hz"]
                item["min_observed_rate_hz"] = round(
                    float(rate_hz)
                    if previous_rate is None
                    else min(previous_rate, float(rate_hz)),
                    3,
                )
            age_ms = topic.get("age_ms")
            if isinstance(age_ms, (int, float)) and math.isfinite(age_ms):
                previous_age = item["max_observed_age_ms"]
                item["max_observed_age_ms"] = round(
                    float(age_ms)
                    if previous_age is None
                    else max(previous_age, float(age_ms)),
                    3,
                )
            if not topic.get("fresh", False):
                item["stale_samples"] += 1
            if topic.get("rate_ok") is False:
                item["low_rate_samples"] += 1


def _system_metrics(
    samples: dict[str, list[float]],
    agent: dict[str, Any],
) -> None:
    storage = agent.get("storage", {})
    for key in ("cpu_pct", "ram_free_gb"):
        value = storage.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value):
            samples[key].append(float(value))


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _finalize_topic_stats(stats: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    finalized = {}
    for topic, item in stats.items():
        values = item["rate_values_hz"]
        finalized[topic] = {
            key: value
            for key, value in item.items()
            if key != "rate_values_hz"
        }
        finalized[topic]["median_observed_rate_hz"] = (
            None if not values else round(float(statistics.median(values)), 3)
        )
    return finalized


def _finalize_system_metrics(samples: dict[str, list[float]]) -> dict[str, Any]:
    cpu = samples["cpu_pct"]
    ram_mib = [value * 1024.0 for value in samples["ram_free_gb"]]
    return {
        "cpu_pct": {
            "sample_count": len(cpu),
            "median": None if not cpu else round(float(statistics.median(cpu)), 3),
            "p95": (
                None
                if not cpu
                else round(float(_nearest_rank(cpu, 0.95)), 3)
            ),
        },
        "ram_free_mib": {
            "sample_count": len(ram_mib),
            "minimum": None if not ram_mib else round(min(ram_mib), 3),
            "median": (
                None
                if not ram_mib
                else round(float(statistics.median(ram_mib)), 3)
            ),
        },
    }


def reconnect_failures(
    events: list[dict[str, Any]],
    *,
    baseline_event_id: int,
    expected_topics: tuple[str, ...],
) -> list[str]:
    new_events = sorted(
        (event for event in events if event["id"] > baseline_event_id),
        key=lambda event: event["id"],
    )
    failures = []
    for topic in expected_topics:
        transitions = [
            event["event_type"]
            for event in new_events
            if event.get("topic") == topic
            and event["event_type"] in {"topic_stale", "topic_recovered"}
        ]
        try:
            stale_index = transitions.index("topic_stale")
            recovered_index = transitions.index("topic_recovered", stale_index + 1)
        except ValueError:
            failures.append(f"missing stale -> recovered transition for {topic}")
            continue
        if recovered_index <= stale_index:
            failures.append(f"invalid reconnect event order for {topic}")
    return failures


def run_soak(
    *,
    fetch: Callable[[str], Any],
    agent_id: str,
    robot_id: str,
    duration_s: float,
    interval_s: float,
    startup_timeout_s: float,
    expected_topics: tuple[str, ...] = (),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    startup_started = monotonic()
    startup_error: str | None = None
    while True:
        try:
            agent, robot = find_target(
                fetch("/api/v1/agents"),
                agent_id=agent_id,
                robot_id=robot_id,
            )
            startup_error = None
            if not health_issues(agent, robot) and required_rates_measured(robot):
                break
        except (OSError, RuntimeError, ValueError) as exc:
            startup_error = f"{type(exc).__name__}: {exc}"
        if monotonic() - startup_started >= startup_timeout_s:
            detail = "" if startup_error is None else f"; last error: {startup_error}"
            raise SoakError(
                "target did not become healthy with measured rates before timeout"
                + detail
            )
        remaining = startup_timeout_s - (monotonic() - startup_started)
        sleep(min(interval_s, remaining))

    query = urlencode({"agent_id": agent_id, "limit": 500})
    try:
        initial_events = fetch(f"/api/v1/events?{query}")
    except (OSError, RuntimeError, ValueError) as exc:
        raise SoakError(f"failed to read baseline events: {exc}") from exc
    baseline_event_id = max(
        (event["id"] for event in initial_events),
        default=0,
    )
    started_at = utcnow()
    started = monotonic()
    deadline = started + duration_s
    sample_count = 0
    unhealthy_sample_count = 0
    fetch_errors = []
    observed_issues: set[str] = set()
    stats: dict[str, dict[str, Any]] = {}
    system_samples: dict[str, list[float]] = {"cpu_pct": [], "ram_free_gb": []}
    final_healthy = False

    while True:
        try:
            agent, robot = find_target(
                fetch("/api/v1/agents"),
                agent_id=agent_id,
                robot_id=robot_id,
            )
            issues = health_issues(agent, robot)
            sample_count += 1
            _topic_stats(stats, robot)
            _system_metrics(system_samples, agent)
            if issues:
                unhealthy_sample_count += 1
                observed_issues.update(issues)
            final_healthy = not issues and required_rates_measured(robot)
        except (OSError, RuntimeError, ValueError) as exc:
            fetch_errors.append(f"{type(exc).__name__}: {exc}")
            final_healthy = False
        now = monotonic()
        if now >= deadline:
            break
        sleep(min(interval_s, deadline - now))

    failures = []
    try:
        events = fetch(f"/api/v1/events?{query}")
    except (OSError, RuntimeError, ValueError) as exc:
        events = []
        failures.append(f"failed to read final events: {exc}")
    if fetch_errors:
        failures.append(f"{len(fetch_errors)} Central fetch error(s)")
    if not expected_topics and unhealthy_sample_count:
        failures.append(f"{unhealthy_sample_count} unhealthy sample(s) during strict soak")
    if expected_topics:
        failures.extend(
            reconnect_failures(
                events,
                baseline_event_id=baseline_event_id,
                expected_topics=expected_topics,
            )
        )
    if not final_healthy:
        failures.append("target was not healthy at the final sample")
    for topic, item in stats.items():
        if item["required"] and item["minimum_rate_hz"] is not None:
            if item["rate_samples"] == 0:
                failures.append(f"required topic {topic} had no measured rate")

    finished_at = utcnow()
    return {
        "schema_version": 2,
        "mode": "reconnect" if expected_topics else "soak",
        "passed": not failures,
        "agent_id": agent_id,
        "robot_id": robot_id,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "requested_duration_s": duration_s,
        "elapsed_s": round(monotonic() - started, 3),
        "sample_count": sample_count,
        "unhealthy_sample_count": unhealthy_sample_count,
        "fetch_error_count": len(fetch_errors),
        "final_healthy": final_healthy,
        "expected_reconnect_topics": list(expected_topics),
        "observed_issues": sorted(observed_issues),
        "failures": failures,
        "topic_stats": _finalize_topic_stats(stats),
        "system_stats": _finalize_system_metrics(system_samples),
        "dds_wire_traffic_measured": False,
    }


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Fleet Central health without sending robot commands"
    )
    parser.add_argument(
        "--control-plane-url",
        default="http://127.0.0.1:18091",
    )
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--robot-id", required=True)
    parser.add_argument("--duration-s", type=float, default=7200.0)
    parser.add_argument("--interval-s", type=float, default=1.0)
    parser.add_argument("--startup-timeout-s", type=float, default=30.0)
    parser.add_argument("--timeout-s", type=float, default=2.0)
    parser.add_argument("--expect-reconnect-topic", action="append", default=[])
    parser.add_argument("--report", type=Path)
    options = parser.parse_args(args)
    for name in ("duration_s", "interval_s", "startup_timeout_s", "timeout_s"):
        value = getattr(options, name)
        if not math.isfinite(value) or value <= 0.0:
            parser.error(name.replace("_", "-") + " must be positive")
    try:
        options.control_plane_url = require_loopback_http(options.control_plane_url)
    except ValueError as exc:
        parser.error(str(exc))
    return options


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)

    def fetch(path: str) -> Any:
        return fetch_json(
            options.control_plane_url + path,
            timeout_s=options.timeout_s,
        )

    try:
        report = run_soak(
            fetch=fetch,
            agent_id=options.agent_id,
            robot_id=options.robot_id,
            duration_s=options.duration_s,
            interval_s=options.interval_s,
            startup_timeout_s=options.startup_timeout_s,
            expected_topics=tuple(options.expect_reconnect_topic),
        )
    except SoakError as exc:
        report = {
            "schema_version": 2,
            "mode": (
                "reconnect" if options.expect_reconnect_topic else "soak"
            ),
            "passed": False,
            "agent_id": options.agent_id,
            "robot_id": options.robot_id,
            "failures": [str(exc)],
        }
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if options.report is not None:
        options.report.parent.mkdir(parents=True, exist_ok=True)
        options.report.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
