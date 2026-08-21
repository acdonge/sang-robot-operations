"""Manifest-backed robot snapshots built from the shared health tracker."""

from __future__ import annotations

from robot_agent.health import (
    HealthSourceConfig,
    HealthTopicConfig,
    HealthTracker,
)
from robot_agent.manifest import RobotManifest


class RobotMonitor:
    def __init__(self, manifest: RobotManifest) -> None:
        self.manifest = manifest
        self._sources = {source.key: source for source in manifest.sources}
        self._tracker = HealthTracker(
            HealthSourceConfig(
                key=source.key,
                required=source.required,
                topics=tuple(
                    HealthTopicConfig(
                        topic=topic,
                        freshness_ms=source.freshness_ms,
                        minimum_rate_hz=source.minimum_rate_hz,
                    )
                    for topic in source.topics
                ),
            )
            for source in manifest.sources
        )

    def mark(
        self,
        source_key: str,
        topic: str,
        *,
        valid: bool = True,
        received_monotonic: float | None = None,
        rate_timestamp_s: float | None = None,
    ) -> None:
        source = self._sources.get(source_key)
        if source is None or topic not in source.topics:
            raise KeyError(f"unknown topic for source {source_key}: {topic}")
        self._tracker.record(
            topic,
            valid=valid,
            received_monotonic=received_monotonic,
            rate_timestamp_s=rate_timestamp_s,
        )

    def robot_snapshot(
        self,
        *,
        now_monotonic: float | None = None,
        include_runtime_kind: bool = False,
    ) -> dict:
        health = self._tracker.snapshot(now_monotonic=now_monotonic)
        source_snapshots = []
        for source in self.manifest.sources:
            source_health = health.sources[source.key]
            source_snapshots.append(
                {
                    "key": source.key,
                    "kind": source.kind,
                    "required": source.required,
                    "freshness_ms": source.freshness_ms,
                    "fresh": source_health.fresh,
                    "rate_ok": source_health.rate_ok,
                    "topics": [
                        {
                            "topic": topic,
                            "age_ms": (
                                None
                                if topic_health.age_ms is None
                                else round(topic_health.age_ms, 3)
                            ),
                            "rate_hz": (
                                None
                                if topic_health.rate_hz is None
                                else topic_health.rate_hz
                            ),
                            "minimum_rate_hz": topic_health.minimum_rate_hz,
                            "rate_ok": topic_health.rate_ok,
                            "seq": topic_health.sequence,
                            "fresh": topic_health.fresh,
                        }
                        for topic, topic_health in source_health.topics.items()
                    ],
                }
            )

        fault_reasons = []
        if health.stale_required:
            fault_reasons.append(
                "stale required sources: " + ", ".join(health.stale_required)
            )
        if health.rate_not_ok_required:
            fault_reasons.append(
                "low-rate required sources: "
                + ", ".join(health.rate_not_ok_required)
            )
        snapshot = {
            "robot_id": self.manifest.robot.robot_id,
            "model": self.manifest.robot.model,
            "fsm_state": (
                "READY" if health.sources_fresh and health.rates_ok else "DEGRADED"
            ),
            "phase": "MONITORING",
            "sources_fresh": health.sources_fresh,
            "rates_ok": health.rates_ok,
            "sources": source_snapshots,
            "fault": None if not fault_reasons else "; ".join(fault_reasons),
            "recording": {
                "active": False,
                "episode_index": 0,
                "episodes_total": 0,
                "frames": 0,
                "elapsed_s": 0.0,
                "current_instruction": None,
            },
        }
        if include_runtime_kind:
            snapshot["runtime_kind"] = self.manifest.robot.runtime_kind
        return snapshot
