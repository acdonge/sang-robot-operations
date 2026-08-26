import pytest

from fleet_control_plane.serve import parse_args as parse_serve_args
from fleet_control_plane.soak import parse_args as parse_soak_args


def test_central_cli_defaults_use_active_admin_port():
    assert parse_serve_args([]).port == 18091
    options = parse_soak_args(["--agent-id", "agent", "--robot-id", "robot"])
    assert options.control_plane_url == "http://127.0.0.1:18091"


def test_remote_central_requires_auth_https_and_host_allowlist():
    with pytest.raises(SystemExit):
        parse_serve_args(["--host", "0.0.0.0"])
    with pytest.raises(SystemExit):
        parse_serve_args(
            [
                "--host",
                "0.0.0.0",
                "--https-secret-root",
                "/secrets",
                "--https-server-certificate-ref",
                "secret://https/server.pem",
                "--https-server-private-key-ref",
                "secret://https/server.key",
            ]
        )

    options = parse_serve_args(
        [
            "--host",
            "0.0.0.0",
            "--allowed-host",
            "manager.sang.local",
            "--https-secret-root",
            "/secrets",
            "--https-server-certificate-ref",
            "secret://https/server.pem",
            "--https-server-private-key-ref",
            "secret://https/server.key",
        ]
    )
    assert options.secure_cookies is True
    assert options.auth_disabled is False
