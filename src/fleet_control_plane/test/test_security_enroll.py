from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from fleet_control_plane.security_enroll import (
    enroll_phase3_edge,
    verify_role_metadata,
    verify_runtime_metadata,
)
from fleet_control_plane.store import FleetStore
from sang_security.pki import (
    initialize_pki,
    issue_certificate,
    provision_agent_runtime,
    provision_https_agent,
)


NOW = datetime.now(timezone.utc)


def test_phase3_edge_enrollment_creates_allowlist_and_two_profiles(tmp_path):
    store = FleetStore(tmp_path / "fleet.db")
    metadata = {
        "agent_id": "workcell-soarm-01",
        "edge": {
            "https_client_certificate_sha256": "a" * 64,
            "https_client_not_before": (NOW - timedelta(minutes=1)).isoformat(),
            "https_client_not_after": (NOW + timedelta(days=30)).isoformat(),
        },
    }
    arguments = {
        "metadata": metadata,
        "display_name": "SO-ARM Edge Laptop",
        "central_ingress_url": "https://central.sang.local:8443",
        "edge_opcua_endpoint": "opc.tcp://edge.sang.local:4840/sang/",
    }
    result = enroll_phase3_edge(store, **arguments)
    repeated = enroll_phase3_edge(store, **arguments)

    assert result["agent_registration"]["identity_uri"] == (
        "urn:sang:agent:workcell-soarm-01"
    )
    profiles = {item["protocol"]: item for item in result["connection_profiles"]}
    assert profiles["https_agent"]["security_mode"] == "MutualTLS"
    assert profiles["opcua"]["security_policy"] == "Basic256Sha256"
    assert profiles["opcua"]["server_certificate_ref"] == (
        "secret://opcua/pins/edge-cert.pem"
    )
    assert store.list_agent_credentials()[0]["certificate_sha256"] == "a" * 64
    assert repeated["agent_registration"] == result["agent_registration"]
    assert len(store.list_agent_registrations()) == 1
    assert len(store.list_agent_credentials()) == 1
    assert len(store.list_connection_profiles()) == 2


def test_processing_edge_enrollment_can_be_heartbeat_only(tmp_path):
    store = FleetStore(tmp_path / "fleet.db")
    metadata = {
        "agent_id": "processing-lab1",
        "edge": {
            "https_client_certificate_sha256": "c" * 64,
            "https_client_not_before": (NOW - timedelta(minutes=1)).isoformat(),
            "https_client_not_after": (NOW + timedelta(days=30)).isoformat(),
        },
    }

    result = enroll_phase3_edge(
        store,
        metadata=metadata,
        display_name="Lab1 Processing PC",
        central_ingress_url="https://central.sang.local:8443",
        edge_opcua_endpoint=None,
    )

    assert [profile["protocol"] for profile in result["connection_profiles"]] == [
        "https_agent"
    ]
    assert len(store.list_connection_profiles()) == 1


def test_enrollment_verifies_exported_agent_certificate_metadata(tmp_path):
    pki = tmp_path / "pki"
    initialize_pki(pki, key_size=2048)
    issued = issue_certificate(
        pki,
        issuer="https",
        name="soarm-agent",
        role="https-client",
        uri="urn:sang:agent:workcell-soarm-01",
    )
    role_root = tmp_path / "role"
    exported = role_root / "https/agents/workcell-soarm-01.pem"
    exported.parent.mkdir(parents=True)
    exported.write_bytes(Path(issued["certificate"]).read_bytes())
    metadata = {
        "agent_id": "workcell-soarm-01",
        "edge": {
            "https_client_certificate_sha256": issued["certificate_sha256"],
            "https_client_not_before": issued["not_before"],
            "https_client_not_after": issued["not_after"],
        },
    }

    verify_role_metadata(metadata, role_root)
    metadata["edge"]["https_client_certificate_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="fingerprint"):
        verify_role_metadata(metadata, role_root)


def test_provisioned_https_agent_role_enrolls_heartbeat_only(tmp_path):
    pki = tmp_path / "pki"
    initialize_pki(pki, key_size=2048)
    roles = provision_https_agent(pki, agent_id="sim-isaac-omy-01")
    role_root = Path(roles["central"])
    metadata = json.loads(
        (role_root / "role-metadata.json").read_text(encoding="utf-8")
    )
    store = FleetStore(tmp_path / "fleet.db")

    verify_role_metadata(metadata, role_root)
    result = enroll_phase3_edge(
        store,
        metadata=metadata,
        display_name="OMY Isaac Agent",
        central_ingress_url="https://central.sang.local:8443",
        edge_opcua_endpoint=None,
    )

    assert result["agent_registration"]["agent_id"] == "sim-isaac-omy-01"
    assert [profile["protocol"] for profile in result["connection_profiles"]] == [
        "https_agent"
    ]


def test_enrollment_verifies_agent_scoped_runtime_pin(tmp_path):
    pki = tmp_path / "pki"
    initialize_pki(pki, key_size=2048)
    roles = provision_agent_runtime(
        pki,
        agent_id="workcell-soarm-01",
        edge_host="192.0.2.20",
    )
    role_root = Path(roles["central"])
    metadata = json.loads(
        (role_root / "runtime-metadata.json").read_text(encoding="utf-8")
    )

    assert verify_runtime_metadata(metadata, role_root) == (
        "secret://opcua/pins/workcell-soarm-01.pem"
    )
    metadata["central"]["server_certificate_ref"] = (
        "secret://opcua/pins/edge-cert.pem"
    )
    with pytest.raises(ValueError, match="not Agent-scoped"):
        verify_runtime_metadata(metadata, role_root)
