from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import pytest

import fleet_control_plane.runtime_deploy as runtime_deploy
from fleet_control_plane.runtime_deploy import (
    RuntimeDeploymentError,
    _write_deployment_index,
    generate_runtime_deployments,
    parse_runtime_inventory,
    resolve_runtime_inventory,
)
from fleet_control_plane.security_enroll import enroll_phase3_edge
from fleet_control_plane.operations_models import CredentialRevoke
from fleet_control_plane.store import FleetStore
from sang_security.pki import PkiError, initialize_pki, provision_https_agent


WORKSPACE = Path(__file__).resolve().parents[3]


def runtime_entry(**overrides):
    value = {
        "agent_id": "workcell-soarm-01",
        "agent_name": "Sang Dual SO-101",
        "robot_id": "bi_so101_01",
        "processing_node_id": "processing-soarm-01",
        "endpoint_host": "192.0.2.20",
        "opcua_port": 4840,
        "preview_port": 8101,
        "ros_domain_id": 42,
        "manifest": "manifests/bi_so101_01.yaml",
        "mapping": "contracts/opcua/bi_so101_01.mapping.v2.yaml",
        "runtime_directory": "agents/workcell-soarm-01",
    }
    value.update(overrides)
    return value


def inventory(*runtimes):
    return {
        "schema_version": "1.0.0",
        "central": {"ingress_url": "https://192.0.2.10:8443"},
        "runtimes": list(runtimes or (runtime_entry(),)),
    }


def workspace_with_second_so101(tmp_path):
    workspace = tmp_path / "workspace"
    shutil.copytree(WORKSPACE / "contracts", workspace / "contracts")
    shutil.copytree(WORKSPACE / "manifests", workspace / "manifests")
    manifest = (WORKSPACE / "manifests/bi_so101_01.yaml").read_text(
        encoding="utf-8"
    )
    mapping = (
        WORKSPACE / "contracts/opcua/bi_so101_01.mapping.v2.yaml"
    ).read_text(encoding="utf-8")
    (workspace / "manifests/bi_so101_02.yaml").write_text(
        manifest.replace("bi_so101_01", "bi_so101_02").replace(
            "workcell-soarm-01", "workcell-soarm-02"
        ),
        encoding="utf-8",
    )
    (workspace / "contracts/opcua/bi_so101_02.mapping.v2.yaml").write_text(
        mapping.replace("bi_so101_01", "bi_so101_02"),
        encoding="utf-8",
    )
    return workspace


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("agent_id", "workcell-soarm-01", "duplicate agent_id"),
        ("robot_id", "bi_so101_01", "duplicate robot_id"),
        (
            "runtime_directory",
            "agents/workcell-soarm-01",
            "duplicate runtime_directory",
        ),
    ),
)
def test_inventory_rejects_global_identity_and_path_collisions(field, value, message):
    second = runtime_entry(
        agent_id="sim-isaac-omy-01",
        robot_id="omy_sim_01",
        runtime_directory="agents/sim-isaac-omy-01",
        opcua_port=4841,
        preview_port=8102,
        ros_domain_id=43,
    )
    second[field] = value

    with pytest.raises(RuntimeDeploymentError, match=message):
        parse_runtime_inventory(inventory(runtime_entry(), second))


def test_inventory_rejects_listener_and_ros_domain_collisions_on_one_node():
    second = runtime_entry(
        agent_id="sim-isaac-omy-01",
        robot_id="omy_sim_01",
        runtime_directory="agents/sim-isaac-omy-01",
        opcua_port=4841,
        preview_port=4840,
        ros_domain_id=43,
    )
    with pytest.raises(RuntimeDeploymentError, match="port 4840"):
        parse_runtime_inventory(inventory(runtime_entry(), second))

    second["preview_port"] = 8102
    second["ros_domain_id"] = 42
    with pytest.raises(RuntimeDeploymentError, match="ROS_DOMAIN_ID 42"):
        parse_runtime_inventory(inventory(runtime_entry(), second))


def test_same_ports_and_domain_are_allowed_on_different_processing_nodes():
    second = runtime_entry(
        agent_id="sim-isaac-omy-01",
        robot_id="omy_sim_01",
        processing_node_id="processing-sim-01",
        endpoint_host="192.0.2.21",
        runtime_directory="agents/sim-isaac-omy-01",
    )

    parsed = parse_runtime_inventory(inventory(runtime_entry(), second))

    assert len(parsed.runtimes) == 2


