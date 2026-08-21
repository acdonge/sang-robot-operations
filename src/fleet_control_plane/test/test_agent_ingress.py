from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from fleet_control_plane.agent_ingress import (
    AgentIngressServer,
    create_server_tls_context,
)
from fleet_control_plane.operations_models import (
    AgentCredentialCreate,
    AgentRegistrationCreate,
    ConnectionProfileCreate,
    CredentialRevoke,
)
from fleet_control_plane.store import FleetStore
from robot_agent.heartbeat import AgentTLSConfig, build_heartbeat, post_heartbeat
from sang_security.certificates import certificate_sha256
from sang_security.pki import initialize_pki, issue_certificate, revoke_certificate


AGENT_ID = "workcell-soarm-01"
BOOT_A = "019fce1c-a4b5-7ba2-b0d5-01ea4940926d"
BOOT_B = "019fce1c-a4b5-7ba2-b0d5-01ea4940926e"


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def heartbeat(
    clock,
    sequence,
    *,
    boot_id=BOOT_A,
    agent_id=AGENT_ID,
    schema_version=None,
):
    return build_heartbeat(
        agent_id=agent_id,
        agent_name="SO-ARM Edge",
        robots=[],
        storage={"cpu_pct": 1.0, "ram_free_gb": 2.0, "disk_free_gb": 3.0},
        sent_at=clock(),
        boot_id=boot_id,
        sequence=sequence,
        schema_version=schema_version,
    )


def setup_registry(store, clock, certificate, credential_id="soarm-http-01"):
    store.create_agent_registration(
        AgentRegistrationCreate(
            agent_id=AGENT_ID,
            display_name="SO-ARM Edge",
        ),
        occurred_at=clock(),
    )
    store.create_agent_credential(
        AgentCredentialCreate(
            credential_id=credential_id,
            agent_id=AGENT_ID,
            certificate_sha256=certificate_sha256(certificate),
            certificate_ref=f"secret://https/agents/{credential_id}.pem",
            not_before=clock() - timedelta(minutes=5),
            not_after=clock() + timedelta(days=30),
        ),
        occurred_at=clock(),
    )
    store.create_connection_profile(
        ConnectionProfileCreate(
            profile_id="soarm-heartbeat",
            owner_kind="agent",
            agent_id=AGENT_ID,
            protocol="https_agent",
            desired_endpoint="https://central.sang.local:8443",
            security_mode="MutualTLS",
            security_policy="TLS1.2+",
            client_certificate_ref="secret://https/agents/soarm-chain.pem",
            client_private_key_ref="secret://https/agents/soarm-key.pem",
            trust_list_ref="secret://pki/bundles/https-ca-crl.pem",
            crl_ref="secret://pki/bundles/https-ca-crl.pem",
        ),
        occurred_at=clock(),
    )


def issue_https_material(root):
    server = issue_certificate(
        root,
        issuer="https",
        name="central-server",
        role="https-server",
        dns_names=("localhost",),
        ip_addresses=("127.0.0.1",),
    )
    client = issue_certificate(
        root,
        issuer="https",
        name="soarm-agent",
        role="https-client",
        uri=f"urn:sang:agent:{AGENT_ID}",
    )
    return server, client


