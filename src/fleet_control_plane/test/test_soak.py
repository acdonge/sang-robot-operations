from datetime import datetime, timezone

import pytest

from fleet_control_plane.soak import (
    health_issues,
    reconnect_failures,
    require_loopback_http,
    run_soak,
)


TOPIC = "/robot/camera/top/compressed"


def agent_payload(*, fresh=True, rate_ok=True, status="online"):
    return [
        {
            "agent_id": "agent-1",
            "status": status,
            "storage": {
                "cpu_pct": 12.5,
                "ram_free_gb": 8.0,
                "disk_free_gb": 100.0,
            },
            "robots": [
                {
                    "robot_id": "robot-1",
                    "fault": None,
                    "sources": [
                        {
                            "key": "top",
                            "required": True,
                            "fresh": fresh,
                            "rate_ok": rate_ok,
                            "topics": [
                                {
                                    "topic": TOPIC,
                                    "fresh": fresh,
                                    "age_ms": 25.0,
                                    "rate_hz": 29.5,
                                    "minimum_rate_hz": 25.0,
                                    "rate_ok": rate_ok,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ]


class Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def test_strict_soak_passes_healthy_samples_and_collects_topic_stats():
    clock = Clock()

    def fetch(path):
        return [] if path.startswith("/api/v1/events") else agent_payload()

    report = run_soak(
        fetch=fetch,
        agent_id="agent-1",
        robot_id="robot-1",
        duration_s=2.0,
        interval_s=1.0,
        startup_timeout_s=2.0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        utcnow=lambda: datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert report["passed"] is True
    assert report["sample_count"] == 3
    assert report["topic_stats"][TOPIC]["min_observed_rate_hz"] == 29.5
    assert report["topic_stats"][TOPIC]["median_observed_rate_hz"] == 29.5
    assert report["system_stats"]["cpu_pct"]["p95"] == 12.5
    assert report["system_stats"]["ram_free_mib"]["minimum"] == 8192.0
    assert report["dds_wire_traffic_measured"] is False


def test_startup_waits_for_agent_registration():
    clock = Clock()
    agent_reads = 0

    def fetch(path):
        nonlocal agent_reads
        if path.startswith("/api/v1/events"):
            return []
        agent_reads += 1
        return [] if agent_reads == 1 else agent_payload()

    report = run_soak(
        fetch=fetch,
        agent_id="agent-1",
        robot_id="robot-1",
        duration_s=1.0,
        interval_s=1.0,
        startup_timeout_s=3.0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert report["passed"] is True
    assert agent_reads >= 3


def test_strict_soak_fails_a_low_rate_sample():
    clock = Clock()
    samples = [agent_payload(), agent_payload(rate_ok=False), agent_payload()]

    def fetch(path):
        if path.startswith("/api/v1/events"):
            return []
        return samples.pop(0) if len(samples) > 1 else samples[0]

    report = run_soak(
        fetch=fetch,
        agent_id="agent-1",
        robot_id="robot-1",
        duration_s=1.0,
        interval_s=1.0,
        startup_timeout_s=2.0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert report["passed"] is False
    assert report["unhealthy_sample_count"] == 1
    assert "below minimum rate" in report["observed_issues"][0]


def test_reconnect_mode_requires_ordered_new_events_and_final_health():
    events = [
        {"id": 8, "event_type": "topic_recovered", "topic": TOPIC},
        {"id": 7, "event_type": "topic_stale", "topic": TOPIC},
        {"id": 5, "event_type": "topic_stale", "topic": TOPIC},
    ]

    assert reconnect_failures(
        events,
        baseline_event_id=5,
        expected_topics=(TOPIC,),
    ) == []
    assert reconnect_failures(
        events[:1],
        baseline_event_id=5,
        expected_topics=(TOPIC,),
    ) == [f"missing stale -> recovered transition for {TOPIC}"]


def test_health_checks_and_remote_transport_guard():
    agent, robot = agent_payload(status="offline", fresh=False)[0], agent_payload(
        status="offline", fresh=False
    )[0]["robots"][0]
    issues = health_issues(agent, robot)

    assert "agent status is offline" in issues
    assert "required source top is stale" in issues
    assert require_loopback_http("http://127.0.0.1:8090/") == "http://127.0.0.1:8090"
    with pytest.raises(ValueError, match="authentication and TLS"):
        require_loopback_http("http://192.168.1.10:8090")
