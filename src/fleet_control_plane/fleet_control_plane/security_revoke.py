"""Revoke one Agent certificate in both the runtime registry and lab PKI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fleet_control_plane.operations_models import CredentialRevoke
from fleet_control_plane.status import utc_now
from fleet_control_plane.store import FleetStore
from sang_security.pki import revoke_certificate


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Revoke an Agent credential in the registry and PKI"
    )
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--credential-id", required=True)
    parser.add_argument("--pki-root", type=Path, required=True)
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--reason", required=True)
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    try:
        pki = revoke_certificate(
            options.pki_root,
            certificate_path=options.certificate,
            reason=options.reason,
        )
        registry = FleetStore(options.db_path).revoke_agent_credential(
            options.credential_id,
            CredentialRevoke(reason=options.reason),
            occurred_at=utc_now(),
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Agent credential revocation failed: {exc}") from exc
    print(
        json.dumps(
            {"pki": pki, "registry": registry},
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