def test_endpoint_host_cannot_alias_two_processing_nodes():
    second = runtime_entry(
        agent_id="sim-isaac-omy-01",
        robot_id="omy_sim_01",
        processing_node_id="processing-sim-01",
        runtime_directory="agents/sim-isaac-omy-01",
        opcua_port=4841,
        preview_port=8102,
        ros_domain_id=43,
    )

    with pytest.raises(RuntimeDeploymentError, match="assigned to both"):
        parse_runtime_inventory(inventory(runtime_entry(), second))


def test_runtime_ports_must_be_unprivileged():
    with pytest.raises(RuntimeDeploymentError, match="greater than or equal to 1024"):
        parse_runtime_inventory(inventory(runtime_entry(opcua_port=443)))


def test_resolver_rejects_manifest_robot_identity_drift(tmp_path):
    parsed = parse_runtime_inventory(
        inventory(runtime_entry(robot_id="wrong-robot"))
    )

    with pytest.raises(RuntimeDeploymentError, match="differs from its manifest"):
        resolve_runtime_inventory(
            parsed,
            workspace_root=WORKSPACE,
            runtime_root=tmp_path / "runtime",
        )


def test_runtime_generation_is_idempotent_and_agent_scoped(tmp_path):
    pki = tmp_path / "pki"
    runtime_root = tmp_path / "runtime"
    db_path = tmp_path / "central/fleet.db"
    initialize_pki(pki, key_size=2048)
    parsed = parse_runtime_inventory(inventory(runtime_entry()))

    first = generate_runtime_deployments(
        parsed,
        workspace_root=WORKSPACE,
        runtime_root=runtime_root,
        pki_root=pki,
        db_path=db_path,
        host_workspace_root=WORKSPACE,
        host_runtime_root=runtime_root,
        host_pki_root=pki,
        uid=1000,
        gid=1000,
    )
    health_key = (
        pki
        / "roles/agents/workcell-soarm-01/edge/local/health-hmac.key"
    ).read_bytes()
    repeated = generate_runtime_deployments(
        parsed,
        workspace_root=WORKSPACE,
        runtime_root=runtime_root,
        pki_root=pki,
        db_path=db_path,
        host_workspace_root=WORKSPACE,
        host_runtime_root=runtime_root,
        host_pki_root=pki,
        uid=1000,
        gid=1000,
    )

    assert repeated == first
    assert (
        pki
        / "roles/agents/workcell-soarm-01/edge/local/health-hmac.key"
    ).read_bytes() == health_key
    env_path = runtime_root / "config/workcell-soarm-01.env"
    assert env_path.stat().st_mode & 0o777 == 0o600
    env = env_path.read_text(encoding="utf-8")
    assert "SANG_MAPPING=/ws/contracts/opcua/bi_so101_01.mapping.v2.yaml" in env
    assert "LOCAL_HEALTH_REPLAY_DB=/var/lib/sang/local-health-replay.db" in env
    deployment = json.loads(
        (
            runtime_root / "agents/workcell-soarm-01/deployment.json"
        ).read_text(encoding="utf-8")
    )
    assert deployment["runtime_kind"] == "physical"
    assert deployment["application_uri"] == "urn:sang:agent:workcell-soarm-01"
    assert deployment["opcua_server_certificate_ref"] == (
        "secret://opcua/pins/workcell-soarm-01.pem"
    )
    store = FleetStore(db_path)
    profile = store.get_agent_connection_profile("workcell-soarm-01", "opcua")
    assert profile["server_certificate_ref"] == (
        "secret://opcua/pins/workcell-soarm-01.pem"
    )
    assert store.schema_versions() == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]


def test_runtime_generation_refuses_to_replace_changed_env(tmp_path):
    pki = tmp_path / "pki"
    runtime_root = tmp_path / "runtime"
    initialize_pki(pki, key_size=2048)
    parsed = parse_runtime_inventory(inventory(runtime_entry()))
    arguments = {
        "workspace_root": WORKSPACE,
        "runtime_root": runtime_root,
        "pki_root": pki,
        "db_path": tmp_path / "central/fleet.db",
    }
    generate_runtime_deployments(parsed, **arguments)
    env_path = runtime_root / "config/workcell-soarm-01.env"
    env_path.write_text("changed=true\n", encoding="utf-8")

    with pytest.raises(RuntimeDeploymentError, match="refusing to overwrite"):
        generate_runtime_deployments(parsed, **arguments)


