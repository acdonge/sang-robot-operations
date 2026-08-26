"""SQLite latest snapshots and transition-only health event history."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any

from fleet_control_plane.models import AgentHeartbeatV1
from fleet_control_plane.operations_models import (
    AgentCredentialCreate,
    AgentRegistrationCreate,
    AgentRegistrationUpdate,
    AssignmentCreate,
    AssetCreate,
    AssetReplace,
    AssetRuntimeKindUpdate,
    ConnectionProfileCreate,
    ConnectionProfileReplace,
    CredentialRevoke,
    DecommissionRequest,
    ProcessingNodeCreate,
    ProcessingNodeReplace,
    TaskDefinitionCreate,
    TaskRunCreate,
    TaskRunReplace,
    TaskRunTransition,
    WorkcellCreate,
    WorkcellReplace,
)
from fleet_control_plane.capture_jobs import CAPTURE_JOB_MIGRATION
from fleet_control_plane.status import isoformat_z, parse_datetime
from fleet_control_plane.recording_storage import (
    RECORDING_MIGRATION,
    RECORDING_QUALITY_MIGRATION,
)


MAX_HEALTH_EVENTS = 10_000
CURRENT_SCHEMA_VERSION = 10


PHASE3_MIGRATION = (
    """
    CREATE TABLE IF NOT EXISTS agent_registrations (
        agent_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        identity_uri TEXT NOT NULL UNIQUE,
        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_credentials (
        credential_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL REFERENCES agent_registrations(agent_id)
            ON DELETE RESTRICT,
        certificate_sha256 TEXT NOT NULL UNIQUE CHECK (
            length(certificate_sha256) = 64
            AND certificate_sha256 = lower(certificate_sha256)
        ),
        certificate_ref TEXT NOT NULL,
        not_before TEXT NOT NULL,
        not_after TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
        created_at TEXT NOT NULL,
        revoked_at TEXT,
        revocation_reason TEXT,
        CHECK (
            (status = 'active' AND revoked_at IS NULL AND revocation_reason IS NULL)
            OR
            (status = 'revoked' AND revoked_at IS NOT NULL AND revocation_reason IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS agent_credentials_agent_status
        ON agent_credentials (agent_id, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS connection_profiles (
        profile_id TEXT PRIMARY KEY,
        owner_kind TEXT NOT NULL CHECK (owner_kind IN ('agent', 'asset')),
        agent_id TEXT REFERENCES agent_registrations(agent_id) ON DELETE RESTRICT,
        asset_id TEXT REFERENCES assets(asset_id) ON DELETE RESTRICT,
        protocol TEXT NOT NULL CHECK (protocol IN ('opcua', 'https_agent')),
        desired_endpoint TEXT NOT NULL,
        expected_application_uri TEXT,
        security_mode TEXT NOT NULL CHECK (
            security_mode IN ('SignAndEncrypt', 'MutualTLS')
        ),
        security_policy TEXT NOT NULL CHECK (
            security_policy IN ('Basic256Sha256', 'TLS1.2+')
        ),
        client_certificate_ref TEXT NOT NULL,
        client_private_key_ref TEXT NOT NULL,
        trust_list_ref TEXT NOT NULL,
        crl_ref TEXT NOT NULL,
        server_certificate_ref TEXT,
        user_certificate_ref TEXT,
        user_private_key_ref TEXT,
        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (
            (owner_kind = 'agent' AND agent_id IS NOT NULL AND asset_id IS NULL)
            OR
            (owner_kind = 'asset' AND asset_id IS NOT NULL AND agent_id IS NULL)
        ),
        CHECK (
            (protocol = 'opcua' AND security_mode = 'SignAndEncrypt'
                AND security_policy = 'Basic256Sha256'
                AND expected_application_uri IS NOT NULL
                AND server_certificate_ref IS NOT NULL
                AND user_certificate_ref IS NOT NULL
                AND user_private_key_ref IS NOT NULL)
            OR
            (protocol = 'https_agent' AND security_mode = 'MutualTLS'
                AND security_policy = 'TLS1.2+'
                AND server_certificate_ref IS NULL
                AND user_certificate_ref IS NULL
                AND user_private_key_ref IS NULL)
        )
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS connection_profiles_owner_protocol
        ON connection_profiles (
            owner_kind,
            COALESCE(agent_id, ''),
            COALESCE(asset_id, ''),
            protocol
        )
    """,
    """
    CREATE TABLE IF NOT EXISTS connection_observations (
        profile_id TEXT PRIMARY KEY REFERENCES connection_profiles(profile_id)
            ON DELETE CASCADE,
        observed_endpoint TEXT,
        observed_application_uri TEXT,
        peer_certificate_sha256 TEXT,
        peer_address TEXT,
        namespace_uris_json TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        last_success_at TEXT,
        last_error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_replay_state (
        agent_id TEXT PRIMARY KEY REFERENCES agent_registrations(agent_id)
            ON DELETE RESTRICT,
        boot_id TEXT NOT NULL,
        max_sequence INTEGER NOT NULL CHECK (max_sequence >= 1),
        max_sent_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_boot_history (
        agent_id TEXT NOT NULL REFERENCES agent_registrations(agent_id)
            ON DELETE RESTRICT,
        boot_id TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_sequence INTEGER NOT NULL CHECK (last_sequence >= 1),
        last_sent_at TEXT NOT NULL,
        PRIMARY KEY (agent_id, boot_id)
    )
    """,
)


class ConfigurationConflict(ValueError):
    """The requested configuration conflicts with persisted state."""


class ConfigurationNotFound(ValueError):
    """A referenced configuration or observed entity does not exist."""


class ConfigurationInvalid(ValueError):
    """The requested configuration violates an operations rule."""


class AgentAuthenticationError(ValueError):
    """An Agent certificate is not enrolled for the claimed identity."""


class AgentReplayError(ValueError):
    """A secure heartbeat is stale, duplicated or from a retired boot."""


def _topic_states(payload: dict[str, Any]) -> dict[tuple[str, str, str], dict]:
    states = {}
    for robot in payload.get("robots", []):
        robot_id = robot["robot_id"]
        for source in robot.get("sources", []):
            for topic in source.get("topics", []):
                states[(robot_id, source["key"], topic["topic"])] = {
                    **topic,
                    "required": source["required"],
                }
    return states


class FleetStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    agent_name TEXT NOT NULL,
                    sw_version TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_status (
                    agent_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS health_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    robot_id TEXT,
                    source_key TEXT,
                    topic TEXT,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS health_events_agent_id_id
                    ON health_events (agent_id, id DESC);
                CREATE INDEX IF NOT EXISTS health_events_type_id
                    ON health_events (event_type, id DESC);

                CREATE TABLE IF NOT EXISTS processing_nodes (
                    node_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS node_agents (
                    node_id TEXT NOT NULL REFERENCES processing_nodes(node_id)
                        ON DELETE RESTRICT,
                    agent_id TEXT NOT NULL UNIQUE REFERENCES agents(agent_id)
                        ON DELETE RESTRICT,
                    PRIMARY KEY (node_id, agent_id)
                );

                CREATE TABLE IF NOT EXISTS assets (
                    asset_id TEXT PRIMARY KEY,
                    asset_type TEXT NOT NULL CHECK (
                        asset_type IN (
                            'robot', 'camera', 'eef', 'welder', 'feeder',
                            'mobile_robot', 'sensor', 'other'
                        )
                    ),
                    display_name TEXT NOT NULL,
                    node_id TEXT REFERENCES processing_nodes(node_id)
                        ON DELETE RESTRICT,
                    agent_id TEXT REFERENCES agents(agent_id)
                        ON DELETE RESTRICT,
                    robot_id TEXT,
                    source_key TEXT,
                    capabilities_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    CHECK (
                        (agent_id IS NULL AND robot_id IS NULL AND source_key IS NULL)
                        OR
                        (agent_id IS NOT NULL AND robot_id IS NOT NULL)
                    ),
                    CHECK (
                        (asset_type = 'camera' AND (
                            agent_id IS NULL OR source_key IS NOT NULL
                        ))
                        OR
                        (asset_type = 'robot' AND source_key IS NULL)
                        OR
                        (asset_type NOT IN ('camera', 'robot') AND
                            agent_id IS NULL AND robot_id IS NULL AND source_key IS NULL)
                    )
                );

                CREATE UNIQUE INDEX IF NOT EXISTS assets_observed_binding
                    ON assets (agent_id, robot_id, COALESCE(source_key, ''))
                    WHERE agent_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS workcells (
                    workcell_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    description TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workcell_assets (
                    workcell_id TEXT NOT NULL REFERENCES workcells(workcell_id)
                        ON DELETE RESTRICT,
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id)
                        ON DELETE RESTRICT,
                    PRIMARY KEY (workcell_id, asset_id)
                );

                CREATE TABLE IF NOT EXISTS task_definitions (
                    task_definition_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    display_name TEXT NOT NULL,
                    description TEXT,
                    roles_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_definition_id, version)
                );

                CREATE TABLE IF NOT EXISTS task_runs (
                    run_id TEXT PRIMARY KEY,
                    task_definition_id TEXT NOT NULL,
                    task_version INTEGER NOT NULL,
                    workcell_id TEXT REFERENCES workcells(workcell_id)
                        ON DELETE RESTRICT,
                    display_name TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('DRAFT', 'READY')),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_definition_id, task_version)
                        REFERENCES task_definitions(task_definition_id, version)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS assignments (
                    run_id TEXT NOT NULL REFERENCES task_runs(run_id)
                        ON DELETE RESTRICT,
                    role TEXT NOT NULL,
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id)
                        ON DELETE RESTRICT,
                    access_mode TEXT NOT NULL CHECK (
                        access_mode IN ('exclusive', 'shared_read')
                    ),
                    reserved INTEGER NOT NULL CHECK (reserved IN (0, 1)),
                    PRIMARY KEY (run_id, role),
                    UNIQUE (run_id, asset_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS assignments_reserved_asset
                    ON assignments (asset_id)
                    WHERE reserved = 1;

                CREATE TABLE IF NOT EXISTS configuration_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS configuration_audit_entity
                    ON configuration_audit (entity_type, entity_id, id DESC);

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY CHECK (version >= 1),
                    name TEXT NOT NULL UNIQUE,
                    applied_at TEXT NOT NULL
                );
                """
            )
            rows = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            versions = [row["version"] for row in rows]
            if versions and versions != list(range(1, max(versions) + 1)):
                raise RuntimeError("database schema migrations are not contiguous")
            if versions and versions[-1] > CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    "database schema is newer than this fleet_control_plane build"
                )
            if not versions:
                connection.execute(
                    """
                    INSERT INTO schema_migrations (version, name, applied_at)
                    VALUES (1, 'baseline_operations', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    """
                )
                connection.commit()
                versions = [1]
            if 2 not in versions:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in PHASE3_MIGRATION:
                        connection.execute(statement)
                    connection.execute(
                        """
                        INSERT INTO schema_migrations (version, name, applied_at)
                        VALUES (2, 'phase3_security_identity', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                        """
                    )
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
                versions.append(2)
            if 3 not in versions:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    asset_columns = {
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA table_info(assets)"
                        ).fetchall()
                    }
                    if "runtime_kind" not in asset_columns:
                        connection.execute(
                            """
                            ALTER TABLE assets ADD COLUMN runtime_kind TEXT
                                CHECK (runtime_kind IN ('physical', 'simulation'))
                            """
                        )
                    connection.execute(
                        """
                        INSERT INTO schema_migrations (version, name, applied_at)
                        VALUES (3, 'phase4_runtime_identity', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                        """
                    )
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
                versions.append(3)
            if 4 not in versions:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    profile_columns = {
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA table_info(connection_profiles)"
                        ).fetchall()
                    }
                    table_sql_row = connection.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'connection_profiles'"
                    ).fetchone()
                    table_sql = "" if table_sql_row is None else table_sql_row["sql"]
                    requires_rebuild = (
                        "server_certificate_ref" not in profile_columns
                        or "server_certificate_ref IS NOT NULL" not in table_sql
                        or "server_certificate_ref IS NULL" not in table_sql
                    )
                    if requires_rebuild:
                        connection.execute(
                            """
                            CREATE TABLE connection_profiles_v4 (
                                profile_id TEXT PRIMARY KEY,
                                owner_kind TEXT NOT NULL CHECK (owner_kind IN ('agent', 'asset')),
                                agent_id TEXT REFERENCES agent_registrations(agent_id)
                                    ON DELETE RESTRICT,
                                asset_id TEXT REFERENCES assets(asset_id) ON DELETE RESTRICT,
                                protocol TEXT NOT NULL CHECK (
                                    protocol IN ('opcua', 'https_agent')
                                ),
                                desired_endpoint TEXT NOT NULL,
                                expected_application_uri TEXT,
                                security_mode TEXT NOT NULL CHECK (
                                    security_mode IN ('SignAndEncrypt', 'MutualTLS')
                                ),
                                security_policy TEXT NOT NULL CHECK (
                                    security_policy IN ('Basic256Sha256', 'TLS1.2+')
                                ),
                                client_certificate_ref TEXT NOT NULL,
                                client_private_key_ref TEXT NOT NULL,
                                trust_list_ref TEXT NOT NULL,
                                crl_ref TEXT NOT NULL,
                                server_certificate_ref TEXT,
                                user_certificate_ref TEXT,
                                user_private_key_ref TEXT,
                                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                                revision INTEGER NOT NULL CHECK (revision >= 1),
                                created_at TEXT NOT NULL,
                                updated_at TEXT NOT NULL,
                                CHECK (
                                    (owner_kind = 'agent' AND agent_id IS NOT NULL
                                        AND asset_id IS NULL)
                                    OR
                                    (owner_kind = 'asset' AND asset_id IS NOT NULL
                                        AND agent_id IS NULL)
                                ),
                                CHECK (
                                    (protocol = 'opcua'
                                        AND security_mode = 'SignAndEncrypt'
                                        AND security_policy = 'Basic256Sha256'
                                        AND expected_application_uri IS NOT NULL
                                        AND server_certificate_ref IS NOT NULL
                                        AND user_certificate_ref IS NOT NULL
                                        AND user_private_key_ref IS NOT NULL)
                                    OR
                                    (protocol = 'https_agent'
                                        AND security_mode = 'MutualTLS'
                                        AND security_policy = 'TLS1.2+'
                                        AND server_certificate_ref IS NULL
                                        AND user_certificate_ref IS NULL
                                        AND user_private_key_ref IS NULL)
                                )
                            )
                            """
                        )
                        server_ref_expression = (
                            "CASE WHEN protocol = 'opcua' THEN "
                            "COALESCE(server_certificate_ref, "
                            "'secret://opcua/pins/edge-cert.pem') ELSE NULL END"
                            if "server_certificate_ref" in profile_columns
                            else "CASE WHEN protocol = 'opcua' THEN "
                            "'secret://opcua/pins/edge-cert.pem' ELSE NULL END"
                        )
                        connection.execute(
                            f"""
                            INSERT INTO connection_profiles_v4 (
                                profile_id, owner_kind, agent_id, asset_id, protocol,
                                desired_endpoint, expected_application_uri, security_mode,
                                security_policy, client_certificate_ref,
                                client_private_key_ref, trust_list_ref, crl_ref,
                                server_certificate_ref, user_certificate_ref,
                                user_private_key_ref, enabled, revision, created_at,
                                updated_at
                            )
                            SELECT
                                profile_id, owner_kind, agent_id, asset_id, protocol,
                                desired_endpoint, expected_application_uri, security_mode,
                                security_policy, client_certificate_ref,
                                client_private_key_ref, trust_list_ref, crl_ref,
                                {server_ref_expression}, user_certificate_ref,
                                user_private_key_ref, enabled, revision, created_at,
                                updated_at
                            FROM connection_profiles
                            """
                        )
                        connection.execute("DROP TABLE connection_profiles")
                        connection.execute(
                            "ALTER TABLE connection_profiles_v4 "
                            "RENAME TO connection_profiles"
                        )
                        connection.execute(
                            """
                            CREATE UNIQUE INDEX connection_profiles_owner_protocol
                                ON connection_profiles (
                                    owner_kind,
                                    COALESCE(agent_id, ''),
                                    COALESCE(asset_id, ''),
                                    protocol
                                )
                            """
                        )
                    connection.execute(
                        """
                        INSERT INTO schema_migrations (version, name, applied_at)
                        VALUES (4, 'phase4_opcua_server_pin', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                        """
                    )
                    violations = connection.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall()
                    if violations:
                        raise RuntimeError(
                            "phase4 OPC UA pin migration violates foreign keys"
                        )
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
                finally:
                    connection.execute("PRAGMA foreign_keys = ON")
                versions.append(4)
            if 5 not in versions:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for table in ("processing_nodes", "assets", "workcells"):
                        columns = {
                            row["name"]
                            for row in connection.execute(
                                f"PRAGMA table_info({table})"
                            ).fetchall()
                        }
                        additions = (
                            ("revision", "INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1)"),
                            (
                                "lifecycle_state",
                                "TEXT NOT NULL DEFAULT 'active' "
                                "CHECK (lifecycle_state IN ('active', 'decommissioned'))",
                            ),
                            ("updated_at", "TEXT NOT NULL DEFAULT ''"),
                            ("decommissioned_at", "TEXT"),
                            ("decommission_reason", "TEXT"),
                        )
                        for column, declaration in additions:
                            if column not in columns:
                                connection.execute(
                                    f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                                )
                        connection.execute(
                            f"UPDATE {table} SET updated_at = created_at "
                            "WHERE updated_at = ''"
                        )

                    run_columns = {
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA table_info(task_runs)"
                        ).fetchall()
                    }
                    run_sql_row = connection.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'task_runs'"
                    ).fetchone()
                    run_sql = "" if run_sql_row is None else run_sql_row["sql"]
                    if (
                        "'ACTIVE'" not in run_sql
                        or "revision" not in run_columns
                        or "updated_at" not in run_columns
                    ):
                        connection.execute(
                            """
                            CREATE TABLE task_runs_v5 (
                                run_id TEXT PRIMARY KEY,
                                task_definition_id TEXT NOT NULL,
                                task_version INTEGER NOT NULL,
                                workcell_id TEXT REFERENCES workcells(workcell_id)
                                    ON DELETE RESTRICT,
                                display_name TEXT NOT NULL,
                                state TEXT NOT NULL CHECK (
                                    state IN (
                                        'DRAFT', 'READY', 'ACTIVE', 'COMPLETE', 'FAILED'
                                    )
                                ),
                                revision INTEGER NOT NULL CHECK (revision >= 1),
                                created_at TEXT NOT NULL,
                                updated_at TEXT NOT NULL,
                                started_at TEXT,
                                finished_at TEXT,
                                terminal_reason TEXT,
                                FOREIGN KEY (task_definition_id, task_version)
                                    REFERENCES task_definitions(task_definition_id, version)
                                    ON DELETE RESTRICT,
                                CHECK (
                                    (state = 'ACTIVE' AND started_at IS NOT NULL
                                        AND finished_at IS NULL)
                                    OR
                                    (state IN ('COMPLETE', 'FAILED')
                                        AND started_at IS NOT NULL
                                        AND finished_at IS NOT NULL)
                                    OR
                                    (state IN ('DRAFT', 'READY')
                                        AND started_at IS NULL
                                        AND finished_at IS NULL)
                                ),
                                CHECK (
                                    (state = 'FAILED' AND terminal_reason IS NOT NULL)
                                    OR state != 'FAILED'
                                )
                            )
                            """
                        )
                        connection.execute(
                            """
                            INSERT INTO task_runs_v5 (
                                run_id, task_definition_id, task_version, workcell_id,
                                display_name, state, revision, created_at, updated_at,
                                started_at, finished_at, terminal_reason
                            )
                            SELECT
                                run_id, task_definition_id, task_version, workcell_id,
                                display_name, state, 1, created_at, created_at,
                                NULL, NULL, NULL
                            FROM task_runs
                            """
                        )
                        connection.execute("DROP TABLE task_runs")
                        connection.execute(
                            "ALTER TABLE task_runs_v5 RENAME TO task_runs"
                        )
                    connection.execute(
                        """
                        INSERT INTO schema_migrations (version, name, applied_at)
                        VALUES (
                            5,
                            'robot_camera_registry_lifecycle',
                            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        )
                        """
                    )
                    violations = connection.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall()
                    if violations:
                        raise RuntimeError(
                            "robot/camera registry lifecycle migration violates foreign keys"
                        )
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
                finally:
                    connection.execute("PRAGMA foreign_keys = ON")
                versions.append(5)
            if 6 not in versions:
                try:
                    connection.executescript(
                        """
                        BEGIN IMMEDIATE;

                        CREATE TABLE IF NOT EXISTS principals (
                            principal_id TEXT PRIMARY KEY,
                            principal_kind TEXT NOT NULL CHECK (
                                principal_kind IN ('user', 'service')
                            ),
                            display_name TEXT NOT NULL,
                            role TEXT NOT NULL CHECK (
                                role IN ('viewer', 'operator', 'admin')
                            ),
                            secret_hash TEXT,
                            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                            revision INTEGER NOT NULL CHECK (revision >= 1),
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            CHECK (
                                (principal_kind = 'user' AND secret_hash IS NOT NULL)
                                OR
                                (principal_kind = 'service' AND secret_hash IS NULL)
                            )
                        );

                        CREATE TABLE IF NOT EXISTS service_tokens (
                            token_id TEXT PRIMARY KEY,
                            principal_id TEXT NOT NULL REFERENCES principals(principal_id)
                                ON DELETE RESTRICT,
                            token_sha256 TEXT NOT NULL UNIQUE CHECK (
                                length(token_sha256) = 64
                            ),
                            expires_at TEXT,
                            created_at TEXT NOT NULL,
                            revoked_at TEXT,
                            revocation_reason TEXT,
                            CHECK (
                                (revoked_at IS NULL AND revocation_reason IS NULL)
                                OR
                                (revoked_at IS NOT NULL AND revocation_reason IS NOT NULL)
                            )
                        );

                        CREATE INDEX IF NOT EXISTS service_tokens_principal
                            ON service_tokens (principal_id, revoked_at);

                        CREATE TABLE IF NOT EXISTS browser_sessions (
                            session_id TEXT PRIMARY KEY,
                            principal_id TEXT NOT NULL REFERENCES principals(principal_id)
                                ON DELETE RESTRICT,
                            session_sha256 TEXT NOT NULL UNIQUE CHECK (
                                length(session_sha256) = 64
                            ),
                            csrf_sha256 TEXT NOT NULL CHECK (length(csrf_sha256) = 64),
                            expires_at TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            last_seen_at TEXT NOT NULL,
                            revoked_at TEXT
                        );

                        CREATE INDEX IF NOT EXISTS browser_sessions_principal
                            ON browser_sessions (principal_id, expires_at);

                        CREATE TABLE IF NOT EXISTS security_audit (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            occurred_at TEXT NOT NULL,
                            principal_id TEXT,
                            action TEXT NOT NULL,
                            target_type TEXT NOT NULL,
                            target_id TEXT,
                            outcome TEXT NOT NULL CHECK (
                                outcome IN ('success', 'denied', 'failed')
                            ),
                            details_json TEXT NOT NULL
                        );

                        CREATE INDEX IF NOT EXISTS security_audit_principal_id
                            ON security_audit (principal_id, id DESC);
                        CREATE INDEX IF NOT EXISTS security_audit_target
                            ON security_audit (target_type, target_id, id DESC);

                        INSERT INTO schema_migrations (version, name, applied_at)
                        VALUES (
                            6,
                            'human_service_identity_rbac',
                            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        );

                        COMMIT;
                        """
                    )
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
                versions.append(6)
            if 7 not in versions:
                try:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        + RECORDING_MIGRATION
                        + """
                        INSERT INTO schema_migrations (version, name, applied_at)
                        VALUES (
                            7,
                            'immutable_recording_catalog',
                            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        );
                        COMMIT;
                        """
                    )
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
                versions.append(7)
            if 8 not in versions:
                try:
                    connection.executescript(
                        """
                        BEGIN IMMEDIATE;

                        CREATE TABLE IF NOT EXISTS principal_agent_scopes (
                            principal_id TEXT NOT NULL REFERENCES principals(principal_id)
                                ON DELETE RESTRICT,
                            agent_id TEXT NOT NULL REFERENCES agent_registrations(agent_id)
                                ON DELETE RESTRICT,
                            PRIMARY KEY (principal_id, agent_id)
                        );

                        CREATE INDEX IF NOT EXISTS principal_agent_scopes_agent
                            ON principal_agent_scopes (agent_id, principal_id);

                        INSERT INTO schema_migrations (version, name, applied_at)
                        VALUES (
                            8,
                            'service_identity_agent_scope',
                            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        );

                        COMMIT;
                        """
                    )
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
            if 9 not in versions:
                try:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        + CAPTURE_JOB_MIGRATION
                        + """
                        INSERT INTO schema_migrations (version, name, applied_at)
                        VALUES (
                            9,
                            'task_run_capture_jobs',
                            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        );
                        COMMIT;
                        """
                    )
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
                versions.append(9)
            if 10 not in versions:
                try:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        + RECORDING_QUALITY_MIGRATION
                        + """
                        INSERT INTO schema_migrations (version, name, applied_at)
                        VALUES (
                            10,
                            'recording_quality_reports',
                            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        );
                        COMMIT;
                        """
                    )
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        occurred_at: datetime,
        event_type: str,
        severity: str,
        agent_id: str,
        message: str,
        robot_id: str | None = None,
        source_key: str | None = None,
        topic: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO health_events (
                occurred_at, event_type, severity, agent_id, robot_id,
                source_key, topic, message, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                isoformat_z(occurred_at),
                event_type,
                severity,
                agent_id,
                robot_id,
                source_key,
                topic,
                message,
                json.dumps(
                    details or {},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )

    @classmethod
    def _record_topic_transitions(
        cls,
        connection: sqlite3.Connection,
        *,
        previous: dict[str, Any] | None,
        current: dict[str, Any],
        occurred_at: datetime,
    ) -> int:
        previous_states = {} if previous is None else _topic_states(previous)
        current_states = _topic_states(current)
        inserted = 0
        for identity, topic_state in current_states.items():
            robot_id, source_key, topic = identity
            before = previous_states.get(identity)
            transitions = []
            if before is None:
                if not topic_state["fresh"]:
                    transitions.append(
                        ("topic_stale", "warning", "Topic became stale")
                    )
                if topic_state.get("rate_ok") is False:
                    transitions.append(
                        ("topic_rate_low", "warning", "Topic rate fell below minimum")
                    )
            else:
                if before["fresh"] and not topic_state["fresh"]:
                    transitions.append(
                        ("topic_stale", "warning", "Topic became stale")
                    )
                elif not before["fresh"] and topic_state["fresh"]:
                    transitions.append(
                        ("topic_recovered", "info", "Topic freshness recovered")
                    )
                before_rate_ok = before.get("rate_ok")
                rate_ok = topic_state.get("rate_ok")
                if before_rate_ok is not False and rate_ok is False:
                    transitions.append(
                        ("topic_rate_low", "warning", "Topic rate fell below minimum")
                    )
                elif before_rate_ok is False and rate_ok is True:
                    transitions.append(
                        (
                            "topic_rate_recovered",
                            "info",
                            "Topic rate recovered",
                        )
                    )
            for event_type, severity, message in transitions:
                cls._insert_event(
                    connection,
                    occurred_at=occurred_at,
                    event_type=event_type,
                    severity=severity,
                    agent_id=current["agent_id"],
                    robot_id=robot_id,
                    source_key=source_key,
                    topic=topic,
                    message=message,
                    details={
                        "required": topic_state["required"],
                        "age_ms": topic_state.get("age_ms"),
                        "rate_hz": topic_state.get("rate_hz"),
                        "minimum_rate_hz": topic_state.get("minimum_rate_hz"),
                    },
                )
                inserted += 1
        return inserted

    @classmethod
    def _record_runtime_kind_transitions(
        cls,
        connection: sqlite3.Connection,
        *,
        previous: dict[str, Any] | None,
        current: dict[str, Any],
        occurred_at: datetime,
    ) -> int:
        rows = connection.execute(
            """
            SELECT asset_id, robot_id, source_key, runtime_kind
            FROM assets
            WHERE agent_id = ? AND runtime_kind IS NOT NULL
            """,
            (current["agent_id"],),
        ).fetchall()
        previous_kinds = {
            robot["robot_id"]: robot.get("runtime_kind")
            for robot in (() if previous is None else previous.get("robots", []))
        }
        current_kinds = {
            robot["robot_id"]: robot.get("runtime_kind")
            for robot in current.get("robots", [])
        }
        inserted = 0
        for row in rows:
            observed = current_kinds.get(row["robot_id"])
            if observed not in {"physical", "simulation"}:
                continue
            declared = row["runtime_kind"]
            mismatch = observed != declared
            previous_observed = previous_kinds.get(row["robot_id"])
            previous_mismatch = (
                None
                if previous_observed not in {"physical", "simulation"}
                else previous_observed != declared
            )
            if mismatch and previous_mismatch is not True:
                event_type = "runtime_kind_drift"
                severity = "critical"
                message = "Observed runtime kind differs from the registered Asset"
            elif not mismatch and previous_mismatch is True:
                event_type = "runtime_kind_recovered"
                severity = "info"
                message = "Observed runtime kind matches the registered Asset again"
            else:
                continue
            cls._insert_event(
                connection,
                occurred_at=occurred_at,
                event_type=event_type,
                severity=severity,
                agent_id=current["agent_id"],
                robot_id=row["robot_id"],
                source_key=row["source_key"],
                message=message,
                details={
                    "asset_id": row["asset_id"],
                    "declared_runtime_kind": declared,
                    "observed_runtime_kind": observed,
                },
            )
            inserted += 1
        return inserted

    @staticmethod
    def _prune_events(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM health_events
            WHERE id IN (
                SELECT id FROM health_events
                ORDER BY id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (MAX_HEALTH_EVENTS,),
        )

    def _upsert_connection(
        self,
        connection: sqlite3.Connection,
        heartbeat: AgentHeartbeatV1,
        received_at: datetime,
    ) -> int:
        payload = heartbeat.model_dump(mode="json")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        previous_row = connection.execute(
            "SELECT payload_json FROM agents WHERE agent_id = ?",
            (heartbeat.agent_id,),
        ).fetchone()
        previous = (
            None
            if previous_row is None
            else json.loads(previous_row["payload_json"])
        )
        status_row = connection.execute(
            "SELECT status FROM agent_status WHERE agent_id = ?",
            (heartbeat.agent_id,),
        ).fetchone()
        inserted = 0
        if status_row is None:
            self._insert_event(
                connection,
                occurred_at=received_at,
                event_type="agent_online",
                severity="info",
                agent_id=heartbeat.agent_id,
                message="Agent registered online",
            )
            inserted += 1
        elif status_row["status"] != "online":
            self._insert_event(
                connection,
                occurred_at=received_at,
                event_type="agent_recovered",
                severity="info",
                agent_id=heartbeat.agent_id,
                message="Agent heartbeat recovered",
                details={"previous_status": status_row["status"]},
            )
            inserted += 1
        connection.execute(
            """
            INSERT INTO agents (
                agent_id, agent_name, sw_version, sent_at, received_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                agent_name = excluded.agent_name,
                sw_version = excluded.sw_version,
                sent_at = excluded.sent_at,
                received_at = excluded.received_at,
                payload_json = excluded.payload_json
            """,
            (
                heartbeat.agent_id,
                heartbeat.agent_name,
                heartbeat.sw_version,
                isoformat_z(heartbeat.sent_at),
                isoformat_z(received_at),
                encoded,
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_status (agent_id, status, updated_at)
            VALUES (?, 'online', ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (heartbeat.agent_id, isoformat_z(received_at)),
        )
        inserted += self._record_topic_transitions(
            connection,
            previous=previous,
            current=payload,
            occurred_at=received_at,
        )
        inserted += self._record_runtime_kind_transitions(
            connection,
            previous=previous,
            current=payload,
            occurred_at=received_at,
        )
        if inserted:
            self._prune_events(connection)
        return inserted

    def upsert(
        self,
        heartbeat: AgentHeartbeatV1,
        *,
        received_at: datetime,
    ) -> int:
        with self._connect() as connection:
            return self._upsert_connection(connection, heartbeat, received_at)

    @staticmethod
    def _authenticate_agent_certificate_connection(
        connection: sqlite3.Connection,
        *,
        agent_id: str,
        certificate_sha256: str,
        certificate_identity_uri: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        registration = connection.execute(
            "SELECT * FROM agent_registrations WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if registration is None or not bool(registration["enabled"]):
            raise AgentAuthenticationError("Agent is not enabled in the registry allowlist")
        if registration["identity_uri"] != certificate_identity_uri:
            raise AgentAuthenticationError("certificate identity URI differs from Agent registry")
        profile = connection.execute(
            """
            SELECT profile_id FROM connection_profiles
            WHERE owner_kind = 'agent' AND agent_id = ?
                AND protocol = 'https_agent' AND enabled = 1
            """,
            (agent_id,),
        ).fetchone()
        if profile is None:
            raise AgentAuthenticationError(
                "Agent has no enabled HTTPS connection profile"
            )
        credential = connection.execute(
            """
            SELECT * FROM agent_credentials
            WHERE agent_id = ? AND certificate_sha256 = ? AND status = 'active'
            """,
            (agent_id, certificate_sha256),
        ).fetchone()
        if credential is None:
            raise AgentAuthenticationError("Agent certificate is not active in the registry")
        if not (
            parse_datetime(credential["not_before"])
            <= occurred_at
            <= parse_datetime(credential["not_after"])
        ):
            raise AgentAuthenticationError(
                "Agent certificate is outside its enrolled validity window"
            )
        return {
            "agent_id": agent_id,
            "profile_id": profile["profile_id"],
            "credential_id": credential["credential_id"],
        }

    def authenticate_agent_certificate(
        self,
        *,
        agent_id: str,
        certificate_sha256: str,
        certificate_identity_uri: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        """Authorize one enrolled Agent certificate outside heartbeat replay checks."""
        with self._connect() as connection:
            return self._authenticate_agent_certificate_connection(
                connection,
                agent_id=agent_id,
                certificate_sha256=certificate_sha256,
                certificate_identity_uri=certificate_identity_uri,
                occurred_at=occurred_at,
            )

    def secure_upsert(
        self,
        heartbeat: AgentHeartbeatV1,
        *,
        certificate_sha256: str,
        certificate_identity_uri: str,
        received_at: datetime,
        maximum_age_s: float = 12.0,
        maximum_future_s: float = 2.0,
    ) -> dict[str, Any]:
        if heartbeat.schema_version not in {2, 3} or heartbeat.boot_id is None or heartbeat.sequence is None:
            raise AgentAuthenticationError(
                "secure Agent ingress requires heartbeat schema v2 or v3"
            )
        age_s = (received_at - heartbeat.sent_at).total_seconds()
        if age_s > maximum_age_s:
            raise AgentReplayError("heartbeat sent_at exceeds the maximum accepted age")
        if age_s < -maximum_future_s:
            raise AgentReplayError("heartbeat sent_at is too far in the future")
        boot_id = str(heartbeat.boot_id)
        sent_at = isoformat_z(heartbeat.sent_at)
        updated_at = isoformat_z(received_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            authentication = self._authenticate_agent_certificate_connection(
                connection,
                agent_id=heartbeat.agent_id,
                certificate_sha256=certificate_sha256,
                certificate_identity_uri=certificate_identity_uri,
                occurred_at=received_at,
            )

            state = connection.execute(
                "SELECT * FROM agent_replay_state WHERE agent_id = ?",
                (heartbeat.agent_id,),
            ).fetchone()
            history = connection.execute(
                """
                SELECT * FROM agent_boot_history
                WHERE agent_id = ? AND boot_id = ?
                """,
                (heartbeat.agent_id, boot_id),
            ).fetchone()
            if state is None:
                if history is not None:
                    raise AgentReplayError("heartbeat uses a previously retired boot_id")
                connection.execute(
                    """
                    INSERT INTO agent_replay_state (
                        agent_id, boot_id, max_sequence, max_sent_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        heartbeat.agent_id,
                        boot_id,
                        heartbeat.sequence,
                        sent_at,
                        updated_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO agent_boot_history (
                        agent_id, boot_id, first_seen_at, last_sequence, last_sent_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        heartbeat.agent_id,
                        boot_id,
                        updated_at,
                        heartbeat.sequence,
                        sent_at,
                    ),
                )
            elif state["boot_id"] == boot_id:
                if heartbeat.sequence <= state["max_sequence"]:
                    raise AgentReplayError("heartbeat sequence did not advance")
                if heartbeat.sent_at < parse_datetime(state["max_sent_at"]):
                    raise AgentReplayError("heartbeat sent_at moved backwards")
                connection.execute(
                    """
                    UPDATE agent_replay_state
                    SET max_sequence = ?, max_sent_at = ?, updated_at = ?
                    WHERE agent_id = ?
                    """,
                    (heartbeat.sequence, sent_at, updated_at, heartbeat.agent_id),
                )
                connection.execute(
                    """
                    UPDATE agent_boot_history
                    SET last_sequence = ?, last_sent_at = ?
                    WHERE agent_id = ? AND boot_id = ?
                    """,
                    (heartbeat.sequence, sent_at, heartbeat.agent_id, boot_id),
                )
            else:
                if history is not None:
                    raise AgentReplayError("heartbeat attempted to return to a retired boot_id")
                if heartbeat.sent_at <= parse_datetime(state["max_sent_at"]):
                    raise AgentReplayError("new Agent boot must advance sent_at")
                connection.execute(
                    """
                    INSERT INTO agent_boot_history (
                        agent_id, boot_id, first_seen_at, last_sequence, last_sent_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        heartbeat.agent_id,
                        boot_id,
                        updated_at,
                        heartbeat.sequence,
                        sent_at,
                    ),
                )
                connection.execute(
                    """
                    UPDATE agent_replay_state
                    SET boot_id = ?, max_sequence = ?, max_sent_at = ?, updated_at = ?
                    WHERE agent_id = ?
                    """,
                    (
                        boot_id,
                        heartbeat.sequence,
                        sent_at,
                        updated_at,
                        heartbeat.agent_id,
                    ),
                )
            transitions = self._upsert_connection(connection, heartbeat, received_at)
        return {
            "transitions": transitions,
            "credential_id": authentication["credential_id"],
            "boot_id": boot_id,
            "sequence": heartbeat.sequence,
        }

    def reconcile_agent_statuses(
        self,
        agents: list[dict[str, Any]],
        *,
        occurred_at: datetime,
    ) -> int:
        inserted = 0
        with self._connect() as connection:
            for agent in agents:
                agent_id = agent["agent_id"]
                current_status = agent["status"]
                row = connection.execute(
                    "SELECT status FROM agent_status WHERE agent_id = ?",
                    (agent_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO agent_status (agent_id, status, updated_at)
                        VALUES (?, ?, ?)
                        """,
                        (agent_id, current_status, isoformat_z(occurred_at)),
                    )
                    continue
                previous_status = row["status"]
                if previous_status == current_status:
                    continue
                if current_status == "offline":
                    event_type = "agent_offline"
                    severity = "critical"
                    message = "Agent heartbeat is offline"
                elif current_status == "stale":
                    event_type = "agent_stale"
                    severity = "warning"
                    message = "Agent heartbeat is stale"
                else:
                    event_type = "agent_recovered"
                    severity = "info"
                    message = "Agent heartbeat recovered"
                self._insert_event(
                    connection,
                    occurred_at=occurred_at,
                    event_type=event_type,
                    severity=severity,
                    agent_id=agent_id,
                    message=message,
                    details={"previous_status": previous_status},
                )
                connection.execute(
                    """
                    UPDATE agent_status
                    SET status = ?, updated_at = ?
                    WHERE agent_id = ?
                    """,
                    (current_status, isoformat_z(occurred_at), agent_id),
                )
                inserted += 1
            if inserted:
                self._prune_events(connection)
        return inserted

    def list_snapshots(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json, received_at FROM agents "
                "ORDER BY agent_name COLLATE NOCASE, agent_id"
            ).fetchall()
        snapshots = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["received_at"] = row["received_at"]
            snapshots.append(payload)
        return snapshots

    def get_snapshot(self, agent_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json, received_at FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        payload["received_at"] = row["received_at"]
        return payload

    def list_events(
        self,
        *,
        limit: int = 100,
        agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("event limit must be between 1 and 500")
        query = "SELECT * FROM health_events"
        parameters: list[Any] = []
        if agent_id is not None:
            query += " WHERE agent_id = ?"
            parameters.append(agent_id)
        query += " ORDER BY id DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                "id": row["id"],
                "occurred_at": row["occurred_at"],
                "event_type": row["event_type"],
                "severity": row["severity"],
                "agent_id": row["agent_id"],
                "robot_id": row["robot_id"],
                "source_key": row["source_key"],
                "topic": row["topic"],
                "message": row["message"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    def event_counts_by_agent(self) -> dict[str, dict[str, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT agent_id, event_type, COUNT(*) AS count
                FROM health_events
                GROUP BY agent_id, event_type
                """
            ).fetchall()
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            counts.setdefault(row["agent_id"], {})[row["event_type"]] = row["count"]
        return counts

    @staticmethod
    def _encoded_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def _insert_configuration_audit(
        cls,
        connection: sqlite3.Connection,
        *,
        occurred_at: datetime,
        action: str,
        entity_type: str,
        entity_id: str,
        details: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO configuration_audit (
                occurred_at, action, entity_type, entity_id, details_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                isoformat_z(occurred_at),
                action,
                entity_type,
                entity_id,
                cls._encoded_json(details),
            ),
        )

    @staticmethod
    def _conflict_from_integrity(error: sqlite3.IntegrityError) -> None:
        message = str(error)
        if "assignments.asset_id" in message:
            raise ConfigurationConflict(
                "an assigned asset is already reserved by another READY task run"
            ) from error
        if "assets_observed_binding" in message:
            raise ConfigurationConflict(
                "the observed robot or camera is already registered as an asset"
            ) from error
        raise ConfigurationConflict(message) from error

    def schema_versions(self) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        return [row["version"] for row in rows]

    def create_agent_registration(
        self,
        body: AgentRegistrationCreate,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        identity_uri = body.identity_uri or f"urn:sang:agent:{body.agent_id}"
        timestamp = isoformat_z(occurred_at)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO agent_registrations (
                        agent_id, display_name, identity_uri, enabled, revision,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        body.agent_id,
                        body.display_name,
                        identity_uri,
                        int(body.enabled),
                        timestamp,
                        timestamp,
                    ),
                )
                self._insert_configuration_audit(
                    connection,
                    occurred_at=occurred_at,
                    action="create",
                    entity_type="agent_registration",
                    entity_id=body.agent_id,
                    details={"identity_uri": identity_uri, "enabled": body.enabled},
                )
        except sqlite3.IntegrityError as error:
            self._conflict_from_integrity(error)
        return {
            "agent_id": body.agent_id,
            "display_name": body.display_name,
            "identity_uri": identity_uri,
            "enabled": body.enabled,
            "revision": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def update_agent_registration(
        self,
        agent_id: str,
        body: AgentRegistrationUpdate,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        timestamp = isoformat_z(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_registrations WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if row is None:
                raise ConfigurationNotFound(f"unknown Agent registration: {agent_id}")
            if row["revision"] != body.expected_revision:
                raise ConfigurationConflict(
                    f"Agent registration revision is {row['revision']}, not "
                    f"{body.expected_revision}"
                )
            revision = row["revision"] + 1
            connection.execute(
                """
                UPDATE agent_registrations
                SET display_name = ?, enabled = ?, revision = ?, updated_at = ?
                WHERE agent_id = ?
                """,
                (body.display_name, int(body.enabled), revision, timestamp, agent_id),
            )
            self._insert_configuration_audit(
                connection,
                occurred_at=occurred_at,
                action="update",
                entity_type="agent_registration",
                entity_id=agent_id,
                details={"enabled": body.enabled, "revision": revision},
            )
        return {
            "agent_id": agent_id,
            "display_name": body.display_name,
            "identity_uri": row["identity_uri"],
            "enabled": body.enabled,
            "revision": revision,
            "created_at": row["created_at"],
            "updated_at": timestamp,
        }

    def create_agent_credential(
        self,
        body: AgentCredentialCreate,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        created_at = isoformat_z(occurred_at)
        not_before = isoformat_z(body.not_before)
        not_after = isoformat_z(body.not_after)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                registration = connection.execute(
                    "SELECT 1 FROM agent_registrations WHERE agent_id = ?",
                    (body.agent_id,),
                ).fetchone()
                if registration is None:
                    raise ConfigurationNotFound(
                        f"unknown Agent registration: {body.agent_id}"
                    )
                connection.execute(
                    """
                    INSERT INTO agent_credentials (
                        credential_id, agent_id, certificate_sha256,
                        certificate_ref, not_before, not_after, status,
                        created_at, revoked_at, revocation_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, NULL, NULL)
                    """,
                    (
                        body.credential_id,
                        body.agent_id,
                        body.certificate_sha256,
                        body.certificate_ref,
                        not_before,
                        not_after,
                        created_at,
                    ),
                )
                self._insert_configuration_audit(
                    connection,
                    occurred_at=occurred_at,
                    action="enroll",
                    entity_type="agent_credential",
                    entity_id=body.credential_id,
                    details={
                        "agent_id": body.agent_id,
                        "certificate_sha256": body.certificate_sha256,
                        "not_after": not_after,
                    },
                )
        except sqlite3.IntegrityError as error:
            self._conflict_from_integrity(error)
        return {
            **body.model_dump(mode="json"),
            "not_before": not_before,
            "not_after": not_after,
            "status": "active",
            "created_at": created_at,
            "revoked_at": None,
            "revocation_reason": None,
        }

    def revoke_agent_credential(
        self,
        credential_id: str,
        body: CredentialRevoke,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        revoked_at = isoformat_z(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_credentials WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
            if row is None:
                raise ConfigurationNotFound(f"unknown Agent credential: {credential_id}")
            if row["status"] == "revoked":
                return self._agent_credential_from_row(row)
            connection.execute(
                """
                UPDATE agent_credentials
                SET status = 'revoked', revoked_at = ?, revocation_reason = ?
                WHERE credential_id = ?
                """,
                (revoked_at, body.reason, credential_id),
            )
            self._insert_configuration_audit(
                connection,
                occurred_at=occurred_at,
                action="revoke",
                entity_type="agent_credential",
                entity_id=credential_id,
                details={"agent_id": row["agent_id"], "reason": body.reason},
            )
        return {
            **self._agent_credential_from_row(row),
            "status": "revoked",
            "revoked_at": revoked_at,
            "revocation_reason": body.reason,
        }

    @staticmethod
    def _agent_credential_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "credential_id": row["credential_id"],
            "agent_id": row["agent_id"],
            "certificate_sha256": row["certificate_sha256"],
            "certificate_ref": row["certificate_ref"],
            "not_before": row["not_before"],
            "not_after": row["not_after"],
            "status": row["status"],
            "created_at": row["created_at"],
            "revoked_at": row["revoked_at"],
            "revocation_reason": row["revocation_reason"],
        }

    @staticmethod
    def _connection_profile_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "profile_id": row["profile_id"],
            "owner_kind": row["owner_kind"],
            "agent_id": row["agent_id"],
            "asset_id": row["asset_id"],
            "protocol": row["protocol"],
            "desired_endpoint": row["desired_endpoint"],
            "expected_application_uri": row["expected_application_uri"],
            "security_mode": row["security_mode"],
            "security_policy": row["security_policy"],
            "client_certificate_ref": row["client_certificate_ref"],
            "client_private_key_ref": row["client_private_key_ref"],
            "trust_list_ref": row["trust_list_ref"],
            "crl_ref": row["crl_ref"],
            "server_certificate_ref": row["server_certificate_ref"],
            "user_certificate_ref": row["user_certificate_ref"],
            "user_private_key_ref": row["user_private_key_ref"],
            "enabled": bool(row["enabled"]),
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_connection_profile(
        self,
        body: ConnectionProfileCreate,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        timestamp = isoformat_z(occurred_at)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO connection_profiles (
                        profile_id, owner_kind, agent_id, asset_id, protocol,
                        desired_endpoint, expected_application_uri, security_mode,
                        security_policy, client_certificate_ref,
                        client_private_key_ref, trust_list_ref, crl_ref,
                        server_certificate_ref, user_certificate_ref,
                        user_private_key_ref, enabled,
                        revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        body.profile_id,
                        body.owner_kind,
                        body.agent_id,
                        body.asset_id,
                        body.protocol,
                        body.desired_endpoint,
                        body.expected_application_uri,
                        body.security_mode,
                        body.security_policy,
                        body.client_certificate_ref,
                        body.client_private_key_ref,
                        body.trust_list_ref,
                        body.crl_ref,
                        body.server_certificate_ref,
                        body.user_certificate_ref,
                        body.user_private_key_ref,
                        int(body.enabled),
                        timestamp,
                        timestamp,
                    ),
                )
                self._insert_configuration_audit(
                    connection,
                    occurred_at=occurred_at,
                    action="create",
                    entity_type="connection_profile",
                    entity_id=body.profile_id,
                    details={
                        "owner_kind": body.owner_kind,
                        "agent_id": body.agent_id,
                        "asset_id": body.asset_id,
                        "protocol": body.protocol,
                        "desired_endpoint": body.desired_endpoint,
                        "revision": 1,
                    },
                )
        except sqlite3.IntegrityError as error:
            self._conflict_from_integrity(error)
        return {
            **body.model_dump(),
            "revision": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def replace_connection_profile(
        self,
        profile_id: str,
        body: ConnectionProfileReplace,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        timestamp = isoformat_z(occurred_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM connection_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            if row is None:
                raise ConfigurationNotFound(f"unknown connection profile: {profile_id}")
            if row["revision"] != body.expected_revision:
                raise ConfigurationConflict(
                    f"connection profile revision is {row['revision']}, not "
                    f"{body.expected_revision}"
                )
            revision = row["revision"] + 1
            connection.execute(
                """
                UPDATE connection_profiles SET
                    protocol = ?, desired_endpoint = ?,
                    expected_application_uri = ?, security_mode = ?,
                    security_policy = ?, client_certificate_ref = ?,
                    client_private_key_ref = ?, trust_list_ref = ?, crl_ref = ?,
                    server_certificate_ref = ?, user_certificate_ref = ?,
                    user_private_key_ref = ?,
                    enabled = ?, revision = ?, updated_at = ?
                WHERE profile_id = ?
                """,
                (
                    body.protocol,
                    body.desired_endpoint,
                    body.expected_application_uri,
                    body.security_mode,
                    body.security_policy,
                    body.client_certificate_ref,
                    body.client_private_key_ref,
                    body.trust_list_ref,
                    body.crl_ref,
                    body.server_certificate_ref,
                    body.user_certificate_ref,
                    body.user_private_key_ref,
                    int(body.enabled),
                    revision,
                    timestamp,
                    profile_id,
                ),
            )
            self._insert_configuration_audit(
                connection,
                occurred_at=occurred_at,
                action="replace",
                entity_type="connection_profile",
                entity_id=profile_id,
                details={
                    "desired_endpoint": body.desired_endpoint,
                    "enabled": body.enabled,
                    "revision": revision,
                },
            )
        return {
            "profile_id": profile_id,
            "owner_kind": row["owner_kind"],
            "agent_id": row["agent_id"],
            "asset_id": row["asset_id"],
            **body.model_dump(exclude={"expected_revision"}),
            "revision": revision,
            "created_at": row["created_at"],
            "updated_at": timestamp,
        }

    def list_agent_registrations(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_registrations ORDER BY display_name, agent_id"
            ).fetchall()
        return [
            {
                "agent_id": row["agent_id"],
                "display_name": row["display_name"],
                "identity_uri": row["identity_uri"],
                "enabled": bool(row["enabled"]),
                "revision": row["revision"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def agent_is_registered(self, agent_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM agent_registrations WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        return row is not None

    def list_agent_credentials(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_credentials
                ORDER BY agent_id, created_at, credential_id
                """
            ).fetchall()
        return [self._agent_credential_from_row(row) for row in rows]

    def list_connection_profiles(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM connection_profiles ORDER BY profile_id"
            ).fetchall()
        return [self._connection_profile_from_row(row) for row in rows]

    def get_agent_connection_profile(
        self,
        agent_id: str,
        protocol: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM connection_profiles
                WHERE owner_kind = 'agent' AND agent_id = ? AND protocol = ?
                """,
                (agent_id, protocol),
            ).fetchone()
        return None if row is None else self._connection_profile_from_row(row)

    def record_connection_observation(
        self,
        profile_id: str,
        *,
        observed_at: datetime,
        observed_endpoint: str | None,
        observed_application_uri: str | None,
        peer_certificate_sha256: str | None,
        peer_address: str | None,
        namespace_uris: list[str] | None = None,
        succeeded: bool,
        error: str | None = None,
    ) -> dict[str, Any]:
        timestamp = isoformat_z(observed_at)
        if succeeded and error is not None:
            raise ValueError("successful connection observations cannot include an error")
        if not succeeded and (error is None or not error.strip()):
            raise ValueError("failed connection observations require an error")
        with self._connect() as connection:
            profile = connection.execute(
                "SELECT 1 FROM connection_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            if profile is None:
                raise ConfigurationNotFound(f"unknown connection profile: {profile_id}")
            previous = connection.execute(
                "SELECT last_success_at FROM connection_observations WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            last_success_at = (
                timestamp
                if succeeded
                else None if previous is None else previous["last_success_at"]
            )
            connection.execute(
                """
                INSERT INTO connection_observations (
                    profile_id, observed_endpoint, observed_application_uri,
                    peer_certificate_sha256, peer_address, namespace_uris_json,
                    observed_at, last_success_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    observed_endpoint = excluded.observed_endpoint,
                    observed_application_uri = excluded.observed_application_uri,
                    peer_certificate_sha256 = excluded.peer_certificate_sha256,
                    peer_address = excluded.peer_address,
                    namespace_uris_json = excluded.namespace_uris_json,
                    observed_at = excluded.observed_at,
                    last_success_at = excluded.last_success_at,
                    last_error = excluded.last_error
                """,
                (
                    profile_id,
                    observed_endpoint,
                    observed_application_uri,
                    peer_certificate_sha256,
                    peer_address,
                    self._encoded_json(namespace_uris or []),
                    timestamp,
                    last_success_at,
                    None if succeeded else error.strip(),
                ),
            )
        return {
            "profile_id": profile_id,
            "observed_endpoint": observed_endpoint,
            "observed_application_uri": observed_application_uri,
            "peer_certificate_sha256": peer_certificate_sha256,
            "peer_address": peer_address,
            "namespace_uris": namespace_uris or [],
            "observed_at": timestamp,
            "last_success_at": last_success_at,
            "last_error": None if succeeded else error.strip(),
        }

    def list_connection_observations(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM connection_observations ORDER BY profile_id"
            ).fetchall()
        return [
            {
                "profile_id": row["profile_id"],
                "observed_endpoint": row["observed_endpoint"],
                "observed_application_uri": row["observed_application_uri"],
                "peer_certificate_sha256": row["peer_certificate_sha256"],
                "peer_address": row["peer_address"],
                "namespace_uris": json.loads(row["namespace_uris_json"]),
                "observed_at": row["observed_at"],
                "last_success_at": row["last_success_at"],
                "last_error": row["last_error"],
            }
            for row in rows
        ]

    def create_processing_node(
        self,
        body: ProcessingNodeCreate,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if body.agent_ids:
                    placeholders = ",".join("?" for _ in body.agent_ids)
                    rows = connection.execute(
                        f"SELECT agent_id FROM agents WHERE agent_id IN ({placeholders})",
                        body.agent_ids,
                    ).fetchall()
                    found = {row["agent_id"] for row in rows}
                    missing = sorted(set(body.agent_ids) - found)
                    if missing:
                        raise ConfigurationNotFound(
                            f"unknown observed agent_ids: {', '.join(missing)}"
                        )
                created_at = isoformat_z(occurred_at)
                connection.execute(
                    """
                    INSERT INTO processing_nodes (node_id, display_name, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (body.node_id, body.display_name, created_at),
                )
                connection.executemany(
                    "INSERT INTO node_agents (node_id, agent_id) VALUES (?, ?)",
                    [(body.node_id, agent_id) for agent_id in body.agent_ids],
                )
                self._insert_configuration_audit(
                    connection,
                    occurred_at=occurred_at,
                    action="create",
                    entity_type="processing_node",
                    entity_id=body.node_id,
                    details={"agent_ids": body.agent_ids},
                )
        except sqlite3.IntegrityError as error:
            self._conflict_from_integrity(error)
        return {
            "node_id": body.node_id,
            "display_name": body.display_name,
            "agent_ids": body.agent_ids,
            "revision": 1,
            "lifecycle_state": "active",
            "created_at": created_at,
            "updated_at": created_at,
            "decommissioned_at": None,
            "decommission_reason": None,
        }

    @staticmethod
    def _validate_observed_agents(
        connection: sqlite3.Connection,
        agent_ids: list[str],
    ) -> None:
        if not agent_ids:
            return
        placeholders = ",".join("?" for _ in agent_ids)
        rows = connection.execute(
            f"SELECT agent_id FROM agents WHERE agent_id IN ({placeholders})",
            agent_ids,
        ).fetchall()
        found = {row["agent_id"] for row in rows}
        missing = sorted(set(agent_ids) - found)
        if missing:
            raise ConfigurationNotFound(
                f"unknown observed agent_ids: {', '.join(missing)}"
            )

    def replace_processing_node(
        self,
        node_id: str,
        body: ProcessingNodeReplace,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM processing_nodes WHERE node_id = ?",
                    (node_id,),
                ).fetchone()
                if row is None:
                    raise ConfigurationNotFound(f"unknown processing node: {node_id}")
                if row["revision"] != body.expected_revision:
                    raise ConfigurationConflict(
                        f"processing node revision is {row['revision']}, "
                        f"not {body.expected_revision}"
                    )
                if row["lifecycle_state"] != "active":
                    raise ConfigurationInvalid(
                        f"processing node {node_id} is decommissioned"
                    )
                self._validate_observed_agents(connection, body.agent_ids)
                bound_rows = connection.execute(
                    """
                    SELECT DISTINCT agent_id FROM assets
                    WHERE node_id = ? AND lifecycle_state = 'active'
                        AND agent_id IS NOT NULL
                    """,
                    (node_id,),
                ).fetchall()
                required_agents = {item["agent_id"] for item in bound_rows}
                missing_agents = sorted(required_agents - set(body.agent_ids))
                if missing_agents:
                    raise ConfigurationInvalid(
                        "processing node still has active assets bound to agents: "
                        + ", ".join(missing_agents)
                    )
                updated_at = isoformat_z(occurred_at)
                connection.execute(
                    """
                    UPDATE processing_nodes
                    SET display_name = ?, revision = revision + 1, updated_at = ?
                    WHERE node_id = ?
                    """,
                    (body.display_name, updated_at, node_id),
                )
                connection.execute(
                    "DELETE FROM node_agents WHERE node_id = ?",
                    (node_id,),
                )
                connection.executemany(
                    "INSERT INTO node_agents (node_id, agent_id) VALUES (?, ?)",
                    [(node_id, agent_id) for agent_id in body.agent_ids],
                )
                self._insert_configuration_audit(
                    connection,
                    occurred_at=occurred_at,
                    action="replace",
                    entity_type="processing_node",
                    entity_id=node_id,
                    details={
                        "previous_revision": body.expected_revision,
                        "agent_ids": body.agent_ids,
                    },
                )
        except sqlite3.IntegrityError as error:
            self._conflict_from_integrity(error)
        return next(
            item for item in self.list_processing_nodes()
            if item["node_id"] == node_id
        )

    def decommission_processing_node(
        self,
        node_id: str,
        body: DecommissionRequest,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM processing_nodes WHERE node_id = ?",
                (node_id,),
            ).fetchone()
            if row is None:
                raise ConfigurationNotFound(f"unknown processing node: {node_id}")
            if row["revision"] != body.expected_revision:
                raise ConfigurationConflict(
                    f"processing node revision is {row['revision']}, "
                    f"not {body.expected_revision}"
                )
            if row["lifecycle_state"] != "active":
                raise ConfigurationInvalid(
                    f"processing node {node_id} is already decommissioned"
                )
            active_assets = connection.execute(
                """
                SELECT asset_id FROM assets
                WHERE node_id = ? AND lifecycle_state = 'active'
                ORDER BY asset_id
                """,
                (node_id,),
            ).fetchall()
            if active_assets:
                raise ConfigurationConflict(
                    "processing node has active assets: "
                    + ", ".join(item["asset_id"] for item in active_assets)
                )
            timestamp = isoformat_z(occurred_at)
            connection.execute(
                """
                UPDATE processing_nodes
                SET lifecycle_state = 'decommissioned', revision = revision + 1,
                    updated_at = ?, decommissioned_at = ?, decommission_reason = ?
                WHERE node_id = ?
                """,
                (timestamp, timestamp, body.reason, node_id),
            )
            self._insert_configuration_audit(
                connection,
                occurred_at=occurred_at,
                action="decommission",
                entity_type="processing_node",
                entity_id=node_id,
                details={
                    "reason": body.reason,
                    "previous_revision": body.expected_revision,
                },
            )
        return next(
            item for item in self.list_processing_nodes()
            if item["node_id"] == node_id
        )

    @staticmethod
    def _find_observed_robot(
        connection: sqlite3.Connection,
        *,
        agent_id: str,
        robot_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT payload_json FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise ConfigurationNotFound(f"unknown observed agent_id: {agent_id}")
        payload = json.loads(row["payload_json"])
        for robot in payload.get("robots", []):
            if robot["robot_id"] == robot_id:
                return robot
        raise ConfigurationNotFound(
            f"agent {agent_id} does not currently observe robot {robot_id}"
        )

    @classmethod
    def _validate_asset_spec(
        cls,
        connection: sqlite3.Connection,
        *,
        asset_type: str,
        node_id: str | None,
        binding: Any,
        runtime_kind: str | None,
    ) -> None:
        if node_id is not None:
            node = connection.execute(
                "SELECT * FROM processing_nodes WHERE node_id = ?",
                (node_id,),
            ).fetchone()
            if node is None:
                raise ConfigurationNotFound(
                    f"unknown processing node: {node_id}"
                )
            if node["lifecycle_state"] != "active":
                raise ConfigurationInvalid(
                    f"processing node {node_id} is decommissioned"
                )
        if binding is None:
            return
        if node_id is None:
            raise ConfigurationInvalid("observed bindings require node_id")
        if asset_type not in {"robot", "camera"}:
            raise ConfigurationInvalid(
                "only robot and camera assets may use observed bindings"
            )
        if asset_type == "camera" and binding.source_key is None:
            raise ConfigurationInvalid("camera bindings require source_key")
        if asset_type == "robot" and binding.source_key is not None:
            raise ConfigurationInvalid("robot bindings must not include source_key")
        membership = connection.execute(
            """
            SELECT 1 FROM node_agents
            WHERE node_id = ? AND agent_id = ?
            """,
            (node_id, binding.agent_id),
        ).fetchone()
        if membership is None:
            raise ConfigurationInvalid(
                f"agent {binding.agent_id} is not assigned to node {node_id}"
            )
        robot = cls._find_observed_robot(
            connection,
            agent_id=binding.agent_id,
            robot_id=binding.robot_id,
        )
        observed_runtime_kind = robot.get("runtime_kind")
        if (
            runtime_kind is not None
            and observed_runtime_kind is not None
            and runtime_kind != observed_runtime_kind
        ):
            raise ConfigurationInvalid(
                f"asset runtime_kind {runtime_kind!r} differs from "
                f"observed runtime_kind {observed_runtime_kind!r}"
            )
        if binding.source_key is None:
            return
        source = next(
            (
                item
                for item in robot.get("sources", [])
                if item["key"] == binding.source_key
            ),
            None,
        )
        if source is None:
            raise ConfigurationNotFound(
                f"robot {binding.robot_id} does not currently expose "
                f"source {binding.source_key}"
            )
        if source["kind"] != "camera":
            raise ConfigurationInvalid(
                f"source {binding.source_key} is not a camera"
            )

    def create_asset(
        self,
        body: AssetCreate,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        binding = body.binding
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._validate_asset_spec(
                    connection,
                    asset_type=body.asset_type,
                    node_id=body.node_id,
                    binding=binding,
                    runtime_kind=body.runtime_kind,
                )
                created_at = isoformat_z(occurred_at)
                connection.execute(
                    """
                    INSERT INTO assets (
                        asset_id, asset_type, display_name, node_id,
                        agent_id, robot_id, source_key, capabilities_json,
                        created_at, runtime_kind
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        body.asset_id,
                        body.asset_type,
                        body.display_name,
                        body.node_id,
                        None if binding is None else binding.agent_id,
                        None if binding is None else binding.robot_id,
                        None if binding is None else binding.source_key,
                        self._encoded_json(body.capabilities),
                        created_at,
                        body.runtime_kind,
                    ),
                )
                self._insert_configuration_audit(
                    connection,
                    occurred_at=occurred_at,
                    action="create",
                    entity_type="asset",
                    entity_id=body.asset_id,
                    details={
                        "asset_type": body.asset_type,
                        "runtime_kind": body.runtime_kind,
                        "node_id": body.node_id,
                        "binding": None if binding is None else binding.model_dump(),
                    },
                )
        except sqlite3.IntegrityError as error:
            self._conflict_from_integrity(error)
        return {
            **body.model_dump(),
            "revision": 1,
            "lifecycle_state": "active",
            "created_at": created_at,
            "updated_at": created_at,
            "decommissioned_at": None,
            "decommission_reason": None,
        }

    def set_asset_runtime_kind(
        self,
        asset_id: str,
        body: AssetRuntimeKindUpdate,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            if row is None:
                raise ConfigurationNotFound(f"unknown asset: {asset_id}")
            if row["revision"] != body.expected_revision:
                raise ConfigurationConflict(
                    f"asset revision is {row['revision']}, not {body.expected_revision}"
                )
            if row["lifecycle_state"] != "active":
                raise ConfigurationInvalid(f"asset {asset_id} is decommissioned")
            if body.runtime_kind is not None and row["agent_id"] is not None:
                robot = self._find_observed_robot(
                    connection,
                    agent_id=row["agent_id"],
                    robot_id=row["robot_id"],
                )
                observed_runtime_kind = robot.get("runtime_kind")
                if (
                    observed_runtime_kind is not None
                    and observed_runtime_kind != body.runtime_kind
                ):
                    raise ConfigurationInvalid(
                        f"asset runtime_kind {body.runtime_kind!r} differs from "
                        f"observed runtime_kind {observed_runtime_kind!r}"
                    )
            updated_at = isoformat_z(occurred_at)
            connection.execute(
                """
                UPDATE assets
                SET runtime_kind = ?, revision = revision + 1, updated_at = ?
                WHERE asset_id = ?
                """,
                (body.runtime_kind, updated_at, asset_id),
            )
            self._insert_configuration_audit(
                connection,
                occurred_at=occurred_at,
                action="set_runtime_kind",
                entity_type="asset",
                entity_id=asset_id,
                details={
                    "previous_runtime_kind": row["runtime_kind"],
                    "runtime_kind": body.runtime_kind,
                    "previous_revision": body.expected_revision,
                },
            )
            updated = connection.execute(
                "SELECT * FROM assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        return self._asset_from_row(updated)

    def replace_asset(
        self,
        asset_id: str,
        body: AssetReplace,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        binding = body.binding
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM assets WHERE asset_id = ?",
                    (asset_id,),
                ).fetchone()
                if row is None:
                    raise ConfigurationNotFound(f"unknown asset: {asset_id}")
                if row["revision"] != body.expected_revision:
                    raise ConfigurationConflict(
                        f"asset revision is {row['revision']}, "
                        f"not {body.expected_revision}"
                    )
                if row["lifecycle_state"] != "active":
                    raise ConfigurationInvalid(f"asset {asset_id} is decommissioned")
                reservation = connection.execute(
                    """
                    SELECT run_id FROM assignments
                    WHERE asset_id = ? AND reserved = 1
                    """,
                    (asset_id,),
                ).fetchone()
                if reservation is not None:
                    raise ConfigurationConflict(
                        f"asset {asset_id} is reserved by task run "
                        f"{reservation['run_id']}"
                    )
                self._validate_asset_spec(
                    connection,
                    asset_type=row["asset_type"],
                    node_id=body.node_id,
                    binding=binding,
                    runtime_kind=body.runtime_kind,
                )
                timestamp = isoformat_z(occurred_at)
                connection.execute(
                    """
                    UPDATE assets
                    SET display_name = ?, node_id = ?, agent_id = ?, robot_id = ?,
                        source_key = ?, capabilities_json = ?, runtime_kind = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE asset_id = ?
                    """,
                    (
                        body.display_name,
                        body.node_id,
                        None if binding is None else binding.agent_id,
                        None if binding is None else binding.robot_id,
                        None if binding is None else binding.source_key,
                        self._encoded_json(body.capabilities),
                        body.runtime_kind,
                        timestamp,
                        asset_id,
                    ),
                )
                self._insert_configuration_audit(
                    connection,
                    occurred_at=occurred_at,
                    action="replace",
                    entity_type="asset",
                    entity_id=asset_id,
                    details={
                        "previous_revision": body.expected_revision,
                        "node_id": body.node_id,
                        "binding": None if binding is None else binding.model_dump(),
                        "runtime_kind": body.runtime_kind,
                    },
                )
                updated = connection.execute(
                    "SELECT * FROM assets WHERE asset_id = ?",
                    (asset_id,),
                ).fetchone()
                return self._asset_from_row(updated)
        except sqlite3.IntegrityError as error:
            self._conflict_from_integrity(error)

    def decommission_asset(
        self,
        asset_id: str,
        body: DecommissionRequest,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            if row is None:
                raise ConfigurationNotFound(f"unknown asset: {asset_id}")
            if row["revision"] != body.expected_revision:
                raise ConfigurationConflict(
                    f"asset revision is {row['revision']}, not {body.expected_revision}"
                )
            if row["lifecycle_state"] != "active":
                raise ConfigurationInvalid(f"asset {asset_id} is already decommissioned")
            reservation = connection.execute(
                """
                SELECT run_id FROM assignments
                WHERE asset_id = ? AND reserved = 1
                """,
                (asset_id,),
            ).fetchone()
            if reservation is not None:
                raise ConfigurationConflict(
                    f"asset {asset_id} is reserved by task run {reservation['run_id']}"
                )
            timestamp = isoformat_z(occurred_at)
            connection.execute(
                """
                UPDATE assets
                SET lifecycle_state = 'decommissioned', revision = revision + 1,
                    updated_at = ?, decommissioned_at = ?, decommission_reason = ?
                WHERE asset_id = ?
                """,
                (timestamp, timestamp, body.reason, asset_id),
            )
            self._insert_configuration_audit(
                connection,
                occurred_at=occurred_at,
                action="decommission",
                entity_type="asset",
                entity_id=asset_id,
                details={
                    "reason": body.reason,
                    "previous_revision": body.expected_revision,
                },
            )
            updated = connection.execute(
                "SELECT * FROM assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            return self._asset_from_row(updated)

    def create_workcell(
        self,
        body: WorkcellCreate,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._validate_workcell_assets(connection, body.asset_ids)
                created_at = isoformat_z(occurred_at)
                connection.execute(
                    """
                    INSERT INTO workcells (
                        workcell_id, display_name, description, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        body.workcell_id,
                        body.display_name,
                        body.description,
                        created_at,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO workcell_assets (workcell_id, asset_id)
                    VALUES (?, ?)
                    """,
                    [
                        (body.workcell_id, asset_id)
                        for asset_id in body.asset_ids
                    ],
                )
                self._insert_configuration_audit(
                    connection,
                    occurred_at=occurred_at,
                    action="create",
                    entity_type="workcell",
                    entity_id=body.workcell_id,
                    details={"asset_ids": body.asset_ids},
                )
        except sqlite3.IntegrityError as error:
            self._conflict_from_integrity(error)
        return {
            **body.model_dump(),
            "revision": 1,
            "lifecycle_state": "active",
            "created_at": created_at,
            "updated_at": created_at,
            "decommissioned_at": None,
            "decommission_reason": None,
        }

    @staticmethod
    def _validate_workcell_assets(
        connection: sqlite3.Connection,
        asset_ids: list[str],
    ) -> None:
        if not asset_ids:
            return
        placeholders = ",".join("?" for _ in asset_ids)
        rows = connection.execute(
            f"SELECT asset_id, lifecycle_state FROM assets "
            f"WHERE asset_id IN ({placeholders})",
            asset_ids,
        ).fetchall()
        found = {row["asset_id"] for row in rows}
        missing = sorted(set(asset_ids) - found)
        if missing:
            raise ConfigurationNotFound(
                f"unknown asset_ids: {', '.join(missing)}"
            )
        decommissioned = sorted(
            row["asset_id"]
            for row in rows
            if row["lifecycle_state"] != "active"
        )
        if decommissioned:
            raise ConfigurationInvalid(
                "decommissioned assets cannot join a workcell: "
                + ", ".join(decommissioned)
            )

    def replace_workcell(
        self,
        workcell_id: str,
        body: WorkcellReplace,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM workcells WHERE workcell_id = ?",
                    (workcell_id,),
                ).fetchone()
                if row is None:
                    raise ConfigurationNotFound(f"unknown workcell: {workcell_id}")
                if row["revision"] != body.expected_revision:
                    raise ConfigurationConflict(
                        f"workcell revision is {row['revision']}, "
                        f"not {body.expected_revision}"
                    )
                if row["lifecycle_state"] != "active":
                    raise ConfigurationInvalid(
                        f"workcell {workcell_id} is decommissioned"
                    )
                active_run = connection.execute(
                    """
                    SELECT run_id FROM task_runs
                    WHERE workcell_id = ? AND state IN ('DRAFT', 'READY', 'ACTIVE')
                    ORDER BY created_at LIMIT 1
                    """,
                    (workcell_id,),
                ).fetchone()
                if active_run is not None:
                    raise ConfigurationConflict(
                        f"workcell {workcell_id} is used by non-terminal task run "
                        f"{active_run['run_id']}"
                    )
                self._validate_workcell_assets(connection, body.asset_ids)
                timestamp = isoformat_z(occurred_at)
                connection.execute(
                    """
                    UPDATE workcells
                    SET display_name = ?, description = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE workcell_id = ?
                    """,
                    (body.display_name, body.description, timestamp, workcell_id),
                )
                connection.execute(
                    "DELETE FROM workcell_assets WHERE workcell_id = ?",
                    (workcell_id,),
                )
                connection.executemany(
                    "INSERT INTO workcell_assets (workcell_id, asset_id) VALUES (?, ?)",
                    [(workcell_id, asset_id) for asset_id in body.asset_ids],
                )
                self._insert_configuration_audit(
                    connection,
                    occurred_at=occurred_at,
                    action="replace",
                    entity_type="workcell",
                    entity_id=workcell_id,
                    details={
                        "previous_revision": body.expected_revision,
                        "asset_ids": body.asset_ids,
                    },
                )
        except sqlite3.IntegrityError as error:
            self._conflict_from_integrity(error)
        return next(
            item for item in self.list_workcells()
            if item["workcell_id"] == workcell_id
        )

    def decommission_workcell(
        self,
        workcell_id: str,
        body: DecommissionRequest,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workcells WHERE workcell_id = ?",
                (workcell_id,),
            ).fetchone()
            if row is None:
                raise ConfigurationNotFound(f"unknown workcell: {workcell_id}")
            if row["revision"] != body.expected_revision:
                raise ConfigurationConflict(
                    f"workcell revision is {row['revision']}, "
                    f"not {body.expected_revision}"
                )
            if row["lifecycle_state"] != "active":
                raise ConfigurationInvalid(
                    f"workcell {workcell_id} is already decommissioned"
                )
            active_run = connection.execute(
                """
                SELECT run_id FROM task_runs
                WHERE workcell_id = ? AND state IN ('DRAFT', 'READY', 'ACTIVE')
                ORDER BY created_at LIMIT 1
                """,
                (workcell_id,),
            ).fetchone()
            if active_run is not None:
                raise ConfigurationConflict(
                    f"workcell {workcell_id} is used by non-terminal task run "
                    f"{active_run['run_id']}"
                )
            timestamp = isoformat_z(occurred_at)
            connection.execute(
                """
                UPDATE workcells
                SET lifecycle_state = 'decommissioned', revision = revision + 1,
                    updated_at = ?, decommissioned_at = ?, decommission_reason = ?
                WHERE workcell_id = ?
                """,
                (timestamp, timestamp, body.reason, workcell_id),
            )
            self._insert_configuration_audit(
                connection,
                occurred_at=occurred_at,
                action="decommission",
                entity_type="workcell",
                entity_id=workcell_id,
                details={
                    "reason": body.reason,
                    "previous_revision": body.expected_revision,
                },
            )
        return next(
            item for item in self.list_workcells()
            if item["workcell_id"] == workcell_id
        )

    def create_task_definition(
        self,
        body: TaskDefinitionCreate,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT MAX(version) AS latest_version
                    FROM task_definitions
                    WHERE task_definition_id = ?
                    """,
                    (body.task_definition_id,),
                ).fetchone()
                latest = row["latest_version"]
                expected = 1 if latest is None else latest + 1
                if body.version != expected:
                    raise ConfigurationConflict(
                        f"next version for {body.task_definition_id} must be {expected}"
                    )
                created_at = isoformat_z(occurred_at)
                roles = [role.model_dump() for role in body.roles]
                connection.execute(
                    """
                    INSERT INTO task_definitions (
                        task_definition_id, version, display_name,
                        description, roles_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        body.task_definition_id,
                        body.version,
                        body.display_name,
                        body.description,
                        self._encoded_json(roles),
                        created_at,
                    ),
                )
                self._insert_configuration_audit(
                    connection,
                    occurred_at=occurred_at,
                    action="create_version",
                    entity_type="task_definition",
                    entity_id=f"{body.task_definition_id}@{body.version}",
                    details={"roles": roles},
                )
        except sqlite3.IntegrityError as error:
            self._conflict_from_integrity(error)
        return {
            **body.model_dump(),
            "created_at": created_at,
        }

    @staticmethod
    def _task_definition_row(
        connection: sqlite3.Connection,
        task_definition_id: str,
        task_version: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM task_definitions
            WHERE task_definition_id = ? AND version = ?
            """,
            (task_definition_id, task_version),
        ).fetchone()
        if row is None:
            raise ConfigurationNotFound(
                f"unknown task definition: {task_definition_id}@{task_version}"
            )
        return row

    @classmethod
    def _resolve_task_assignments(
        cls,
        connection: sqlite3.Connection,
        *,
        definition: sqlite3.Row,
        assignments: list[AssignmentCreate],
        workcell_id: str | None,
        require_complete: bool,
    ) -> list[dict[str, Any]]:
        roles = {
            role["role"]: role
            for role in json.loads(definition["roles_json"])
        }
        by_role = {item.role: item for item in assignments}
        unknown_roles = sorted(set(by_role) - set(roles))
        if unknown_roles:
            raise ConfigurationInvalid(
                f"unknown task roles: {', '.join(unknown_roles)}"
            )
        missing_roles = sorted(set(roles) - set(by_role))
        if require_complete and missing_roles:
            raise ConfigurationInvalid(
                f"READY task run is missing roles: {', '.join(missing_roles)}"
            )

        if workcell_id is not None:
            workcell = connection.execute(
                "SELECT * FROM workcells WHERE workcell_id = ?",
                (workcell_id,),
            ).fetchone()
            if workcell is None:
                raise ConfigurationNotFound(f"unknown workcell: {workcell_id}")
            if workcell["lifecycle_state"] != "active":
                raise ConfigurationInvalid(
                    f"workcell {workcell_id} is decommissioned"
                )

        resolved = []
        for assignment in assignments:
            asset = connection.execute(
                "SELECT * FROM assets WHERE asset_id = ?",
                (assignment.asset_id,),
            ).fetchone()
            if asset is None:
                raise ConfigurationNotFound(f"unknown asset: {assignment.asset_id}")
            if asset["lifecycle_state"] != "active":
                raise ConfigurationInvalid(
                    f"asset {assignment.asset_id} is decommissioned"
                )
            role = roles[assignment.role]
            if asset["asset_type"] != role["asset_type"]:
                raise ConfigurationInvalid(
                    f"role {assignment.role} requires {role['asset_type']}, "
                    f"not {asset['asset_type']}"
                )
            if assignment.access_mode != role["access_mode"]:
                raise ConfigurationInvalid(
                    f"role {assignment.role} requires access mode "
                    f"{role['access_mode']}"
                )
            if workcell_id is not None:
                membership = connection.execute(
                    """
                    SELECT 1 FROM workcell_assets
                    WHERE workcell_id = ? AND asset_id = ?
                    """,
                    (workcell_id, assignment.asset_id),
                ).fetchone()
                if membership is None:
                    raise ConfigurationInvalid(
                        f"asset {assignment.asset_id} is not in workcell {workcell_id}"
                    )
            resolved.append(
                {
                    "role": assignment.role,
                    "asset_id": assignment.asset_id,
                    "access_mode": assignment.access_mode,
                }
            )
        return resolved

    def create_task_run(
        self,
        body: TaskRunCreate,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                definition = self._task_definition_row(
                    connection,
                    body.task_definition_id,
                    body.task_version,
                )
                resolved_assignments = self._resolve_task_assignments(
                    connection,
                    definition=definition,
                    assignments=body.assignments,
                    workcell_id=body.workcell_id,
                    require_complete=body.state == "READY",
                )
                created_at = isoformat_z(occurred_at)
                connection.execute(
                    """
                    INSERT INTO task_runs (
                        run_id, task_definition_id, task_version,
                        workcell_id, display_name, state, revision,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        body.run_id,
                        body.task_definition_id,
                        body.task_version,
                        body.workcell_id,
                        body.display_name,
                        body.state,
                        created_at,
                        created_at,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO assignments (
                        run_id, role, asset_id, access_mode, reserved
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            body.run_id,
                            item["role"],
                            item["asset_id"],
                            item["access_mode"],
                            int(body.state == "READY"),
                        )
                        for item in resolved_assignments
                    ],
                )
                self._insert_configuration_audit(
                    connection,
                    occurred_at=occurred_at,
                    action="create",
                    entity_type="task_run",
                    entity_id=body.run_id,
                    details={
                        "state": body.state,
                        "task_definition": (
                            f"{body.task_definition_id}@{body.task_version}"
                        ),
                        "assignments": resolved_assignments,
                    },
                )
        except sqlite3.IntegrityError as error:
            self._conflict_from_integrity(error)
        return {
            **body.model_dump(),
            "revision": 1,
            "created_at": created_at,
            "updated_at": created_at,
            "started_at": None,
            "finished_at": None,
            "terminal_reason": None,
        }

    def replace_task_run(
        self,
        run_id: str,
        body: TaskRunReplace,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM task_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise ConfigurationNotFound(f"unknown task run: {run_id}")
                if row["revision"] != body.expected_revision:
                    raise ConfigurationConflict(
                        f"task run revision is {row['revision']}, "
                        f"not {body.expected_revision}"
                    )
                if row["state"] != "DRAFT":
                    raise ConfigurationInvalid(
                        "only DRAFT task runs may be edited"
                    )
                definition = self._task_definition_row(
                    connection,
                    row["task_definition_id"],
                    row["task_version"],
                )
                assignments = self._resolve_task_assignments(
                    connection,
                    definition=definition,
                    assignments=body.assignments,
                    workcell_id=body.workcell_id,
                    require_complete=False,
                )
                updated_at = isoformat_z(occurred_at)
                connection.execute(
                    """
                    UPDATE task_runs
                    SET workcell_id = ?, display_name = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE run_id = ?
                    """,
                    (body.workcell_id, body.display_name, updated_at, run_id),
                )
                connection.execute(
                    "DELETE FROM assignments WHERE run_id = ?",
                    (run_id,),
                )
                connection.executemany(
                    """
                    INSERT INTO assignments (
                        run_id, role, asset_id, access_mode, reserved
                    ) VALUES (?, ?, ?, ?, 0)
                    """,
                    [
                        (
                            run_id,
                            assignment["role"],
                            assignment["asset_id"],
                            assignment["access_mode"],
                        )
                        for assignment in assignments
                    ],
                )
                self._insert_configuration_audit(
                    connection,
                    occurred_at=occurred_at,
                    action="replace",
                    entity_type="task_run",
                    entity_id=run_id,
                    details={
                        "previous_revision": body.expected_revision,
                        "workcell_id": body.workcell_id,
                        "assignments": assignments,
                    },
                )
                updated = connection.execute(
                    "SELECT * FROM task_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                return self._task_run_from_row(connection, updated)
        except sqlite3.IntegrityError as error:
            self._conflict_from_integrity(error)

    def transition_task_run(
        self,
        run_id: str,
        body: TaskRunTransition,
        *,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        allowed = {
            "DRAFT": {"READY"},
            "READY": {"ACTIVE"},
            "ACTIVE": {"COMPLETE", "FAILED"},
            "COMPLETE": set(),
            "FAILED": set(),
        }
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM task_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise ConfigurationNotFound(f"unknown task run: {run_id}")
                if row["revision"] != body.expected_revision:
                    raise ConfigurationConflict(
                        f"task run revision is {row['revision']}, "
                        f"not {body.expected_revision}"
                    )
                previous_state = row["state"]
                if body.state not in allowed[previous_state]:
                    raise ConfigurationInvalid(
                        f"invalid task run transition: {previous_state} -> {body.state}"
                    )

                if body.state == "READY":
                    definition = self._task_definition_row(
                        connection,
                        row["task_definition_id"],
                        row["task_version"],
                    )
                    assignment_rows = connection.execute(
                        """
                        SELECT role, asset_id, access_mode
                        FROM assignments WHERE run_id = ? ORDER BY role
                        """,
                        (run_id,),
                    ).fetchall()
                    self._resolve_task_assignments(
                        connection,
                        definition=definition,
                        assignments=[
                            AssignmentCreate.model_validate(dict(item))
                            for item in assignment_rows
                        ],
                        workcell_id=row["workcell_id"],
                        require_complete=True,
                    )
                    connection.execute(
                        "UPDATE assignments SET reserved = 1 WHERE run_id = ?",
                        (run_id,),
                    )
                elif body.state in {"COMPLETE", "FAILED"}:
                    connection.execute(
                        "UPDATE assignments SET reserved = 0 WHERE run_id = ?",
                        (run_id,),
                    )

                timestamp = isoformat_z(occurred_at)
                started_at = timestamp if body.state == "ACTIVE" else row["started_at"]
                finished_at = (
                    timestamp
                    if body.state in {"COMPLETE", "FAILED"}
                    else row["finished_at"]
                )
                connection.execute(
                    """
                    UPDATE task_runs
                    SET state = ?, revision = revision + 1, updated_at = ?,
                        started_at = ?, finished_at = ?, terminal_reason = ?
                    WHERE run_id = ?
                    """,
                    (
                        body.state,
                        timestamp,
                        started_at,
                        finished_at,
                        body.reason,
                        run_id,
                    ),
                )
                self._insert_configuration_audit(
                    connection,
                    occurred_at=occurred_at,
                    action="transition",
                    entity_type="task_run",
                    entity_id=run_id,
                    details={
                        "from": previous_state,
                        "to": body.state,
                        "reason": body.reason,
                        "previous_revision": body.expected_revision,
                    },
                )
                updated = connection.execute(
                    "SELECT * FROM task_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                return self._task_run_from_row(connection, updated)
        except sqlite3.IntegrityError as error:
            self._conflict_from_integrity(error)

    @staticmethod
    def _asset_from_row(row: sqlite3.Row) -> dict[str, Any]:
        binding = None
        if row["agent_id"] is not None:
            binding = {
                "agent_id": row["agent_id"],
                "robot_id": row["robot_id"],
                "source_key": row["source_key"],
            }
        return {
            "asset_id": row["asset_id"],
            "asset_type": row["asset_type"],
            "display_name": row["display_name"],
            "runtime_kind": row["runtime_kind"],
            "node_id": row["node_id"],
            "binding": binding,
            "capabilities": json.loads(row["capabilities_json"]),
            "revision": row["revision"],
            "lifecycle_state": row["lifecycle_state"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "decommissioned_at": row["decommissioned_at"],
            "decommission_reason": row["decommission_reason"],
        }

    def list_processing_nodes(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM processing_nodes ORDER BY display_name, node_id"
            ).fetchall()
            memberships = connection.execute(
                "SELECT node_id, agent_id FROM node_agents ORDER BY agent_id"
            ).fetchall()
        agents_by_node: dict[str, list[str]] = {}
        for membership in memberships:
            agents_by_node.setdefault(membership["node_id"], []).append(
                membership["agent_id"]
            )
        return [
            {
                "node_id": row["node_id"],
                "display_name": row["display_name"],
                "agent_ids": agents_by_node.get(row["node_id"], []),
                "revision": row["revision"],
                "lifecycle_state": row["lifecycle_state"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "decommissioned_at": row["decommissioned_at"],
                "decommission_reason": row["decommission_reason"],
            }
            for row in rows
        ]

    def list_assets(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM assets ORDER BY display_name, asset_id"
            ).fetchall()
        return [self._asset_from_row(row) for row in rows]

    def list_workcells(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workcells ORDER BY display_name, workcell_id"
            ).fetchall()
            memberships = connection.execute(
                """
                SELECT workcell_id, asset_id FROM workcell_assets
                ORDER BY asset_id
                """
            ).fetchall()
        assets_by_workcell: dict[str, list[str]] = {}
        for membership in memberships:
            assets_by_workcell.setdefault(membership["workcell_id"], []).append(
                membership["asset_id"]
            )
        return [
            {
                "workcell_id": row["workcell_id"],
                "display_name": row["display_name"],
                "description": row["description"],
                "asset_ids": assets_by_workcell.get(row["workcell_id"], []),
                "revision": row["revision"],
                "lifecycle_state": row["lifecycle_state"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "decommissioned_at": row["decommissioned_at"],
                "decommission_reason": row["decommission_reason"],
            }
            for row in rows
        ]

    def list_task_definitions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_definitions
                ORDER BY task_definition_id, version DESC
                """
            ).fetchall()
        return [
            {
                "task_definition_id": row["task_definition_id"],
                "version": row["version"],
                "display_name": row["display_name"],
                "description": row["description"],
                "roles": json.loads(row["roles_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @classmethod
    def _task_run_from_row(
        cls,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        assignment_rows = connection.execute(
            """
            SELECT assignments.role, assignments.access_mode,
                   assignments.reserved, assets.*
            FROM assignments
            JOIN assets ON assets.asset_id = assignments.asset_id
            WHERE assignments.run_id = ?
            ORDER BY assignments.role
            """,
            (row["run_id"],),
        ).fetchall()
        assignments = []
        for assignment in assignment_rows:
            assignments.append(
                {
                    "role": assignment["role"],
                    "asset_id": assignment["asset_id"],
                    "access_mode": assignment["access_mode"],
                    "reserved": bool(assignment["reserved"]),
                    "asset": cls._asset_from_row(assignment),
                }
            )
        return {
            "run_id": row["run_id"],
            "task_definition_id": row["task_definition_id"],
            "task_version": row["task_version"],
            "workcell_id": row["workcell_id"],
            "display_name": row["display_name"],
            "state": row["state"],
            "revision": row["revision"],
            "assignments": assignments,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "terminal_reason": row["terminal_reason"],
        }

    def get_task_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            return self._task_run_from_row(connection, row)

    def list_task_runs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_runs ORDER BY created_at DESC, run_id"
            ).fetchall()
            return [self._task_run_from_row(connection, row) for row in rows]

    def list_configuration_audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("audit limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM configuration_audit
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "occurred_at": row["occurred_at"],
                "action": row["action"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    def get_configuration(self) -> dict[str, Any]:
        return {
            "schema_versions": self.schema_versions(),
            "processing_nodes": self.list_processing_nodes(),
            "assets": self.list_assets(),
            "workcells": self.list_workcells(),
            "task_definitions": self.list_task_definitions(),
            "task_runs": self.list_task_runs(),
            "agent_registrations": self.list_agent_registrations(),
            "agent_credentials": self.list_agent_credentials(),
            "connection_profiles": self.list_connection_profiles(),
            "connection_observations": self.list_connection_observations(),
            "audit": self.list_configuration_audit(),
        }