def start_ingress(store, clock, root, server_material, *, dynamic_tls=False):
    def context():
        return create_server_tls_context(
            certificate_chain=Path(server_material["chain"]),
            private_key=Path(server_material["private_key"]),
            client_ca_crl_bundle=root / "bundles" / "https-ca-crl.pem",
        )

    server = AgentIngressServer(
        ("127.0.0.1", 0),
        store=store,
        **(
            {"tls_context_factory": context}
            if dynamic_tls
            else {"tls_context": context()}
        ),
        now=clock,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"https://127.0.0.1:{server.server_address[1]}"


def client_tls(root, material):
    return AgentTLSConfig(
        ca_bundle=root / "bundles" / "https-ca-crl.pem",
        client_certificate_chain=Path(material["chain"]),
        client_private_key=Path(material["private_key"]),
    )


def stop_ingress(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5.0)
    assert not thread.is_alive()


def test_mtls_ingress_identity_replay_rotation_and_observation(tmp_path):
    clock = Clock()
    root = tmp_path / "pki"
    initialize_pki(root, key_size=2048)
    server_material, first_client = issue_https_material(root)
    store = FleetStore(tmp_path / "fleet.db")
    setup_registry(store, clock, first_client["certificate"])
    server, thread, url = start_ingress(store, clock, root, server_material)
    first_tls = client_tls(root, first_client)
    try:
        response = post_heartbeat(
            url,
            heartbeat(clock, 1),
            timeout_s=2.0,
            tls=first_tls,
        )
        assert response["accepted_sequence"] == 1
        with pytest.raises(HTTPError) as duplicate:
            post_heartbeat(
                url,
                heartbeat(clock, 1),
                timeout_s=2.0,
                tls=first_tls,
            )
        assert duplicate.value.code == 409

        assert post_heartbeat(
            url,
            heartbeat(clock, 2, schema_version=3),
            timeout_s=2.0,
            tls=first_tls,
        )["accepted_sequence"] == 2

        clock.advance(1)
        assert post_heartbeat(
            url,
            heartbeat(clock, 1, boot_id=BOOT_B),
            timeout_s=2.0,
            tls=first_tls,
        )["accepted_sequence"] == 1
        with pytest.raises(HTTPError) as retired:
            post_heartbeat(
                url,
                heartbeat(clock, 3, boot_id=BOOT_A),
                timeout_s=2.0,
                tls=first_tls,
            )
        assert retired.value.code == 409

        observation = store.list_connection_observations()[0]
        assert observation["observed_application_uri"] == f"urn:sang:agent:{AGENT_ID}"
        assert observation["observed_endpoint"].startswith("https://127.0.0.1:")
        assert observation["observed_endpoint"] != "https://central.sang.local:8443"
        assert observation["peer_certificate_sha256"] == certificate_sha256(
            first_client["certificate"]
        )

        second_client = issue_certificate(
            root,
            issuer="https",
            name="soarm-agent-rotated",
            role="https-client",
            uri=f"urn:sang:agent:{AGENT_ID}",
        )
        store.create_agent_credential(
            AgentCredentialCreate(
                credential_id="soarm-http-02",
                agent_id=AGENT_ID,
                certificate_sha256=certificate_sha256(second_client["certificate"]),
                certificate_ref="secret://https/agents/soarm-http-02.pem",
                not_before=clock() - timedelta(minutes=5),
                not_after=clock() + timedelta(days=30),
            ),
            occurred_at=clock(),
        )
        store.revoke_agent_credential(
            "soarm-http-01",
            CredentialRevoke(reason="rotation complete"),
            occurred_at=clock(),
        )
        with pytest.raises(HTTPError) as revoked:
            post_heartbeat(
                url,
                heartbeat(clock, 2, boot_id=BOOT_B),
                timeout_s=2.0,
                tls=first_tls,
            )
        assert revoked.value.code == 403
        assert post_heartbeat(
            url,
            heartbeat(clock, 2, boot_id=BOOT_B),
            timeout_s=2.0,
            tls=client_tls(root, second_client),
        )["accepted_sequence"] == 2
    finally:
        stop_ingress(server, thread)


def test_mtls_ingress_rejects_missing_cert_and_payload_identity_mismatch(tmp_path):
    clock = Clock()
    root = tmp_path / "pki"
    initialize_pki(root, key_size=2048)
    server_material, client_material = issue_https_material(root)
    store = FleetStore(tmp_path / "fleet.db")
    setup_registry(store, clock, client_material["certificate"])
    server, thread, url = start_ingress(store, clock, root, server_material)
    try:
        context = ssl.create_default_context(
            cafile=str(root / "bundles" / "https-ca.pem")
        )
        with pytest.raises((URLError, ssl.SSLError, ConnectionError)):
            urlopen(url + "/healthz", timeout=2.0, context=context)

        payload = heartbeat(clock, 1, agent_id="different-agent")
        request = Request(
            url + "/api/v1/agents/heartbeat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as mismatch:
            urlopen(
                request,
                timeout=2.0,
                context=client_tls(root, client_material).context(),
            )
        assert mismatch.value.code == 403

        malformed = heartbeat(clock, 1)
        malformed["sequence"] = 0
        request = Request(
            url + "/api/v1/agents/heartbeat",
            data=json.dumps(malformed).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as invalid:
            urlopen(
                request,
                timeout=2.0,
                context=client_tls(root, client_material).context(),
            )
        assert invalid.value.code == 422
        assert json.loads(invalid.value.read())["error"] == (
            "heartbeat schema validation failed"
        )
    finally:
        stop_ingress(server, thread)


def test_mtls_ingress_requires_enabled_connection_profile(tmp_path):
    clock = Clock()
    root = tmp_path / "pki"
    initialize_pki(root, key_size=2048)
    server_material, client_material = issue_https_material(root)
    store = FleetStore(tmp_path / "fleet.db")
    setup_registry(store, clock, client_material["certificate"])
    with store._connect() as connection:
        connection.execute(
            "UPDATE connection_profiles SET enabled = 0 WHERE profile_id = ?",
            ("soarm-heartbeat",),
        )
    server, thread, url = start_ingress(store, clock, root, server_material)
    try:
        with pytest.raises(HTTPError) as disabled:
            post_heartbeat(
                url,
                heartbeat(clock, 1),
                timeout_s=2.0,
                tls=client_tls(root, client_material),
            )
        assert disabled.value.code == 403
    finally:
        stop_ingress(server, thread)


def test_mtls_ingress_reloads_crl_and_rejects_wrong_issuing_ca(tmp_path):
    clock = Clock()
    root = tmp_path / "pki"
    initialize_pki(root, key_size=2048)
    server_material, client_material = issue_https_material(root)
    store = FleetStore(tmp_path / "fleet.db")
    setup_registry(store, clock, client_material["certificate"])
    server, thread, url = start_ingress(
        store,
        clock,
        root,
        server_material,
        dynamic_tls=True,
    )
    try:
        assert post_heartbeat(
            url,
            heartbeat(clock, 1),
            timeout_s=2.0,
            tls=client_tls(root, client_material),
        )["accepted_sequence"] == 1

        revoke_certificate(
            root,
            certificate_path=client_material["certificate"],
            reason="test revocation",
        )
        with pytest.raises((URLError, ssl.SSLError, ConnectionError)):
            post_heartbeat(
                url,
                heartbeat(clock, 2),
                timeout_s=2.0,
                tls=client_tls(root, client_material),
            )

        opcua_client = issue_certificate(
            root,
            issuer="opcua",
            name="wrong-domain-client",
            role="opcua-client",
            uri=f"urn:sang:agent:{AGENT_ID}",
        )
        with pytest.raises((URLError, ssl.SSLError, ConnectionError)):
            post_heartbeat(
                url,
                heartbeat(clock, 2),
                timeout_s=2.0,
                tls=client_tls(root, opcua_client),
            )
    finally:
        stop_ingress(server, thread)


def test_idle_tls_client_does_not_block_other_agents(tmp_path):
    clock = Clock()
    root = tmp_path / "pki"
    initialize_pki(root, key_size=2048)
    server_material, client_material = issue_https_material(root)
    store = FleetStore(tmp_path / "fleet.db")
    setup_registry(store, clock, client_material["certificate"])
    server, thread, url = start_ingress(store, clock, root, server_material)
    stalled = socket.create_connection(server.server_address, timeout=1.0)
    try:
        started = time.monotonic()
        response = post_heartbeat(
            url,
            heartbeat(clock, 1),
            timeout_s=2.0,
            tls=client_tls(root, client_material),
        )
        assert response["accepted_sequence"] == 1
        assert time.monotonic() - started < 1.0
    finally:
        stalled.close()
        stop_ingress(server, thread)


def test_mtls_ingress_rejects_stale_and_future_clock_skew(tmp_path):
    clock = Clock()
    root = tmp_path / "pki"
    initialize_pki(root, key_size=2048)
    server_material, client_material = issue_https_material(root)
    store = FleetStore(tmp_path / "fleet.db")
    setup_registry(store, clock, client_material["certificate"])
    server, thread, url = start_ingress(store, clock, root, server_material)
    tls = client_tls(root, client_material)
    try:
        stale = heartbeat(clock, 1)
        stale["sent_at"] = (clock() - timedelta(seconds=12.001)).isoformat()
        with pytest.raises(HTTPError) as old:
            post_heartbeat(url, stale, timeout_s=2.0, tls=tls)
        assert old.value.code == 409
        assert "maximum accepted age" in json.loads(old.value.read())["error"]

        future = heartbeat(clock, 1)
        future["sent_at"] = (clock() + timedelta(seconds=2.001)).isoformat()
        with pytest.raises(HTTPError) as ahead:
            post_heartbeat(url, future, timeout_s=2.0, tls=tls)
        assert ahead.value.code == 409
        assert "future" in json.loads(ahead.value.read())["error"]

        assert post_heartbeat(
            url,
            heartbeat(clock, 1),
            timeout_s=2.0,
            tls=tls,
        )["accepted_sequence"] == 1
    finally:
        stop_ingress(server, thread)
