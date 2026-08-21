"""Enroll one Phase 3 Edge identity and its desired secure connections."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fleet_control_plane.operations_models import (
    AgentCredentialCreate,
    AgentRegistrationCreate,
    ConnectionProfileCreate,
)
from fleet_control_plane.status import isoformat_z, parse_datetime, utc_now
from fleet_control_plane.store import FleetStore
from sang_security.certificates import (
    certificate_sha256,
    load_certificate,
    require_identity_uri,
)


def verify_role_metadata(metadata: dict, role_root: Path) -> None:
    agent_id = metadata["agent_id"]
    certificate = load_certificate(role_root / "https" / "agents" / f"{agent_id}.pem")
    require_identity_uri(certificate, f"urn:sang:agent:{agent_id}")
    edge = metadata["edge"]
    if edge["https_client_certificate_sha256"] != certificate_sha256(certificate):
        raise ValueError("Agent certificate fingerprint differs from role metadata")
    if parse_datetime(edge["https_client_not_before"]) != (
        certificate.not_valid_before_utc
    ):
        raise ValueError("Agent certificate not_before differs from role metadata")
    if parse_datetime(edge["https_client_not_after"]) != certificate.not_valid_after_utc:
        raise ValueError("Agent certificate not_after differs from role metadata")


def verify_runtime_metadata(metadata: dict, role_root: Path) -> str:
    agent_id = metadata["agent_id"]
    if metadata.get("application_uri") != f"urn:sang:agent:{agent_id}":
        raise ValueError("OPC UA runtime ApplicationUri differs from the Agent")
    server_ref = metadata["central"]["server_certificate_ref"]
    expected_ref = f"secret://opcua/pins/{agent_id}.pem"
    if server_ref != expected_ref:
        raise ValueError("OPC UA runtime server pin reference is not Agent-scoped")
    certificate = load_certificate(role_root / "opcua" / "pins" / f"{agent_id}.pem")
    require_identity_uri(certificate, metadata["application_uri"])
    if metadata["edge"]["opcua_server_certificate_sha256"] != certificate_sha256(
        certificate
    ):
        raise ValueError("OPC UA server pin fingerprint differs from runtime metadata")
    return server_ref


def enroll_phase3_edge(
    store: FleetStore,
    *,
    metadata: dict,
    display_name: str,
    central_ingress_url: str,
    edge_opcua_endpoint: str | None,
    opcua_server_certificate_ref: str | None = None,
) -> dict:
    agent_id = metadata["agent_id"]
    edge = metadata["edge"]
    occurred_at = utc_now()
    registration_body = AgentRegistrationCreate(
        agent_id=agent_id,
        display_name=display_name,
    )
    credential_body = AgentCredentialCreate(
        credential_id=f"{agent_id}-https-01",
        agent_id=agent_id,
        certificate_sha256=edge["https_client_certificate_sha256"],
        certificate_ref="secret://https/client/chain.pem",
        not_before=parse_datetime(edge["https_client_not_before"]),
        not_after=parse_datetime(edge["https_client_not_after"]),
    )
    heartbeat_body = ConnectionProfileCreate(
        profile_id=f"{agent_id}-https",
        owner_kind="agent",
        agent_id=agent_id,
        protocol="https_agent",
        desired_endpoint=central_ingress_url,
        security_mode="MutualTLS",
        security_policy="TLS1.2+",
        client_certificate_ref="secret://https/client/chain.pem",
        client_private_key_ref="secret://https/client/key.pem",
        trust_list_ref="secret://https/trust/ca-crl.pem",
        crl_ref="secret://https/trust/ca-crl.pem",
    )
    opcua_body = None
    if edge_opcua_endpoint is not None:
        server_certificate_ref = (
            opcua_server_certificate_ref
            or "secret://opcua/pins/edge-cert.pem"
        )
        opcua_body = ConnectionProfileCreate(
            profile_id=f"{agent_id}-opcua",
            owner_kind="agent",
            agent_id=agent_id,
            protocol="opcua",
            desired_endpoint=edge_opcua_endpoint,
            expected_application_uri=f"urn:sang:agent:{agent_id}",
            security_mode="SignAndEncrypt",
            security_policy="Basic256Sha256",
            client_certificate_ref="secret://opcua/application/cert.pem",
            client_private_key_ref="secret://opcua/application/key.pem",
            trust_list_ref="secret://opcua/trust/servers",
            crl_ref="secret://opcua/crl",
            server_certificate_ref=server_certificate_ref,
            user_certificate_ref="secret://opcua/user/cert.pem",
            user_private_key_ref="secret://opcua/user/key.pem",
        )

    def existing_or_create(items, key, expected, create, label):
        existing = next((item for item in items if item[key] == expected[key]), None)
        if existing is None:
            return create()
        mismatched = [
            field
            for field, value in expected.items()
            if field in existing and existing[field] != value
        ]
        if mismatched:
            raise ValueError(
                f"existing {label} differs in: {', '.join(sorted(mismatched))}"
            )
        return existing

    registration_expected = {
        "agent_id": agent_id,
        "display_name": display_name,
        "identity_uri": f"urn:sang:agent:{agent_id}",
        "enabled": True,
    }
    registration = existing_or_create(
        store.list_agent_registrations(),
        "agent_id",
        registration_expected,
        lambda: store.create_agent_registration(
            registration_body,
            occurred_at=occurred_at,
        ),
        "Agent registration",
    )
    credential_expected = credential_body.model_dump(mode="json")
    credential_expected["not_before"] = isoformat_z(credential_body.not_before)
    credential_expected["not_after"] = isoformat_z(credential_body.not_after)
    credential_expected["status"] = "active"
    credential = existing_or_create(
        store.list_agent_credentials(),
        "credential_id",
        credential_expected,
        lambda: store.create_agent_credential(
            credential_body,
            occurred_at=occurred_at,
        ),
        "Agent credential",
    )
    profiles = store.list_connection_profiles()
    heartbeat_profile = existing_or_create(
        profiles,
        "profile_id",
        heartbeat_body.model_dump(mode="json"),
        lambda: store.create_connection_profile(
            heartbeat_body,
            occurred_at=occurred_at,
        ),
        "HTTPS connection profile",
    )
    connection_profiles = [heartbeat_profile]
    if opcua_body is not None:
        opcua_profile = existing_or_create(
            profiles,
            "profile_id",
            opcua_body.model_dump(mode="json"),
            lambda: store.create_connection_profile(
                opcua_body,
                occurred_at=occurred_at,
            ),
            "OPC UA connection profile",
        )
        connection_profiles.append(opcua_profile)
    return {
        "agent_registration": registration,
        "agent_credential": credential,
        "connection_profiles": connection_profiles,
    }


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enroll one SANG Phase 3 Edge")
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--role-metadata", type=Path, required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--central-ingress-url", required=True)
    parser.add_argument("--edge-opcua-endpoint")
    parser.add_argument("--runtime-metadata", type=Path)
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    try:
        metadata = json.loads(options.role_metadata.read_text(encoding="utf-8"))
        verify_role_metadata(metadata, options.role_metadata.parent)
        server_certificate_ref = None
        if options.runtime_metadata is not None:
            runtime_metadata = json.loads(
                options.runtime_metadata.read_text(encoding="utf-8")
            )
            if runtime_metadata.get("agent_id") != metadata.get("agent_id"):
                raise ValueError("HTTPS and OPC UA runtime metadata Agent IDs differ")
            server_certificate_ref = verify_runtime_metadata(
                runtime_metadata,
                options.runtime_metadata.parent,
            )
            if options.edge_opcua_endpoint is None:
                raise ValueError("runtime metadata requires an OPC UA endpoint")
        result = enroll_phase3_edge(
            FleetStore(options.db_path),
            metadata=metadata,
            display_name=options.display_name,
            central_ingress_url=options.central_ingress_url,
            edge_opcua_endpoint=options.edge_opcua_endpoint,
            opcua_server_certificate_ref=server_certificate_ref,
        )
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Phase 3 enrollment failed: {exc}") from exc
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
