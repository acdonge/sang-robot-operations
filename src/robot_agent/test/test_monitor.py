from datetime import datetime, timezone
from pathlib import Path

import pytest

from robot_agent.local_health import (
    build_local_health_snapshot,
    parse_local_health_snapshot,
)
from robot_agent.manifest import load_manifest
from robot_agent.monitor import RobotMonitor


MANIFEST_PATH = Path(__file__).parents[3] / "manifests" / "bi_so101_01.yaml"


def test_runtime_kind_is_only_added_for_the_v3_wire_contract():
    monitor = RobotMonitor(load_manifest(MANIFEST_PATH))

    assert "runtime_kind" not in monitor.robot_snapshot(now_monotonic=1.0)
    assert monitor.robot_snapshot(
        now_monotonic=1.0,
        include_runtime_kind=True,
    )["runtime_kind"] == "physical"


def test_optional_action_does_not_degrade_idle_robot():
    monitor = RobotMonitor(load_manifest(MANIFEST_PATH))
    for source in monitor.manifest.sources:
        if not source.required:
            continue
        for topic in source.topics:
            monitor.mark(source.key, topic, received_monotonic=9.97)
            monitor.mark(source.key, topic, received_monotonic=10.0)

    snapshot = monitor.robot_snapshot(now_monotonic=10.5)

    assert snapshot["sources_fresh"] is True
    assert snapshot["fsm_state"] == "READY"
    assert snapshot["fault"] is None
    assert len(snapshot["sources"]) == 7
    state = next(
        source
        for source in snapshot["sources"]
        if source["key"] == "observation.state"
    )
    assert state["kind"] == "state"
    assert state["required"] is True
    assert state["fresh"] is True
    assert state["rate_ok"] is True
    assert [topic["seq"] for topic in state["topics"]] == [2, 2]
    assert [topic["age_ms"] for topic in state["topics"]] == [500.0, 500.0]
    action = next(
        source for source in snapshot["sources"] if source["key"] == "action"
    )
    assert action["required"] is False
    assert action["fresh"] is False
    assert snapshot["rates_ok"] is True


def test_required_source_age_degrades_robot():
    monitor = RobotMonitor(load_manifest(MANIFEST_PATH))
    for source in monitor.manifest.sources:
        if source.required:
            for topic in source.topics:
                monitor.mark(source.key, topic, received_monotonic=10.0)

    snapshot = monitor.robot_snapshot(now_monotonic=11.1)

    assert snapshot["sources_fresh"] is False
    assert snapshot["fsm_state"] == "DEGRADED"
    assert "observation.state" in snapshot["fault"]
    assert snapshot["recording"]["active"] is False
    assert all(
        not topic["fresh"]
        for source in snapshot["sources"]
        if source["required"]
        for topic in source["topics"]
    )


def test_topic_rate_uses_recent_message_window():
    monitor = RobotMonitor(load_manifest(MANIFEST_PATH))
    topic = monitor.manifest.sources[0].topics[0]
    for received_at in (10.0, 10.05, 10.1):
        monitor.mark(
            monitor.manifest.sources[0].key,
            topic,
            received_monotonic=received_at,
        )

    snapshot = monitor.robot_snapshot(now_monotonic=10.1)
    measured = snapshot["sources"][0]["topics"][0]

    assert measured["rate_hz"] == pytest.approx(20.0)
    assert measured["minimum_rate_hz"] == 18.0
    assert measured["rate_ok"] is True

    expired = monitor.robot_snapshot(now_monotonic=15.2)
    assert expired["sources"][0]["topics"][0]["rate_hz"] is None


def test_rate_precision_is_preserved_across_the_local_health_boundary():
    manifest = load_manifest(MANIFEST_PATH)
    monitor = RobotMonitor(manifest)
    source = manifest.sources[0]
    interval_s = 1.0 / 17.9996
    monitor.mark(source.key, source.topics[0], received_monotonic=10.0)
    monitor.mark(
        source.key,
        source.topics[0],
        received_monotonic=10.0 + interval_s,
    )

    robot = monitor.robot_snapshot(now_monotonic=10.0 + interval_s)
    topic = robot["sources"][0]["topics"][0]
    assert topic["rate_hz"] < topic["minimum_rate_hz"]
    assert topic["rate_ok"] is False

    wire = build_local_health_snapshot(
        agent_id="workcell-soarm-01",
        boot_id="12345678-1234-5678-9234-567812345678",
        sequence=1,
        snapshot_time_utc=datetime.now(timezone.utc).isoformat(),
        manifest=manifest,
        robot=robot,
    )
    parsed = parse_local_health_snapshot(
        wire,
        expected_agent_id="workcell-soarm-01",
        manifest=manifest,
    )
    assert parsed.health.sources[source.key].topics[source.topics[0]].rate_ok is False


def test_required_low_rate_degrades_robot_without_marking_topic_stale():
    monitor = RobotMonitor(load_manifest(MANIFEST_PATH))
    for source in monitor.manifest.sources:
        if not source.required:
            continue
        for topic in source.topics:
            monitor.mark(source.key, topic, received_monotonic=10.0)
            monitor.mark(source.key, topic, received_monotonic=10.03)

    top = next(source for source in monitor.manifest.sources if source.key == "top")
    monitor.mark(top.key, top.topics[0], received_monotonic=10.2)
    snapshot = monitor.robot_snapshot(now_monotonic=10.2)
    top_snapshot = next(
        source for source in snapshot["sources"] if source["key"] == "top"
    )

    assert top_snapshot["fresh"] is True
    assert top_snapshot["rate_ok"] is False
    assert snapshot["sources_fresh"] is True
    assert snapshot["rates_ok"] is False
    assert snapshot["fsm_state"] == "DEGRADED"
    assert "low-rate required sources: top" in snapshot["fault"]


def test_invalid_sample_counts_for_rate_but_breaks_freshness_until_recovery():
    monitor = RobotMonitor(load_manifest(MANIFEST_PATH))
    for source in monitor.manifest.sources:
        if not source.required:
            continue
        for topic in source.topics:
            monitor.mark(source.key, topic, received_monotonic=10.0)
            monitor.mark(source.key, topic, received_monotonic=10.03)

    state = next(
        source
        for source in monitor.manifest.sources
        if source.key == "observation.state"
    )
    topic = state.topics[0]
    monitor.mark(state.key, topic, valid=False, received_monotonic=10.04)

    invalid = monitor.robot_snapshot(now_monotonic=10.04)
    measured = invalid["sources"][0]["topics"][0]
    assert measured["seq"] == 3
    assert measured["rate_ok"] is True
    assert measured["fresh"] is False
    assert invalid["sources_fresh"] is False

    monitor.mark(state.key, topic, valid=True, received_monotonic=10.05)
    recovered = monitor.robot_snapshot(now_monotonic=10.05)
    assert recovered["sources"][0]["topics"][0]["fresh"] is True
    assert recovered["sources_fresh"] is True
