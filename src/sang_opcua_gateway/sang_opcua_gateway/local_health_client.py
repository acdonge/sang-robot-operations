"""Loopback-only client for the Agent's authoritative health snapshot."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3
from urllib.parse import quote
from urllib.request import urlopen

from robot_agent.local_health import (
    LocalHealthError,
    LocalHealthSnapshot,
    parse_local_health_snapshot,
)
from robot_agent.manifest import RobotManifest


MAX_SNAPSHOT_BYTES = 256 * 1024


class LocalHealthReplayError(LocalHealthError):
    """An authenticated snapshot violates the persisted replay window."""


class LocalHealthReplayStore:
    """Persist the accepted Agent boot and sequence across Gateway restarts."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if self.path.exists() and self.path.is_symlink():
            raise LocalHealthReplayError("local health replay database cannot be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_health_replay_state (
                    agent_id TEXT NOT NULL,
                    robot_id TEXT NOT NULL,
                    boot_id TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    max_sequence INTEGER NOT NULL,
                    max_snapshot_time TEXT NOT NULL,
                    wire_digest TEXT NOT NULL,
                    PRIMARY KEY (agent_id, robot_id)
                );
                CREATE TABLE IF NOT EXISTS local_health_boot_history (
                    agent_id TEXT NOT NULL,
                    robot_id TEXT NOT NULL,
                    boot_id TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    last_sequence INTEGER NOT NULL,
                    last_snapshot_time TEXT NOT NULL,
                    wire_digest TEXT NOT NULL,
                    PRIMARY KEY (agent_id, robot_id, boot_id)
                );
                """
            )
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _time(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def accept(self, snapshot: LocalHealthSnapshot) -> bool:
        if not snapshot.authenticated or snapshot.key_id is None:
            raise LocalHealthReplayError(
                "persisted replay defense requires authenticated local health"
            )
        snapshot_time = snapshot.snapshot_time.isoformat()
        identity = (snapshot.agent_id, snapshot.robot_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                """
                SELECT * FROM local_health_replay_state
                WHERE agent_id = ? AND robot_id = ?
                """,
                identity,
            ).fetchone()
            history = connection.execute(
                """
                SELECT * FROM local_health_boot_history
                WHERE agent_id = ? AND robot_id = ? AND boot_id = ?
                """,
                (*identity, snapshot.boot_id),
            ).fetchone()
            if state is None:
                if history is not None:
                    raise LocalHealthReplayError(
                        "local health snapshot uses a retired boot_id"
                    )
                connection.execute(
                    """
                    INSERT INTO local_health_replay_state (
                        agent_id, robot_id, boot_id, key_id, max_sequence,
                        max_snapshot_time, wire_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        *identity,
                        snapshot.boot_id,
                        snapshot.key_id,
                        snapshot.sequence,
                        snapshot_time,
                        snapshot.wire_digest,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO local_health_boot_history (
                        agent_id, robot_id, boot_id, key_id, last_sequence,
                        last_snapshot_time, wire_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        *identity,
                        snapshot.boot_id,
                        snapshot.key_id,
                        snapshot.sequence,
                        snapshot_time,
                        snapshot.wire_digest,
                    ),
                )
                return True

            if state["boot_id"] == snapshot.boot_id:
                if state["key_id"] != snapshot.key_id:
                    raise LocalHealthReplayError(
                        "local health key changed within one Agent boot"
                    )
                if snapshot.sequence < state["max_sequence"]:
                    raise LocalHealthReplayError(
                        "local health sequence moved backwards"
                    )
                if snapshot.sequence == state["max_sequence"]:
                    if snapshot.wire_digest != state["wire_digest"]:
                        raise LocalHealthReplayError(
                            "local health sequence digest changed"
                        )
                    return False
                if snapshot.snapshot_time <= self._time(state["max_snapshot_time"]):
                    raise LocalHealthReplayError(
                        "local health timestamp did not advance"
                    )
                connection.execute(
                    """
                    UPDATE local_health_replay_state
                    SET max_sequence = ?, max_snapshot_time = ?, wire_digest = ?
                    WHERE agent_id = ? AND robot_id = ?
                    """,
                    (
                        snapshot.sequence,
                        snapshot_time,
                        snapshot.wire_digest,
                        *identity,
                    ),
                )
                connection.execute(
                    """
                    UPDATE local_health_boot_history
                    SET last_sequence = ?, last_snapshot_time = ?, wire_digest = ?
                    WHERE agent_id = ? AND robot_id = ? AND boot_id = ?
                    """,
                    (
                        snapshot.sequence,
                        snapshot_time,
                        snapshot.wire_digest,
                        *identity,
                        snapshot.boot_id,
                    ),
                )
                return True

            if history is not None:
                raise LocalHealthReplayError(
                    "local health snapshot attempted to return to a retired boot_id"
                )
            if snapshot.snapshot_time <= self._time(state["max_snapshot_time"]):
                raise LocalHealthReplayError(
                    "new local health boot must advance snapshot time"
                )
            connection.execute(
                """
                INSERT INTO local_health_boot_history (
                    agent_id, robot_id, boot_id, key_id, last_sequence,
                    last_snapshot_time, wire_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *identity,
                    snapshot.boot_id,
                    snapshot.key_id,
                    snapshot.sequence,
                    snapshot_time,
                    snapshot.wire_digest,
                ),
            )
            connection.execute(
                """
                UPDATE local_health_replay_state
                SET boot_id = ?, key_id = ?, max_sequence = ?,
                    max_snapshot_time = ?, wire_digest = ?
                WHERE agent_id = ? AND robot_id = ?
                """,
                (
                    snapshot.boot_id,
                    snapshot.key_id,
                    snapshot.sequence,
                    snapshot_time,
                    snapshot.wire_digest,
                    *identity,
                ),
            )
            return True


def snapshot_url(base_url: str, robot_id: str) -> str:
    return (
        f"{base_url.rstrip('/')}/health/v1/robots/"
        f"{quote(robot_id, safe='')}/latest"
    )


def fetch_local_health(
    url: str,
    *,
    timeout_s: float,
    agent_id: str,
    manifest: RobotManifest,
    expected_mapping_digest: str | None = None,
    verification_key: bytes | None = None,
    expected_key_id: str | None = None,
    require_authenticated: bool = False,
) -> LocalHealthSnapshot:
    with urlopen(url, timeout=timeout_s) as response:
        content_length = response.headers.get("Content-Length")
        if content_length is not None and int(content_length) > MAX_SNAPSHOT_BYTES:
            raise LocalHealthError("local health snapshot exceeds 256 KiB")
        payload = response.read(MAX_SNAPSHOT_BYTES + 1)
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise LocalHealthError("local health snapshot exceeds 256 KiB")
    return parse_local_health_snapshot(
        payload,
        expected_agent_id=agent_id,
        manifest=manifest,
        expected_mapping_digest=expected_mapping_digest,
        verification_key=verification_key,
        expected_key_id=expected_key_id,
        require_authenticated=require_authenticated,
    )
