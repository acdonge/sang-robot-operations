import asyncio
from pathlib import Path
import shutil
import socket
import time

from asyncua import Client
from asyncua.ua.uaerrors import (
    BadCertificateRevoked,
    BadCertificateUntrusted,
    BadUserAccessDenied,
    UaError,
)
import pytest

from sang_opcua_gateway.contract import load_gateway_contract
from sang_opcua_gateway.server import OpcUaServerThread
from sang_opcua_gateway.state import GatewayStore
from sang_security.opcua import (
    OpcUaClientSecurity,
    OpcUaServerSecurity,
    configure_secure_client,
)
from sang_security.pki import initialize_pki, issue_certificate, revoke_certificate


ROOT = Path(__file__).resolve().parents[3]
AGENT_ID = "workcell-soarm-01"
EDGE_URI = f"urn:sang:agent:{AGENT_ID}"
CENTRAL_URI = "urn:sang:central:control-plane"


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def contract(endpoint):
    return load_gateway_contract(
        manifest_path=ROOT / "manifests/bi_so101_01.yaml",
        mapping_path=ROOT / "contracts/opcua/bi_so101_01.mapping.v2.yaml",
        model_contract_path=ROOT / "contracts/opcua/model-contract.yaml",
        status_codes_path=ROOT / "contracts/opcua/status-code-mapping.yaml",
        nodeset_path=ROOT / "contracts/opcua/SANG.NodeSet2.xml",
        endpoint=endpoint,
        agent_id=AGENT_ID,
        preview_base_url="http://127.0.0.1:8101",
        allow_remote_endpoint=True,
    )


def issue_material(root):
    edge = issue_certificate(
        root,
        issuer="opcua",
        name="edge-server",
        role="opcua-server",
        uri=EDGE_URI,
        dns_names=("localhost",),
        ip_addresses=("127.0.0.1",),
    )
    application = issue_certificate(
        root,
        issuer="opcua",
        name="central-application",
        role="opcua-client",
        uri=CENTRAL_URI,
    )
    user = issue_certificate(
        root,
        issuer="opcua",
        name="central-user",
        role="opcua-user",
        uri="urn:sang:user:central-shadow",
    )
    unauthorized_user = issue_certificate(
        root,
        issuer="opcua",
        name="unauthorized-user",
        role="opcua-user",
        uri="urn:sang:user:unauthorized",
    )
    return edge, application, user, unauthorized_user


def server_security(trust_directory, crl_directory, edge, user):
    return OpcUaServerSecurity(
        certificate=Path(edge["certificate"]),
        private_key=Path(edge["private_key"]),
        trust_directory=trust_directory,
        crl_directory=crl_directory,
        authorized_user_certificates=(Path(user["certificate"]),),
    )


def client_security(root, edge, application, user):
    return OpcUaClientSecurity(
        application_uri=CENTRAL_URI,
        expected_server_application_uri=EDGE_URI,
        application_certificate=Path(application["certificate"]),
        application_private_key=Path(application["private_key"]),
        trust_directory=root / "trust" / "opcua",
        crl_directory=root / "crl" / "opcua",
        user_certificate=Path(user["certificate"]),
        user_private_key=Path(user["private_key"]),
        server_certificate=Path(edge["certificate"]),
    )


async def secure_read(endpoint, security):
    client = Client(endpoint, timeout=3.0)
    await configure_secure_client(client, security)
    async with client:
        assert await client.nodes.server_state.read_value() == 0


async def insecure_read(endpoint):
    async with Client(endpoint, timeout=2.0) as client:
        await client.nodes.server_state.read_value()


def test_secure_gateway_accepts_only_trusted_application_and_authorized_user(tmp_path):
    pki = tmp_path / "pki"
    initialize_pki(pki, key_size=2048)
    edge, application, user, unauthorized_user = issue_material(pki)
    edge_runtime = tmp_path / "edge-runtime"
    edge_trust = edge_runtime / "trust"
    edge_crl = edge_runtime / "crl"
    shutil.copytree(pki / "trust" / "opcua", edge_trust)
    shutil.copytree(pki / "crl" / "opcua", edge_crl)
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/sang/"
    gateway_contract = contract(endpoint)
    server = OpcUaServerThread(
        gateway_contract,
        GatewayStore(gateway_contract),
        security=server_security(edge_trust, edge_crl, edge, user),
        trust_reload_interval_s=0.05,
    )
    server.start()
    try:
        asyncio.run(secure_read(endpoint, client_security(pki, edge, application, user)))
        with pytest.raises(UaError, match="No matching endpoints"):
            asyncio.run(insecure_read(endpoint))
        with pytest.raises(BadUserAccessDenied):
            asyncio.run(
                secure_read(
                    endpoint,
                    client_security(pki, edge, application, unauthorized_user),
                )
            )

        rogue_pki = tmp_path / "rogue-pki"
        initialize_pki(rogue_pki, key_size=2048)
        rogue_application = issue_certificate(
            rogue_pki,
            issuer="opcua",
            name="rogue-application",
            role="opcua-client",
            uri=CENTRAL_URI,
        )
        untrusted = client_security(pki, edge, rogue_application, user)
        with pytest.raises(BadCertificateUntrusted):
            asyncio.run(secure_read(endpoint, untrusted))

        revoke_certificate(
            pki,
            certificate_path=application["certificate"],
            reason="test application revocation",
        )
        time.sleep(0.15)
        asyncio.run(secure_read(endpoint, client_security(pki, edge, application, user)))
        shutil.copy2(
            pki / "crl" / "opcua" / "opcua-crl.pem",
            edge_crl / "opcua-crl.pem",
        )
        time.sleep(0.15)
        with pytest.raises(BadCertificateRevoked):
            asyncio.run(
                secure_read(endpoint, client_security(pki, edge, application, user))
            )

        rotated_application = issue_certificate(
            pki,
            issuer="opcua",
            name="central-application-rotated",
            role="opcua-client",
            uri=CENTRAL_URI,
        )
        revoke_certificate(
            pki,
            certificate_path=user["certificate"],
            reason="test user revocation",
        )
        shutil.copy2(
            pki / "crl" / "opcua" / "opcua-crl.pem",
            edge_crl / "opcua-crl.pem",
        )
        time.sleep(0.15)
        with pytest.raises(BadUserAccessDenied):
            asyncio.run(
                secure_read(
                    endpoint,
                    client_security(
                        pki,
                        edge,
                        rotated_application,
                        user,
                    ),
                )
            )
    finally:
        server.stop()
