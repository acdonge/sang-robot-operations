"""Pure fleet status derivation from centrally received heartbeat snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class FleetTTLs:
    online_s: float = 12.0
    offline_s: float = 30.0

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.online_s)
            or not math.isfinite(self.offline_s)
            or self.online_s <= 0.0
            or self.offline_s <= self.online_s
        ):
            raise ValueError("TTLs require 0 < online_s < offline_s")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_z(value: datetime) -> str:
    value = _aware_utc(value)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return _aware_utc(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _aware_utc(parsed)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(timezone.utc)


def derive_agent_status(
    received_at: str | datetime,
    now: datetime,
    ttls: FleetTTLs,
) -> tuple[str, float]:
    received = parse_datetime(received_at)
    current = _aware_utc(now)
    age_s = max(0.0, (current - received).total_seconds())
    if age_s <= ttls.online_s:
        return "online", age_s
    if age_s <= ttls.offline_s:
        return "stale", age_s
    return "offline", age_s


def present_agent(
    snapshot: Mapping[str, Any],
    *,
    now: datetime,
    ttls: FleetTTLs,
) -> dict[str, Any]:
    received = parse_datetime(snapshot["received_at"])
    sent = parse_datetime(snapshot["sent_at"])
    status, age_s = derive_agent_status(received, now, ttls)
    return {
        **snapshot,
        "status": status,
        "age_s": round(age_s, 3),
        "clock_skew_s": round((received - sent).total_seconds(), 3),
    }


def summarize_agents(
    agents: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    agent_list = list(agents)
    agent_counts = {
        "online": 0,
        "stale": 0,
        "offline": 0,
        "needs_attention": 0,
        "total": len(agent_list),
    }
    robot_counts = {
        "total": 0,
        "recording": 0,
        "faulted": 0,
        "not_fresh": 0,
        "low_rate": 0,
        "unreachable": 0,
    }
    disk_values = []
    dataset_count = 0
    dataset_episodes = 0
    recording_counts = {
        "enabled_agents": 0,
        "complete": 0,
        "failed": 0,
        "partial": 0,
        "invalid": 0,
        "total": 0,
        "total_size_bytes": 0,
        "truncated_agents": 0,
    }

    for agent in agent_list:
        status = str(agent["status"])
        agent_counts[status] += 1
        robots = list(agent.get("robots", []))
        if status != "online" or any(
            robot.get("fault")
            or robot.get("fsm_state") in {"FAULT", "ABORTING"}
            or not robot.get("sources_fresh", False)
            or not robot.get("rates_ok", True)
            for robot in robots
        ) or (
            agent.get("recordings", {}).get("enabled")
            and agent.get("recordings", {}).get("state") == "degraded"
        ):
            agent_counts["needs_attention"] += 1
        robot_counts["total"] += len(robots)
        if status == "offline":
            robot_counts["unreachable"] += len(robots)
        for robot in robots:
            if status == "online" and robot.get("recording", {}).get("active"):
                robot_counts["recording"] += 1
            if robot.get("fault") or robot.get("fsm_state") in {"FAULT", "ABORTING"}:
                robot_counts["faulted"] += 1
            if not robot.get("sources_fresh", False):
                robot_counts["not_fresh"] += 1
            if not robot.get("rates_ok", True):
                robot_counts["low_rate"] += 1

        if status == "online":
            disk_free = agent.get("storage", {}).get("disk_free_gb")
            if isinstance(disk_free, (int, float)) and math.isfinite(disk_free):
                disk_values.append(float(disk_free))
        datasets = agent.get("datasets", {})
        dataset_count += int(datasets.get("count", 0))
        dataset_episodes += int(datasets.get("total_episodes", 0))
        recordings = agent.get("recordings", {})
        if recordings.get("enabled"):
            recording_counts["enabled_agents"] += 1
        for source_key, summary_key in (
            ("complete_count", "complete"),
            ("failed_count", "failed"),
            ("partial_count", "partial"),
            ("invalid_count", "invalid"),
        ):
            value = int(recordings.get(source_key, 0))
            recording_counts[summary_key] += value
            recording_counts["total"] += value
        recording_counts["total_size_bytes"] += int(
            recordings.get("total_size_bytes", 0)
        )
        if recordings.get("truncated"):
            recording_counts["truncated_agents"] += 1

    return {
        "agents": agent_counts,
        "robots": robot_counts,
        "storage": {
            "disk_free_gb_min": None if not disk_values else round(min(disk_values), 3)
        },
        "datasets": {
            "total_count": dataset_count,
            "total_episodes": dataset_episodes,
        },
        "recordings": recording_counts,
        "sync": {"supported": False},
        "generated_at": isoformat_z(now),
    }
