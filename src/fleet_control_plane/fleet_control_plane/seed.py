"""Seed deterministic multi-PC snapshots for the loopback Admin demo."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fleet_control_plane.models import AgentHeartbeatV1
from fleet_control_plane.store import FleetStore


def _source(
    key: str,
    kind: str,
    topics: list[str],
    *,
    required: bool,
    fresh: bool,
    age_ms: float | None,
    seq: int,
    rate_hz: float | None = None,
    minimum_rate_hz: float | None = None,
) -> dict:
    rate_ok = None
    if rate_hz is not None and minimum_rate_hz is not None:
        rate_ok = rate_hz >= minimum_rate_hz
    return {
        "key": key,
        "kind": kind,
        "required": required,
        "freshness_ms": 1000,
        "fresh": fresh,
        "rate_ok": rate_ok,
        "topics": [
            {
                "topic": topic,
                "age_ms": age_ms,
                "seq": seq,
                "fresh": fresh,
                "rate_hz": rate_hz,
                "minimum_rate_hz": minimum_rate_hz,
                "rate_ok": rate_ok,
            }
            for topic in topics
        ],
    }


def _heartbeat(
    agent_id: str,
    agent_name: str,
    *,
    sent_at: datetime,
    disk_free_gb: float,
    datasets: int,
    episodes: int,
    robot: dict,
) -> AgentHeartbeatV1:
    return AgentHeartbeatV1.model_validate(
        {
            "schema_version": 1,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "sw_version": "0.1.0-demo",
            "sent_at": sent_at.isoformat(),
            "storage": {
                "disk_free_gb": disk_free_gb,
                "ram_free_gb": 24.6,
                "cpu_pct": 31.0,
            },
            "datasets": {
                "count": datasets,
                "total_episodes": episodes,
                "total_size_mb": datasets * 4096.0,
            },
            "sync": {"supported": False, "state": "unsupported", "pending": None},
            "robots": [robot],
        }
    )


def seed_store(store: FleetStore, *, now: datetime) -> None:
    cases = [
        (
            1.0,
            _heartbeat(
                "workcell-soarm-01",
                "SO-ARM Workcell",
                sent_at=now - timedelta(seconds=1.2),
                disk_free_gb=812.4,
                datasets=12,
                episodes=240,
                robot={
                    "robot_id": "bi_so101_01",
                    "model": "bi_so101",
                    "fsm_state": "RECORDING",
                    "phase": "RECORDING",
                    "sources_fresh": True,
                    "sources": [
                        _source(
                            "observation.state",
                            "state",
                            [
                                "/bi_so101/follower_left/joint_states_norm",
                                "/bi_so101/follower_right/joint_states_norm",
                            ],
                            required=True,
                            fresh=True,
                            age_ms=32.0,
                            seq=1842,
                            rate_hz=20.0,
                            minimum_rate_hz=18.0,
                        ),
                        _source(
                            "action",
                            "action",
                            [
                                "/bi_so101/leader_left/joint_trajectory_norm",
                                "/bi_so101/leader_right/joint_trajectory_norm",
                            ],
                            required=False,
                            fresh=False,
                            age_ms=None,
                            seq=0,
                        ),
                        *[
                            _source(
                                name,
                                "camera",
                                [
                                    f"/bi_so101/camera/{name}/color/"
                                    "image_rect_raw/compressed"
                                ],
                                required=True,
                                fresh=True,
                                age_ms=48.0,
                                seq=5530,
                                rate_hz=29.5,
                                minimum_rate_hz=25.0,
                            )
                            for name in (
                                "top",
                                "left_side",
                                "right_side",
                                "left_wrist",
                                "right_wrist",
                            )
                        ],
                    ],
                    "fault": None,
                    "recording": {
                        "active": True,
                        "episode_index": 2,
                        "episodes_total": 10,
                        "frames": 431,
                        "elapsed_s": 14.37,
                        "current_instruction": "Pick and hand over the red block",
                    },
                },
            ),
        ),
        (
            18.0,
            _heartbeat(
                "workcell-omy-01",
                "OMY Assembly Cell",
                sent_at=now - timedelta(seconds=18.4),
                disk_free_gb=241.8,
                datasets=7,
                episodes=96,
                robot={
                    "robot_id": "omy_01",
                    "model": "omy_f3m",
                    "fsm_state": "FAULT",
                    "phase": "FAULT",
                    "sources_fresh": False,
                    "sources": [
                        _source(
                            "observation.state",
                            "state",
                            ["/joint_states"],
                            required=True,
                            fresh=True,
                            age_ms=41.0,
                            seq=910,
                            rate_hz=30.0,
                        ),
                        _source(
                            "action",
                            "action",
                            ["/leader/joint_trajectory"],
                            required=False,
                            fresh=False,
                            age_ms=None,
                            seq=0,
                        ),
                        _source(
                            "cam_top",
                            "camera",
                            ["/camera/cam_top/color/image_raw/compressed"],
                            required=True,
                            fresh=True,
                            age_ms=46.0,
                            seq=842,
                        ),
                        _source(
                            "cam_wrist",
                            "camera",
                            ["/camera/cam_wrist/color/image_rect_raw/compressed"],
                            required=True,
                            fresh=False,
                            age_ms=3500.0,
                            seq=831,
                        )
                    ],
                    "fault": "wrist camera stale",
                    "recording": {
                        "active": False,
                        "episode_index": 0,
                        "episodes_total": 0,
                        "frames": 0,
                        "elapsed_s": 0.0,
                        "current_instruction": None,
                    },
                },
            ),
        ),
        (
            45.0,
            _heartbeat(
                "workcell-rb5-01",
                "RB5 Welding Cell",
                sent_at=now - timedelta(seconds=45.8),
                disk_free_gb=118.2,
                datasets=3,
                episodes=24,
                robot={
                    "robot_id": "rb5_850e_01",
                    "model": "rb5_850e",
                    "fsm_state": "DISABLED",
                    "phase": "DISABLED",
                    "sources_fresh": True,
                    "sources": [
                        _source(
                            "observation.state",
                            "state",
                            ["/rbpodo/joint_states"],
                            required=True,
                            fresh=True,
                            age_ms=63.0,
                            seq=420,
                            rate_hz=29.8,
                            minimum_rate_hz=20.0,
                        )
                    ],
                    "fault": None,
                    "recording": {
                        "active": False,
                        "episode_index": 0,
                        "episodes_total": 0,
                        "frames": 0,
                        "elapsed_s": 0.0,
                        "current_instruction": None,
                    },
                },
            ),
        ),
    ]
    for age_s, heartbeat in cases:
        store.upsert(heartbeat, received_at=now - timedelta(seconds=age_s))


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the loopback Admin demo")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path.home() / ".local" / "share" / "sang-fleet" / "fleet.db",
    )
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    now = datetime.now(timezone.utc)
    seed_store(FleetStore(options.db_path), now=now)
    print(f"Seeded 3 Agent snapshots in {options.db_path}")


if __name__ == "__main__":
    main()
