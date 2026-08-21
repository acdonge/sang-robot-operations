from datetime import datetime, timedelta, timezone

from fleet_control_plane.models import AgentHeartbeatV1
from fleet_control_plane.store import FleetStore


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def heartbeat(agent_id: str, *, name: str = "Agent", datasets: int = 1):
    return AgentHeartbeatV1.model_validate(
        {
            "schema_version": 1,
            "agent_id": agent_id,
            "agent_name": name,
            "sw_version": "0.1.0",
            "sent_at": NOW.isoformat(),
            "storage": {
                "disk_free_gb": 100.0,
                "ram_free_gb": 20.0,
                "cpu_pct": 15.0,
            },
            "datasets": {
                "count": datasets,
                "total_episodes": datasets * 2,
                "total_size_mb": datasets * 10.0,
            },
            "sync": {"supported": False, "state": "unsupported", "pending": None},
            "robots": [],
        }
    )


def test_store_upserts_latest_snapshot_and_keeps_agents_independent(tmp_path):
    path = tmp_path / "fleet.db"
    store = FleetStore(path)
    store.upsert(heartbeat("agent-a", name="Before"), received_at=NOW)
    store.upsert(heartbeat("agent-b", datasets=3), received_at=NOW)
    store.upsert(
        heartbeat("agent-a", name="After", datasets=2),
        received_at=NOW + timedelta(seconds=5),
    )

    snapshots = store.list_snapshots()
    assert [item["agent_id"] for item in snapshots] == ["agent-a", "agent-b"]
    agent_a = store.get_snapshot("agent-a")
    assert agent_a is not None
    assert agent_a["agent_name"] == "After"
    assert agent_a["datasets"]["count"] == 2
    assert agent_a["received_at"].endswith("Z")


def test_store_survives_reopen_without_persisting_derived_status(tmp_path):
    path = tmp_path / "fleet.db"
    FleetStore(path).upsert(heartbeat("agent-a"), received_at=NOW)

    reopened = FleetStore(path)
    snapshot = reopened.get_snapshot("agent-a")

    assert snapshot is not None
    assert snapshot["agent_id"] == "agent-a"
    assert "status" not in snapshot
    assert snapshot["received_at"] == "2026-07-21T12:00:00.000Z"
    assert reopened.list_events()[0]["event_type"] == "agent_online"


def test_unknown_agent_returns_none(tmp_path):
    assert FleetStore(tmp_path / "fleet.db").get_snapshot("missing") is None
