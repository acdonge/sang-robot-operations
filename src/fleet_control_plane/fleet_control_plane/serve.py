"""Command-line entry point for the loopback-only Admin MVP."""

import argparse
from pathlib import Path

from fleet_control_plane.app import create_app
from fleet_control_plane.status import FleetTTLs
from fleet_control_plane.store import FleetStore


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SANG Fleet/Admin Control Plane MVP")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path.home() / ".local" / "share" / "sang-fleet" / "fleet.db",
    )
    parser.add_argument("--ui-dir", type=Path, default=Path("/ws/ui-admin"))
    parser.add_argument(
        "--operator-ui-dir",
        type=Path,
        default=Path("/ws/ui-operator"),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18091)
    parser.add_argument("--online-ttl-s", type=float, default=12.0)
    parser.add_argument("--offline-ttl-s", type=float, default=30.0)
    options = parser.parse_args(args)
    if options.host not in LOOPBACK_HOSTS:
        parser.error(
            "remote bind is disabled in this MVP; per-Agent authentication and TLS "
            "must be implemented first"
        )
    if not 1 <= options.port <= 65535:
        parser.error("port must be between 1 and 65535")
    return options


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    import uvicorn

    app = create_app(
        FleetStore(options.db_path),
        ui_dir=options.ui_dir,
        operator_ui_dir=options.operator_ui_dir,
        ttls=FleetTTLs(options.online_ttl_s, options.offline_ttl_s),
    )
    uvicorn.run(app, host=options.host, port=options.port)


if __name__ == "__main__":
    main()