def test_deployment_index_allows_only_additive_runtime_updates(tmp_path):
    path = tmp_path / "deployment-index.json"
    first_runtime = {"agent_id": "agent-a", "value": 1}
    existing = {
        "schema_version": "1.0.0",
        "central_ingress_url": "https://192.0.2.10:8443",
        "runtimes": [first_runtime],
    }
    _write_deployment_index(path, existing, None)
    added = {
        **existing,
        "runtimes": [first_runtime, {"agent_id": "agent-b", "value": 2}],
    }

    _write_deployment_index(path, added, existing)

    assert json.loads(path.read_text(encoding="utf-8")) == added
    with pytest.raises(RuntimeDeploymentError, match="alter"):
        _write_deployment_index(
            path,
            {**added, "runtimes": [{"agent_id": "agent-a", "value": 9}]},
            added,
        )


def test_generator_adds_a_second_runtime_without_mutating_the_first(tmp_path):
    workspace = workspace_with_second_so101(tmp_path)
    pki = tmp_path / "pki"
    runtime_root = tmp_path / "runtime"
    db_path = tmp_path / "central/fleet.db"
    initialize_pki(pki, key_size=2048)
    first_inventory = parse_runtime_inventory(inventory(runtime_entry()))
    arguments = {
        "workspace_root": workspace,
        "runtime_root": runtime_root,
        "pki_root": pki,
        "db_path": db_path,
    }
    first = generate_runtime_deployments(first_inventory, **arguments)
    first_record = first["runtimes"][0]
    second = runtime_entry(
        agent_id="workcell-soarm-02",
        agent_name="Sang Dual SO-101 B",
        robot_id="bi_so101_02",
        processing_node_id="processing-soarm-02",
        endpoint_host="192.0.2.21",
        manifest="manifests/bi_so101_02.yaml",
        mapping="contracts/opcua/bi_so101_02.mapping.v2.yaml",
        runtime_directory="agents/workcell-soarm-02",
    )
    combined_inventory = parse_runtime_inventory(
        inventory(runtime_entry(), second)
    )

    combined = generate_runtime_deployments(combined_inventory, **arguments)

    assert combined["runtimes"][0] == first_record
    assert {item["agent_id"] for item in combined["runtimes"]} == {
        "workcell-soarm-01",
        "workcell-soarm-02",
    }
    allocation_registry = json.loads(
        (pki / "roles/central/runtime-allocations.json").read_text(
            encoding="utf-8"
        )
    )
    assert {item["agent_id"] for item in allocation_registry["runtimes"]} == {
        "workcell-soarm-01",
        "workcell-soarm-02",
    }
    with pytest.raises(RuntimeDeploymentError, match="would remove existing runtime"):
        generate_runtime_deployments(first_inventory, **arguments)


def test_global_allocations_reject_collisions_across_runtime_roots(tmp_path):
    workspace = workspace_with_second_so101(tmp_path)
    pki = tmp_path / "pki"
    db_path = tmp_path / "central/fleet.db"
    initialize_pki(pki, key_size=2048)
    generate_runtime_deployments(
        parse_runtime_inventory(inventory(runtime_entry())),
        workspace_root=workspace,
        runtime_root=tmp_path / "runtime-a",
        pki_root=pki,
        db_path=db_path,
    )
    colliding = runtime_entry(
        agent_id="workcell-soarm-02",
        agent_name="Sang Dual SO-101 B",
        robot_id="bi_so101_02",
        manifest="manifests/bi_so101_02.yaml",
        mapping="contracts/opcua/bi_so101_02.mapping.v2.yaml",
        runtime_directory="agents/workcell-soarm-02",
    )

    with pytest.raises(RuntimeDeploymentError, match="reuses port 4840"):
        generate_runtime_deployments(
            parse_runtime_inventory(inventory(colliding)),
            workspace_root=workspace,
            runtime_root=tmp_path / "runtime-b",
            pki_root=pki,
            db_path=db_path,
        )

    assert not (pki / "issued/opcua/workcell-soarm-02-opcua-server").exists()


