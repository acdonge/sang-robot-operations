import json
from pathlib import Path

from fleet_control_plane.operations_models import CredentialRevoke
from fleet_control_plane.security_enroll import enroll_phase3_edge
from fleet_control_plane.security_rotate import prepare_agent_https_rotation
from fleet_control_plane.status import utc_now
from fleet_control_plane.store import FleetStore
from sang_security.certificates import certificate_sha256
from sang_security.pki import bootstrap_lab, revoke_certificate


def test_agent_https_rotation_is_make_before_break_and_idempotent(tmp_path):
    agent_id = "workcell-soarm-01"
    pki = tmp_path / "pki"
    bootstrapped = bootstrap_lab(
        pki,
        central_host="central.sang.local",
        central_ip="127.0.0.1",
        edge_host="edge.sang.local",
        edge_ip="127.0.0.1",
        agent_id=agent_id,
        key_size=2048,
    )
    central_role = Path(bootstrapped["roles"]["central"])
    edge_role = Path(bootstrapped["roles"]["edge"])
    metadata = json.loads(
        (central_role / "role-metadata.json").read_text(encoding="utf-8")
    )
    store = FleetStore(tmp_path / "fleet.db")
    enroll_phase3_edge(
        store,
        metadata=metadata,
        display_name="SO-ARM Edge",
        central_ingress_url="https://127.0.0.1:8443",
        edge_opcua_endpoint="opc.tcp://127.0.0.1:4840/sang/",
    )
    old_certificate = bootstrapped["certificates"]["agent_https_client"]
    health_key = (edge_role / "local/health-hmac.key").read_bytes()

    rotated = prepare_agent_https_rotation(
        store,
        pki_root=pki,
        agent_id=agent_id,
        certificate_name=f"{agent_id}-https-client-02",
        credential_id=f"{agent_id}-https-02",
    )
    repeated = prepare_agent_https_rotation(
        store,
        pki_root=pki,
        agent_id=agent_id,
        certificate_name=f"{agent_id}-https-client-02",
        credential_id=f"{agent_id}-https-02",
    )

    assert len(store.list_agent_credentials()) == 2
    assert all(item["status"] == "active" for item in store.list_agent_credentials())
    assert rotated["activation"]["previous_certificate"] == old_certificate[
        "certificate"
    ]
    assert rotated["previous_active_credential_ids"] == [f"{agent_id}-https-01"]
    assert repeated["issued"]["certificate_sha256"] == rotated["issued"][
        "certificate_sha256"
    ]
    assert certificate_sha256(
        central_role / f"https/agents/{agent_id}.pem"
    ) == rotated["issued"]["certificate_sha256"]
    assert certificate_sha256(
        edge_role / "https/client/chain.pem"
    ) == rotated["issued"]["certificate_sha256"]
    assert (edge_role / "local/health-hmac.key").read_bytes() == health_key

    resumed = bootstrap_lab(
        pki,
        central_host="central.sang.local",
        central_ip="127.0.0.1",
        edge_host="edge.sang.local",
        edge_ip="127.0.0.1",
        agent_id=agent_id,
        key_size=2048,
    )
    assert resumed["roles"] == bootstrapped["roles"]
    assert certificate_sha256(
        central_role / f"https/agents/{agent_id}.pem"
    ) == rotated["issued"]["certificate_sha256"]
    assert certificate_sha256(
        edge_role / "https/client/chain.pem"
    ) == rotated["issued"]["certificate_sha256"]
    assert (edge_role / "local/health-hmac.key").read_bytes() == health_key

    revoke_certificate(
        pki,
        certificate_path=old_certificate["certificate"],
        reason="rotation complete",
    )
    store.revoke_agent_credential(
        f"{agent_id}-https-01",
        CredentialRevoke(reason="rotation complete"),
        occurred_at=utc_now(),
    )
    credentials = {
        item["credential_id"]: item for item in store.list_agent_credentials()
    }
    assert credentials[f"{agent_id}-https-01"]["status"] == "revoked"
    assert credentials[f"{agent_id}-https-02"]["status"] == "active"
