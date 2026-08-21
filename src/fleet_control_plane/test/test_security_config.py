from datetime import datetime, timedelta, timezone
import sqlite3

from fastapi.testclient import TestClient
import pytest

from fleet_control_plane.app import create_app
from fleet_control_plane.store import FleetStore


NOW = datetime(2026, 8, 4, 21, 0, tzinfo=timezone.utc)
FINGERPRINT = "a" * 64


def make_client(tmp_path):
    store = FleetStore(tmp_path / "fleet.db")
    app = create_app(store, now=lambda: NOW, websocket_interval_s=0.01)
    return TestClient(app), store


def register_agent(client):
    response = client.post(
        "/api/v1/config/agent-registrations",
        json={
            "agent_id": "workcell-soarm-01",
            "display_name": "SO-ARM Edge",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def opcua_profile():
    return {
        "profile_id": "soarm-opcua",
        "owner_kind": "agent",
        "agent_id": "workcell-soarm-01",
        "asset_id": None,
        "protocol": "opcua",
        "desired_endpoint": "opc.tcp://soarm-edge.sang.local:4840/sang/",
        "expected_application_uri": "urn:sang:agent:workcell-soarm-01",
        "security_mode": "SignAndEncrypt",
        "security_policy": "Basic256Sha256",
        "client_certificate_ref": "secret://opcua/central/application-cert.pem",
        "client_private_key_ref": "secret://opcua/central/application-key.pem",
        "trust_list_ref": "secret://opcua/trust/edge",
        "crl_ref": "secret://opcua/crl",
        "user_certificate_ref": "secret://opcua/central/user-cert.pem",
        "user_private_key_ref": "secret://opcua/central/user-key.pem",
        "enabled": True,
    }


def test_phase3_migration_is_additive_and_preserves_existing_rows(tmp_path):
    path = tmp_path / "fleet.db"
    store = FleetStore(path)
    with store._connect() as connection:
        connection.execute(
            """
            INSERT INTO agents (
                agent_id, agent_name, sw_version, sent_at, received_at, payload_json
            ) VALUES ('legacy-agent', 'Legacy', '0.1', ?, ?, '{}')
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
        for table in (
            "agent_boot_history",
            "agent_replay_state",
            "connection_observations",
            "connection_profiles",
            "agent_credentials",
            "agent_registrations",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 2")

    migrated = FleetStore(path)

    assert migrated.schema_versions() == [1, 2, 3, 4]
    with migrated._connect() as connection:
        assert connection.execute(
            "SELECT agent_name FROM agents WHERE agent_id = 'legacy-agent'"
        ).fetchone()["agent_name"] == "Legacy"
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'connection_profiles'"
        ).fetchone() is not None
        assert "runtime_kind" in {
            row["name"]
            for row in connection.execute("PRAGMA table_info(assets)").fetchall()
        }


def test_phase3_migration_resumes_after_partial_ddl(tmp_path):
    path = tmp_path / "fleet.db"
    store = FleetStore(path)
    with store._connect() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version >= 2")
        for table in (
            "agent_boot_history",
            "agent_replay_state",
            "connection_observations",
            "connection_profiles",
            "agent_credentials",
        ):
            connection.execute(f"DROP TABLE {table}")

    resumed = FleetStore(path)

    assert resumed.schema_versions() == [1, 2, 3, 4]
    assert resumed.list_agent_registrations() == []


def test_phase4_pin_migration_preserves_profiles_and_observations(tmp_path):
    client, store = make_client(tmp_path)
    register_agent(client)
    created = client.post(
        "/api/v1/config/connection-profiles",
        json=opcua_profile(),
    )
    assert created.status_code == 201, created.text
    store.record_connection_observation(
        "soarm-opcua",
        observed_at=NOW,
        observed_endpoint=created.json()["desired_endpoint"],
        observed_application_uri="urn:sang:agent:workcell-soarm-01",
        peer_certificate_sha256="b" * 64,
        peer_address="192.0.2.20:4840",
        namespace_uris=["urn:sang:fleet:model:1"],
        succeeded=True,
    )

    with store._connect() as connection:
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE connection_profiles_v3 AS
            SELECT
                profile_id, owner_kind, agent_id, asset_id, protocol,
                desired_endpoint, expected_application_uri, security_mode,
                security_policy, client_certificate_ref, client_private_key_ref,
                trust_list_ref, crl_ref, user_certificate_ref,
                user_private_key_ref, enabled, revision, created_at, updated_at
            FROM connection_profiles
            """
        )
        connection.execute("DROP TABLE connection_profiles")
        connection.execute(
            "ALTER TABLE connection_profiles_v3 RENAME TO connection_profiles"
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 4")
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")

    migrated = FleetStore(store.path)

    assert migrated.schema_versions() == [1, 2, 3, 4]
    profile = migrated.get_agent_connection_profile("workcell-soarm-01", "opcua")
    assert profile["server_certificate_ref"] == "secret://opcua/pins/edge-cert.pem"
    assert migrated.list_connection_observations()[0]["profile_id"] == "soarm-opcua"
    with migrated._connect() as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'connection_profiles'"
        ).fetchone()["sql"]
        assert "server_certificate_ref IS NOT NULL" in table_sql
        assert "server_certificate_ref IS NULL" in table_sql
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE connection_profiles SET server_certificate_ref = NULL "
                "WHERE profile_id = 'soarm-opcua'"
            )


def test_agent_allowlist_credential_rotation_and_profile_revision(tmp_path):
    client, store = make_client(tmp_path)
    registration = register_agent(client)
    assert registration["identity_uri"] == "urn:sang:agent:workcell-soarm-01"
    assert registration["revision"] == 1

    credential = client.post(
        "/api/v1/config/agent-credentials",
        json={
            "credential_id": "soarm-http-2026-01",
            "agent_id": "workcell-soarm-01",
            "certificate_sha256": FINGERPRINT,
            "certificate_ref": "secret://https/agents/soarm-cert.pem",
            "not_before": (NOW - timedelta(minutes=1)).isoformat(),
            "not_after": (NOW + timedelta(days=30)).isoformat(),
        },
    )
    assert credential.status_code == 201, credential.text
    assert credential.json()["status"] == "active"

    created = client.post(
        "/api/v1/config/connection-profiles",
        json=opcua_profile(),
    )
    assert created.status_code == 201, created.text
    assert created.json()["revision"] == 1

    replacement = opcua_profile()
    replacement.pop("profile_id")
    replacement.pop("owner_kind")
    replacement.pop("agent_id")
    replacement.pop("asset_id")
    replacement["expected_revision"] = 1
    replacement["desired_endpoint"] = "opc.tcp://soarm-edge.sang.local:4841/sang/"
    replaced = client.put(
        "/api/v1/config/connection-profiles/soarm-opcua",
        json=replacement,
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["revision"] == 2

    stale = client.put(
        "/api/v1/config/connection-profiles/soarm-opcua",
        json=replacement,
    )
    assert stale.status_code == 409

    observation = store.record_connection_observation(
        "soarm-opcua",
        observed_at=NOW,
        observed_endpoint=replaced.json()["desired_endpoint"],
        observed_application_uri="urn:sang:agent:workcell-soarm-01",
        peer_certificate_sha256="b" * 64,
        peer_address="192.0.2.20:4841",
        namespace_uris=["urn:sang:fleet:model:1", "urn:sang:fleet:edge:1"],
        succeeded=True,
    )
    assert observation["last_error"] is None

    revoked = client.post(
        "/api/v1/config/agent-credentials/soarm-http-2026-01/revoke",
        json={"reason": "rotation complete"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    security = client.get("/api/v1/config/security").json()
    assert security["schema_versions"] == [1, 2, 3, 4]
    assert security["agent_registrations"][0]["enabled"] is True
    assert security["agent_credentials"][0]["status"] == "revoked"
    assert security["connection_profiles"][0]["revision"] == 2
    assert security["connection_observations"][0]["last_success_at"] is not None
    assert [item["action"] for item in store.list_configuration_audit()] == [
        "revoke",
        "replace",
        "create",
        "enroll",
        "create",
    ]


def test_security_contract_rejects_unsafe_or_inconsistent_profiles(tmp_path):
    client, _ = make_client(tmp_path)
    register_agent(client)

    invalid_ref = opcua_profile()
    invalid_ref["client_private_key_ref"] = "secret://opcua/../private-key.pem"
    assert client.post(
        "/api/v1/config/connection-profiles", json=invalid_ref
    ).status_code == 422

    insecure = opcua_profile()
    insecure["security_mode"] = "MutualTLS"
    assert client.post(
        "/api/v1/config/connection-profiles", json=insecure
    ).status_code == 422

    wrong_scheme = opcua_profile()
    wrong_scheme["desired_endpoint"] = "http://soarm-edge.sang.local:4840"
    assert client.post(
        "/api/v1/config/connection-profiles", json=wrong_scheme
    ).status_code == 422

    credential = {
        "credential_id": "bad-window",
        "agent_id": "workcell-soarm-01",
        "certificate_sha256": FINGERPRINT,
        "certificate_ref": "secret://https/agents/soarm-cert.pem",
        "not_before": NOW.isoformat(),
        "not_after": (NOW - timedelta(seconds=1)).isoformat(),
    }
    assert client.post(
        "/api/v1/config/agent-credentials", json=credential
    ).status_code == 422


def test_agent_registration_disable_uses_optimistic_revision(tmp_path):
    client, _ = make_client(tmp_path)
    register_agent(client)

    updated = client.put(
        "/api/v1/config/agent-registrations/workcell-soarm-01",
        json={
            "expected_revision": 1,
            "display_name": "SO-ARM Edge",
            "enabled": False,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False
    assert updated.json()["revision"] == 2

    stale = client.put(
        "/api/v1/config/agent-registrations/workcell-soarm-01",
        json={
            "expected_revision": 1,
            "display_name": "SO-ARM Edge",
            "enabled": True,
        },
    )
    assert stale.status_code == 409
