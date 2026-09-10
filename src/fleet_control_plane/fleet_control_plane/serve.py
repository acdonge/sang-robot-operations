"""Command-line entry point for the loopback-only Admin MVP."""

import argparse
from pathlib import Path

from fleet_control_plane.app import create_app
from robot_agent.heartbeat import AgentTLSConfig
from sang_security.refs import SecretResolver
from fleet_control_plane.status import FleetTTLs
from fleet_control_plane.store import FleetStore
from fleet_control_plane.recording_storage import FilesystemObjectStore
from robot_agent.clock_sync import ClockSyncSampler


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
    parser.add_argument(
        "--auth-disabled",
        action="store_true",
        help="legacy loopback-only mode; Robot/Camera v1 deployments must not use it",
    )
    parser.add_argument(
        "--secure-cookies",
        action="store_true",
        help="mark browser cookies Secure when TLS terminates at the application boundary",
    )
    parser.add_argument("--preview-secret-root", type=Path)
    parser.add_argument("--preview-ca-bundle-ref")
    parser.add_argument("--preview-client-certificate-ref")
    parser.add_argument("--preview-client-private-key-ref")
    parser.add_argument("--https-secret-root", type=Path)
    parser.add_argument("--https-server-certificate-ref")
    parser.add_argument("--https-server-private-key-ref")
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="accepted HTTP Host name; required for remote bind",
    )
    parser.add_argument(
        "--object-store-root",
        type=Path,
        help="immutable central recording Object Store root",
    )
    parser.add_argument(
        "--quality-policy-root",
        type=Path,
        help="checked-in Robot Manifest directory used for recording quality limits",
    )
    parser.add_argument(
        "--aas-model-lock",
        type=Path,
        default=Path("/ws/contracts/aas/metamodel.lock.yaml"),
        help="pinned IDTA metamodel lock used for read-only AAS imports",
    )
    parser.add_argument("--clock-sync-host")
    parser.add_argument("--clock-sync-timeout-s", type=float, default=1.0)
    parser.add_argument(
        "--workflow-adapter-profile",
        action="append",
        type=Path,
        default=[],
        help="checked-in output-disconnected Unity adapter profile",
    )
    parser.add_argument(
        "--workflow-skill-catalog",
        action="append",
        type=Path,
        default=[],
        help="checked-in versioned Skill definitions for Workflow authoring",
    )
    parser.add_argument(
        "--workflow-runtime-base-url",
        help="loopback URL of the fixed Unity runtime supervisor",
    )
    options = parser.parse_args(args)
    if not 1 <= options.port <= 65535:
        parser.error("port must be between 1 and 65535")
    preview_tls_values = (
        options.preview_secret_root,
        options.preview_ca_bundle_ref,
        options.preview_client_certificate_ref,
        options.preview_client_private_key_ref,
    )
    if any(value is not None for value in preview_tls_values) and any(
        value is None for value in preview_tls_values
    ):
        parser.error("Central preview TLS requires secret root, CA, certificate and key")
    https_values = (
        options.https_secret_root,
        options.https_server_certificate_ref,
        options.https_server_private_key_ref,
    )
    if any(value is not None for value in https_values) and any(
        value is None for value in https_values
    ):
        parser.error("Central HTTPS requires secret root, server certificate and key")
    if options.host not in LOOPBACK_HOSTS:
        if options.auth_disabled:
            parser.error("remote Central bind requires authentication")
        if any(value is None for value in https_values):
            parser.error("remote Central bind requires HTTPS")
        if not options.allowed_host:
            parser.error("remote Central bind requires at least one --allowed-host")
    for host in options.allowed_host:
        if not host or "/" in host or "://" in host:
            parser.error("allowed-host values must be host names or IP addresses")
    if options.https_secret_root is not None:
        options.secure_cookies = True
    if options.quality_policy_root is not None and options.object_store_root is None:
        parser.error("recording quality analysis requires --object-store-root")
    if options.clock_sync_timeout_s <= 0.0:
        parser.error("clock-sync-timeout-s must be positive")
    return options


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    import uvicorn

    preview_ssl_context = None
    if options.preview_secret_root is not None:
        resolver = SecretResolver(options.preview_secret_root)
        preview_ssl_context = AgentTLSConfig(
            ca_bundle=resolver.resolve(options.preview_ca_bundle_ref),
            client_certificate_chain=resolver.resolve(
                options.preview_client_certificate_ref
            ),
            client_private_key=resolver.resolve(
                options.preview_client_private_key_ref,
                private=True,
            ),
        ).context()

    https_certificate = None
    https_private_key = None
    if options.https_secret_root is not None:
        resolver = SecretResolver(options.https_secret_root)
        https_certificate = resolver.resolve(options.https_server_certificate_ref)
        https_private_key = resolver.resolve(
            options.https_server_private_key_ref,
            private=True,
        )

    app = create_app(
        FleetStore(options.db_path),
        ui_dir=options.ui_dir,
        operator_ui_dir=options.operator_ui_dir,
        ttls=FleetTTLs(options.online_ttl_s, options.offline_ttl_s),
        auth_enabled=not options.auth_disabled,
        secure_cookies=options.secure_cookies,
        preview_ssl_context=preview_ssl_context,
        object_store=(
            None
            if options.object_store_root is None
            else FilesystemObjectStore(options.object_store_root)
        ),
        quality_policy_root=options.quality_policy_root,
        clock_sync=(
            None
            if options.clock_sync_host is None
            else ClockSyncSampler(
                options.clock_sync_host,
                timeout_s=options.clock_sync_timeout_s,
            )
        ),
        workflow_adapter_profile_paths=options.workflow_adapter_profile,
        workflow_skill_catalog_paths=options.workflow_skill_catalog,
        workflow_runtime_base_url=options.workflow_runtime_base_url,
        aas_model_lock_path=options.aas_model_lock,
        allowed_admin_hosts=frozenset(
            {"127.0.0.1", "localhost", "::1", "testserver", *options.allowed_host}
        ),
    )
    uvicorn.run(
        app,
        host=options.host,
        port=options.port,
        ssl_certfile=None if https_certificate is None else str(https_certificate),
        ssl_keyfile=None if https_private_key is None else str(https_private_key),
    )


if __name__ == "__main__":
    main()