def test_global_allocations_survive_a_later_agent_provisioning_failure(
    tmp_path,
    monkeypatch,
):
    workspace = workspace_with_second_so101(tmp_path)
    pki = tmp_path / "pki"
    initialize_pki(pki, key_size=2048)
    second = runtime_entry(
        agent_id="workcell-soarm-02",
        agent_name="Sang Dual SO-101 B",
        robot_id="bi_so101_02",
        processing_node_id="processing-soarm-02",
        endpoint_host="192.0.2.21",
        manifest="manifests/bi_so101_02.yaml",
        mapping="contracts/opcua/bi_so101_02.mapping.v2.yaml",
        runtime_directory="agents/workcell-soarm-02",
    )
    original = runtime_deploy.provision_agent_runtime

    def fail_second(root, *, agent_id, edge_host):
        if agent_id == "workcell-soarm-02":
            raise PkiError("injected second-Agent failure")
        return original(root, agent_id=agent_id, edge_host=edge_host)

    monkeypatch.setattr(runtime_deploy, "provision_agent_runtime", fail_second)

    with pytest.raises(RuntimeDeploymentError, match="injected second-Agent failure"):
        generate_runtime_deployments(
            parse_runtime_inventory(inventory(runtime_entry(), second)),
            workspace_root=workspace,
            runtime_root=tmp_path / "runtime",
            pki_root=pki,
            db_path=tmp_path / "central/fleet.db",
        )

    registry = json.loads(
        (pki / "roles/central/runtime-allocations.json").read_text(
            encoding="utf-8"
        )
    )
    assert {item["agent_id"] for item in registry["runtimes"]} == {
        "workcell-soarm-01",
        "workcell-soarm-02",
    }


def test_legacy_pin_conflict_fails_before_opcua_certificate_issuance(tmp_path):
    pki = tmp_path / "pki"
    runtime_root = tmp_path / "runtime"
    db_path = tmp_path / "central/fleet.db"
    initialize_pki(pki, key_size=2048)
    https_roles = provision_https_agent(pki, agent_id="workcell-soarm-01")
    metadata = json.loads(
        (Path(https_roles["central"]) / "role-metadata.json").read_text(
            encoding="utf-8"
        )
    )
    enroll_phase3_edge(
        FleetStore(db_path),
        metadata=metadata,
        display_name="Sang Dual SO-101",
        central_ingress_url="https://192.0.2.10:8443",
        edge_opcua_endpoint="opc.tcp://192.0.2.20:4840/sang/",
    )
    parsed = parse_runtime_inventory(inventory(runtime_entry()))

    with pytest.raises(RuntimeDeploymentError, match="server_certificate_ref"):
        generate_runtime_deployments(
            parsed,
            workspace_root=WORKSPACE,
            runtime_root=runtime_root,
            pki_root=pki,
            db_path=db_path,
        )

    assert not (pki / "issued/opcua/workcell-soarm-01-opcua-server").exists()


def test_revoked_https_credential_fails_before_opcua_certificate_issuance(tmp_path):
    pki = tmp_path / "pki"
    runtime_root = tmp_path / "runtime"
    db_path = tmp_path / "central/fleet.db"
    initialize_pki(pki, key_size=2048)
    https_roles = provision_https_agent(pki, agent_id="workcell-soarm-01")
    metadata = json.loads(
        (Path(https_roles["central"]) / "role-metadata.json").read_text(
            encoding="utf-8"
        )
    )
    store = FleetStore(db_path)
    enroll_phase3_edge(
        store,
        metadata=metadata,
        display_name="Sang Dual SO-101",
        central_ingress_url="https://192.0.2.10:8443",
        edge_opcua_endpoint=None,
    )
    store.revoke_agent_credential(
        "workcell-soarm-01-https-01",
        CredentialRevoke(reason="test revocation"),
        occurred_at=datetime.now(timezone.utc),
    )

    with pytest.raises(RuntimeDeploymentError, match="credential differs"):
        generate_runtime_deployments(
            parse_runtime_inventory(inventory(runtime_entry())),
            workspace_root=WORKSPACE,
            runtime_root=runtime_root,
            pki_root=pki,
            db_path=db_path,
        )

    assert not (pki / "issued/opcua/workcell-soarm-01-opcua-server").exists()
