from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
import pytest

from sang_security.certificates import (
    certificate_sha256,
    certificate_uri_sans,
    load_certificate,
    require_identity_uri,
)
from sang_security.pki import (
    PkiError,
    bootstrap_lab,
    initialize_pki,
    issue_certificate,
    provision_agent_runtime,
    provision_https_agent,
    refresh_crls,
    revoke_certificate,
)
from sang_security.opcua import OpcUaSecurityError, _validated_crl_expirations
from sang_security.refs import SecretReferenceError, SecretResolver


NOW = datetime(2026, 8, 4, 21, 30, tzinfo=timezone.utc)


def test_secret_resolver_rejects_escape_and_weak_private_permissions(tmp_path):
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    private = root / "agent" / "key.pem"
    private.parent.mkdir()
    private.write_text("private", encoding="utf-8")
    private.chmod(0o600)
    resolver = SecretResolver(root)

    assert resolver.resolve("secret://agent/key.pem", private=True) == private
    with pytest.raises(SecretReferenceError):
        resolver.resolve("secret://agent/../key.pem")

    private.chmod(0o644)
    with pytest.raises(SecretReferenceError, match="permissions"):
        resolver.resolve("secret://agent/key.pem", private=True)

    outside = tmp_path / "outside.pem"
    outside.write_text("outside", encoding="utf-8")
    (root / "escape.pem").symlink_to(outside)
    with pytest.raises(SecretReferenceError, match="escapes"):
        resolver.resolve("secret://escape.pem")


def test_pki_issue_rotation_and_revocation_crl(tmp_path):
    root = tmp_path / "pki"
    initialized = initialize_pki(root, now=NOW, key_size=2048)
    assert initialized["issuers"] == ["https", "opcua"]
    assert (root / "root" / "ca-key.pem").stat().st_mode & 0o777 == 0o600
    assert (root / "issuers" / "opcua" / "ca-key.pem").stat().st_mode & 0o777 == 0o600
    crl_checked_at = datetime.now(timezone.utc)
    expirations = _validated_crl_expirations(
        [root / "trust" / "opcua"],
        [root / "crl" / "opcua"],
        now=crl_checked_at,
    )
    assert len(expirations) == 1
    with pytest.raises(OpcUaSecurityError, match="expired"):
        _validated_crl_expirations(
            [root / "trust" / "opcua"],
            [root / "crl" / "opcua"],
            now=crl_checked_at + timedelta(days=31),
        )

    first = issue_certificate(
        root,
        issuer="opcua",
        name="soarm-edge-2026-01",
        role="opcua-server",
        uri="urn:sang:agent:workcell-soarm-01",
        dns_names=("soarm-edge.sang.local",),
        ip_addresses=("192.0.2.20",),
        now=NOW,
    )
    second = issue_certificate(
        root,
        issuer="opcua",
        name="soarm-edge-2026-02",
        role="opcua-server",
        uri="urn:sang:agent:workcell-soarm-01",
        dns_names=("soarm-edge.sang.local",),
        now=NOW + timedelta(seconds=1),
    )
    assert first["certificate_sha256"] != second["certificate_sha256"]

    certificate = load_certificate(first["certificate"])
    assert certificate_uri_sans(certificate) == (
        "urn:sang:agent:workcell-soarm-01",
    )
    require_identity_uri(certificate, "urn:sang:agent:workcell-soarm-01")
    eku = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku
    assert certificate_sha256(certificate) == first["certificate_sha256"]
    assert os.stat(first["private_key"]).st_mode & 0o777 == 0o600

    revoked = revoke_certificate(
        root,
        certificate_path=first["certificate"],
        reason="rotation complete",
        now=NOW + timedelta(minutes=1),
    )
    assert revoked["status"] == "revoked"
    crl = x509.load_pem_x509_crl(
        (root / "crl" / "opcua" / "opcua-crl.pem").read_bytes()
    )
    assert {item.serial_number for item in crl} == {certificate.serial_number}
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert state["issued"][str(certificate.serial_number)]["status"] == "revoked"

    repeated = revoke_certificate(
        root,
        certificate_path=first["certificate"],
        reason="again",
        now=NOW + timedelta(minutes=2),
    )
    assert repeated["revoked_at"] == revoked["revoked_at"]
    refreshed = refresh_crls(root, issuer="opcua")
    assert refreshed["validity_days"] == 30
    with pytest.raises(PkiError, match="absent or empty"):
        initialize_pki(root, now=NOW, key_size=2048)


def test_issuer_role_and_required_identity_are_fail_closed(tmp_path):
    root = tmp_path / "pki"
    initialize_pki(root, now=NOW, key_size=2048)

    with pytest.raises(PkiError, match="does not match"):
        issue_certificate(
            root,
            issuer="https",
            name="bad-opcua-server",
            role="opcua-server",
            uri="urn:sang:agent:bad",
            now=NOW,
        )
    with pytest.raises(PkiError, match="require"):
        issue_certificate(
            root,
            issuer="https",
            name="agent-without-uri",
            role="https-client",
            now=NOW,
        )


