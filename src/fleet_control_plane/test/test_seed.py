from datetime import datetime, timezone

from fleet_control_plane.seed import seed_store
from fleet_control_plane.store import FleetStore


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def source_by_key(snapshot, key):
    return next(
        source
        for source in snapshot["robots"][0]["sources"]
        if source["key"] == key
    )


def test_seed_topics_match_supported_robot_manifests(tmp_path):
    store = FleetStore(tmp_path / "fleet.db")
    seed_store(store, now=NOW)
    snapshots = {
        snapshot["agent_id"]: snapshot for snapshot in store.list_snapshots()
    }

    assert set(snapshots) == {
        "workcell-soarm-01",
        "workcell-omy-01",
        "workcell-rb5-01",
    }

    omy = snapshots["workcell-omy-01"]
    assert source_by_key(omy, "observation.state")["topics"][0]["topic"] == (
        "/joint_states"
    )
    assert source_by_key(omy, "action")["topics"][0]["topic"] == (
        "/leader/joint_trajectory"
    )
    assert source_by_key(omy, "cam_top")["topics"][0]["topic"] == (
        "/camera/cam_top/color/image_raw/compressed"
    )
    assert source_by_key(omy, "cam_wrist")["topics"][0]["topic"] == (
        "/camera/cam_wrist/color/image_rect_raw/compressed"
    )

    rb5_state = source_by_key(snapshots["workcell-rb5-01"], "observation.state")
    assert rb5_state["topics"][0]["topic"] == "/rbpodo/joint_states"
    assert rb5_state["topics"][0]["minimum_rate_hz"] == 20.0
    assert rb5_state["rate_ok"] is True
