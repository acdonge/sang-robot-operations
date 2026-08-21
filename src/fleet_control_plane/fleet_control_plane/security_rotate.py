"""Prepare make-before-break rotation of one Agent mTLS certificate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fleet_control_plane.operations_models import AgentCredentialCreate
from fleet_control_plane.status import isoformat_z, parse_datetime, utc_now
from fleet_control_plane.store import FleetStore
from sang_security.pki import (
    activate_agent_https_certificate,
    issue_certificate,
)


def prepare_agent_https_rotation(
    store: FleetStore,
    *,
    pki_root: Path,
    agent_id: str,
    certificate_name: str,
    credential_id: str,
    days: int = 365,
) -> dict:
    previous_active = [
        item
        for item in store.list_agent_credentials()
        if item["agent_id"] == agent_id and item["status"] == "active"
    ]
    issued = issue_certificate(
        pki_root,
        issuer="https",
        name=certificate_name,
        role="https-client",
        uri=f"urn:sang:agent:{agent_id}",
        days=days,
    )
    body = AgentCredentialCreate(
        credential_id=credential_id,
        agent_id=agent_id,
        certificate_sha256=issued["certificate_sha256"],
        certificate_ref="secret://https/client/chain.pem",
        not_before=parse_datetime(issued["not_before"]),
        not_after=parse_datetime(issued["not_after"]),
    )
    existing = next(
        (
            item
            for item in store.list_agent_credentials()
            if item["credential_id"] == credential_id
        ),
        None,
    )
    if existing is None:
        credential = store.create_agent_credential(body, occurred_at=utc_now())
    else:
        expected = {
            **body.model_dump(mode="json"),
            "not_before": isoformat_z(body.not_before),
            "not_after": isoformat_z(body.not_after),
            "status": "active",
        }
        mismatched = [
            field
            for field, value in expected.items()
            if existing.get(field) != value
        ]
        if mismatched:
            raise ValueError(
                "existing rotation credential differs in: "
                + ", ".join(sorted(mismatched))
            )
        credential = existing
    activation = activate_agent_https_certificate(
        pki_root,
        agent_id=agent_id,
        certificate=issued,
    )
    return {
        "issued": issued,
        "registry_credential": credential,
        "activation": activation,
        "previous_active_credential_ids": [
            item["credential_id"]
            for item in previous_active
            if item["credential_id"] != credential_id
        ],
    }


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare make-before-break Agent mTLS certificate rotation"
    )
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--pki-root", type=Path, required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--certificate-name", required=True)
    parser.add_argument("--credential-id", required=True)
    parser.add_argument("--days", type=int, default=365)
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    try:
        result = prepare_agent_https_rotation(
            FleetStore(options.db_path),
            pki_root=options.pki_root,
            agent_id=options.agent_id,
            certificate_name=options.certificate_name,
            credential_id=options.credential_id,
            days=options.days,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Agent credential rotation preparation failed: {exc}") from exc
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