def test_https_chain_contains_leaf_and_intermediate(tmp_path):
    root = tmp_path / "pki"
    initialize_pki(root, now=NOW, key_size=2048)
    issued = issue_certificate(
        root,
        issuer="https",
        name="central-server",
        role="https-server",
        dns_names=("central.sang.local",),
        ip_addresses=("127.0.0.1",),
        now=NOW,
    )

    blocks = Path(issued["chain"]).read_text(encoding="utf-8").count(
        "-----BEGIN CERTIFICATE-----"
    )
    assert blocks == 2
    assert not (root / "trust" / "https" / "root-ca.pem").exists()
    assert (root / "bundles" / "https-ca.pem").read_text(
        encoding="utf-8"
    ).count("-----BEGIN CERTIFICATE-----") == 1
    key = serialization.load_pem_private_key(
        Path(issued["private_key"]).read_bytes(), password=None
    )
    assert key.key_size == 2048


def test_bootstrap_exports_only_role_specific_runtime_material(tmp_path):
    root = tmp_path / "pki"
    result = bootstrap_lab(
        root,
        central_host="central.sang.local",
        central_ip="127.0.0.1",
        edge_host="edge.sang.local",
        edge_ip="127.0.0.1",
        agent_id="workcell-soarm-01",
        key_size=2048,
    )
    central = Path(result["roles"]["central"])
    edge = Path(result["roles"]["edge"])

    assert (central / "https/server/key.pem").stat().st_mode & 0o777 == 0o600
    assert (edge / "https/client/key.pem").stat().st_mode & 0o777 == 0o600
    assert (edge / "local/health-hmac.key").stat().st_mode & 0o777 == 0o600
    assert not list(central.rglob("ca-key.pem"))
    assert not list(edge.rglob("ca-key.pem"))
    assert not (central / "opcua/server/key.pem").exists()
    assert not (edge / "opcua/application/key.pem").exists()
    assert result["roles"]["health_key_id"] == (
        "workcell-soarm-01-health-01"
    )
    health_key = (edge / "local/health-hmac.key").read_bytes()

    resumed = bootstrap_lab(
        root,
        central_host="central.sang.local",
        central_ip="127.0.0.1",
        edge_host="edge.sang.local",
        edge_ip="127.0.0.1",
        agent_id="workcell-soarm-01",
        key_size=2048,
    )

    assert resumed["certificates"]["agent_https_client"][
        "certificate_sha256"
    ] == result["certificates"]["agent_https_client"]["certificate_sha256"]
    assert (edge / "local/health-hmac.key").read_bytes() == health_key


def test_issue_recovers_complete_output_missing_from_state(tmp_path):
    root = tmp_path / "pki"
    initialize_pki(root, key_size=2048)
    issued = issue_certificate(
        root,
        issuer="https",
        name="recoverable-agent",
        role="https-client",
        uri="urn:sang:agent:recoverable-agent",
    )
    output = Path(issued["certificate"]).parent
    (output / "metadata.json").unlink()
    state_path = root / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["issued"] = {}
    state_path.write_text(json.dumps(state), encoding="utf-8")

    recovered = issue_certificate(
        root,
        issuer="https",
        name="recoverable-agent",
        role="https-client",
        uri="urn:sang:agent:recoverable-agent",
    )

    assert recovered["certificate_sha256"] == issued["certificate_sha256"]
    assert (output / "metadata.json").is_file()
    restored = json.loads(state_path.read_text(encoding="utf-8"))
    assert recovered["serial_number"] in restored["issued"]


def test_multiple_https_agents_have_independent_idempotent_role_exports(tmp_path):
    root = tmp_path / "pki"
    initialize_pki(root, key_size=2048)

    first = provision_https_agent(root, agent_id="processing-lab1")
    second = provision_https_agent(root, agent_id="sim-isaac-omy-01")
    repeated = provision_https_agent(root, agent_id="sim-isaac-omy-01")

    assert first["certificate_sha256"] != second["certificate_sha256"]
    assert repeated == second
    first_edge = Path(first["edge"])
    second_edge = Path(second["edge"])
    assert (first_edge / "https/client/key.pem").read_bytes() != (
        second_edge / "https/client/key.pem"
    ).read_bytes()
    assert not list((root / "roles/agents").rglob("ca-key.pem"))
    assert (second_edge / "https/client/key.pem").stat().st_mode & 0o777 == 0o600

    old_trust = (second_edge / "https/trust/ca-crl.pem").read_bytes()
    revoke_certificate(
        root,
        certificate_path=(
            root / "issued/https/processing-lab1-https-client/cert.pem"
        ),
        reason="test isolated Agent revocation",
        now=NOW,
    )
    new_trust = (second_edge / "https/trust/ca-crl.pem").read_bytes()
    assert old_trust != new_trust


