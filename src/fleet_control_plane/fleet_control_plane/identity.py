"""Offline bootstrap and service credential provisioning for Central."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import secrets
import stat

from fleet_control_plane.auth import AuthManager, ServiceIdentityCreate
from fleet_control_plane.store import FleetStore


def _password_file(path: Path) -> str:
    resolved = path.expanduser().resolve(strict=True)
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode & 0o077:
        raise ValueError("password file must not be readable or writable by group/others")
    password = resolved.read_text(encoding="utf-8").rstrip("\r\n")
    if not password:
        raise ValueError("password file is empty")
    return password


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _admin_actor(manager: AuthManager, actor_id: str) -> None:
    actor = next(
        (item for item in manager.list_principals() if item["principal_id"] == actor_id),
        None,
    )
    if actor is None or actor["role"] != "admin" or not actor["enabled"]:
        raise ValueError("actor-id must name an enabled administrator")


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SANG Central identity bootstrap")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path.home() / ".local" / "share" / "sang-fleet" / "fleet.db",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap = subparsers.add_parser("bootstrap-admin")
    bootstrap.add_argument("--principal-id", required=True)
    bootstrap.add_argument("--display-name", required=True)
    bootstrap.add_argument("--password-file", type=Path, required=True)
    service = subparsers.add_parser("create-service")
    service.add_argument("--actor-id", required=True)
    service.add_argument("--principal-id", required=True)
    service.add_argument("--display-name", required=True)
    service.add_argument("--role", choices=("viewer", "operator", "admin"), required=True)
    service.add_argument("--token-id", required=True)
    service.add_argument(
        "--agent-id",
        action="append",
        default=[],
        help="Agent scope for this service; repeat for multiple Agents",
    )
    service.add_argument("--expires-at", type=_aware_datetime)
    service.add_argument("--token-output", type=Path, required=True)
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    FleetStore(options.db_path)
    manager = AuthManager(options.db_path)
    if options.command == "bootstrap-admin":
        result = manager.bootstrap_admin(
            principal_id=options.principal_id,
            display_name=options.display_name,
            password=_password_file(options.password_file),
            occurred_at=datetime.now(timezone.utc),
        )
        print(
            f"bootstrapped administrator {result['principal_id']} "
            f"with role {result['role']}"
        )
        return

    _admin_actor(manager, options.actor_id)
    output = options.token_output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    raw_token = "sang_pat_" + secrets.token_urlsafe(32)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    created = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            stream.write(raw_token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        result = manager.create_service_identity(
            ServiceIdentityCreate(
                principal_id=options.principal_id,
                display_name=options.display_name,
                role=options.role,
                token_id=options.token_id,
                expires_at=options.expires_at,
                agent_ids=options.agent_id,
            ),
            actor_id=options.actor_id,
            occurred_at=datetime.now(timezone.utc),
            raw_token=raw_token,
        )
        created = True
    finally:
        raw_token = ""
        if not created:
            output.unlink(missing_ok=True)
    print(
        f"created service identity {result['principal_id']} with role {result['role']}; "
        f"token written once to {output}"
    )


if __name__ == "__main__":
    main()
