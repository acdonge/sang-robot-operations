from datetime import datetime, timedelta, timezone

from fleet_control_plane.status import (
    FleetTTLs,
    derive_agent_status,
    present_agent,
    summarize_agents,
)


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
TTLS = FleetTTLs(online_s=12.0, offline_s=30.0)


def agent_snapshot(
    *,
    received_age_s: float,
    sent_offset_s: float = 0.0,
    fsm_state: str = "READY",
    sources_fresh: bool = True,
    fault: str | None = None,
) -> dict:
    received = NOW - timedelta(seconds=received_age_s)
    sent = received + timedelta(seconds=sent_offset_s)
    return {
        "schema_version": 1,
        "agent_id": f"agent-{received_age_s}",
        "agent_name": "Agent",
        "sw_version": "0.1.0",
        "sent_at": sent.isoformat(),
        "received_at": received.isoformat(),
        "storage": {"disk_free_gb": 50.0, "ram_free_gb": 10.0, "cpu_pct": 20.0},
        "datasets": {"count": 2, "total_episodes": 8, "total_size_mb": 100.0},
        "recordings": {
            "enabled": True,
            "state": "ready",
            "error_code": None,
            "complete_count": 1,
            "failed_count": 1,
            "partial_count": 0,
            "invalid_count": 0,
            "scanned_count": 2,
            "total_size_bytes": 1024,
            "truncated": False,
            "recent": [],
        },
        "sync": {"supported": False, "state": "unsupported", "pending": None},
        "robots": [
            {
                "robot_id": "robot-1",
                "model": "test_robot",
                "fsm_state": fsm_state,
                "phase": fsm_state,
                "sources_fresh": sources_fresh,
                "fault": fault,
                "recording": {
                    "active": fsm_state == "RECORDING",
                    "episode_index": 1,
                    "episodes_total": 5,
                    "frames": 120,
                    "elapsed_s": 4.0,
                    "current_instruction": "test",
                },
            }
        ],
    }


def test_status_boundaries_use_central_received_time():
    assert derive_agent_status(NOW - timedelta(seconds=12), NOW, TTLS)[0] == "online"
    assert derive_agent_status(NOW - timedelta(seconds=12.001), NOW, TTLS)[0] == "stale"
    assert derive_agent_status(NOW - timedelta(seconds=30), NOW, TTLS)[0] == "stale"
    assert derive_agent_status(NOW - timedelta(seconds=30.001), NOW, TTLS)[0] == "offline"


def test_agent_clock_skew_is_diagnostic_only():
    future_clock = present_agent(
        agent_snapshot(received_age_s=5, sent_offset_s=3600),
        now=NOW,
        ttls=TTLS,
    )
    assert future_clock["status"] == "online"
    assert future_clock["age_s"] == 5.0
    assert future_clock["clock_skew_s"] == -3600.0


def test_summary_keeps_agent_liveness_and_robot_health_separate():
    online_recording = present_agent(
        agent_snapshot(received_age_s=1, fsm_state="RECORDING"), now=NOW, ttls=TTLS
    )
    stale_fault = present_agent(
        agent_snapshot(received_age_s=20, fault="camera lost", sources_fresh=False),
        now=NOW,
        ttls=TTLS,
    )
    offline_recording = present_agent(
        agent_snapshot(received_age_s=40, fsm_state="RECORDING"), now=NOW, ttls=TTLS
    )

    summary = summarize_agents(
        [online_recording, stale_fault, offline_recording], now=NOW
    )

    assert summary["agents"] == {
        "online": 1,
        "stale": 1,
        "offline": 1,
        "needs_attention": 2,
        "total": 3,
    }
    assert summary["robots"] == {
        "total": 3,
        "recording": 1,
        "faulted": 1,
        "not_fresh": 1,
        "low_rate": 0,
        "unreachable": 1,
    }
    assert summary["storage"]["disk_free_gb_min"] == 50.0
    assert summary["datasets"] == {"total_count": 6, "total_episodes": 24}
    assert summary["recordings"] == {
        "enabled_agents": 3,
        "complete": 3,
        "failed": 3,
        "partial": 0,
        "invalid": 0,
        "total": 6,
        "total_size_bytes": 3072,
        "truncated_agents": 0,
    }
    assert summary["sync"] == {"supported": False}


def test_degraded_recording_inventory_needs_attention():
    agent = present_agent(agent_snapshot(received_age_s=1), now=NOW, ttls=TTLS)
    agent["recordings"].update(
        {
            "state": "degraded",
            "error_code": "capture_root_unavailable",
        }
    )

    summary = summarize_agents([agent], now=NOW)

    assert summary["agents"]["needs_attention"] == 1