def test_https_agent_role_export_fails_closed_when_partial(tmp_path):
    root = tmp_path / "pki"
    initialize_pki(root, key_size=2048)
    partial = root / "roles/agents/sim-isaac-omy-01/edge"
    partial.mkdir(parents=True)
    (partial / "unexpected.txt").write_text("partial", encoding="utf-8")

    with pytest.raises(PkiError, match="partially present"):
        provision_https_agent(root, agent_id="sim-isaac-omy-01")


def test_concurrent_https_agent_provisioning_is_serialized(tmp_path):
    root = tmp_path / "pki"
    initialize_pki(root, key_size=2048)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: provision_https_agent(
                    root,
                    agent_id="sim-isaac-omy-01",
                ),
                range(2),
            )
        )

    assert results[0] == results[1]
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    issued = [
        item
        for item in state["issued"].values()
        if item.get("uri") == "urn:sang:agent:sim-isaac-omy-01"
    ]
    assert len(issued) == 1
    assert (root / ".provision-agent.lock").stat().st_mode & 0o777 == 0o600


def test_agent_runtimes_have_independent_opcua_and_health_material(tmp_path):
    root = tmp_path / "pki"
    initialize_pki(root, key_size=2048)

    first = provision_agent_runtime(
        root,
        agent_id="workcell-soarm-01",
        edge_host="192.0.2.20",
    )
    second = provision_agent_runtime(
        root,
        agent_id="sim-isaac-omy-01",
        edge_host="sim-edge.sang.local",
    )
    repeated = provision_agent_runtime(
        root,
        agent_id="workcell-soarm-01",
        edge_host="192.0.2.20",
    )

    assert repeated == first
    assert first["certificate_sha256"] != second["certificate_sha256"]
    assert first["server_certificate_ref"] == (
        "secret://opcua/pins/workcell-soarm-01.pem"
    )
    first_edge = Path(first["edge"])
    second_edge = Path(second["edge"])
    assert (first_edge / "opcua/server/key.pem").read_bytes() != (
        second_edge / "opcua/server/key.pem"
    ).read_bytes()
    assert (first_edge / "local/health-hmac.key").read_bytes() != (
        second_edge / "local/health-hmac.key"
    ).read_bytes()
    assert (first_edge / "opcua/server/key.pem").stat().st_mode & 0o777 == 0o600
    assert (first_edge / "local/health-hmac.key").stat().st_mode & 0o777 == 0o600
    assert not list((root / "roles/agents").rglob("ca-key.pem"))
    assert not (first_edge / "opcua/application/key.pem").exists()
    assert (
        root / "roles/central/opcua/pins/workcell-soarm-01.pem"
    ).read_bytes() == (first_edge / "opcua/server/cert.pem").read_bytes()


def test_agent_runtime_repairs_one_missing_metadata_copy(tmp_path):
    root = tmp_path / "pki"
    initialize_pki(root, key_size=2048)
    result = provision_agent_runtime(
        root,
        agent_id="workcell-soarm-01",
        edge_host="soarm-edge.sang.local",
    )
    central_metadata = Path(result["central"]) / "runtime-metadata.json"
    expected = central_metadata.read_bytes()
    central_metadata.unlink()

    resumed = provision_agent_runtime(
        root,
        agent_id="workcell-soarm-01",
        edge_host="soarm-edge.sang.local",
    )

    assert resumed == result
    assert central_metadata.read_bytes() == expected


def test_agent_runtime_rejects_endpoint_identity_drift(tmp_path):
    root = tmp_path / "pki"
    initialize_pki(root, key_size=2048)
    provision_agent_runtime(
        root,
        agent_id="workcell-soarm-01",
        edge_host="192.0.2.20",
    )

    with pytest.raises(PkiError, match="differs from the request"):
        provision_agent_runtime(
            root,
            agent_id="workcell-soarm-01",
            edge_host="192.0.2.21",
        )


def test_agent_runtime_rejects_non_scoped_server_pin_metadata(tmp_path):
    root = tmp_path / "pki"
    initialize_pki(root, key_size=2048)
    result = provision_agent_runtime(
        root,
        agent_id="workcell-soarm-01",
        edge_host="192.0.2.20",
    )
    for role in (Path(result["central"]), Path(result["edge"])):
        path = role / "runtime-metadata.json"
        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata["central"]["server_certificate_ref"] = (
            "secret://opcua/pins/edge-cert.pem"
        )
        path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(PkiError, match="differs from the request"):
        provision_agent_runtime(
            root,
            agent_id="workcell-soarm-01",
            edge_host="192.0.2.20",
        )
