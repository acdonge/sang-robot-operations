from fleet_control_plane.serve import parse_args as parse_serve_args
from fleet_control_plane.soak import parse_args as parse_soak_args


def test_central_cli_defaults_use_active_admin_port():
    assert parse_serve_args([]).port == 18091
    options = parse_soak_args(["--agent-id", "agent", "--robot-id", "robot"])
    assert options.control_plane_url == "http://127.0.0.1:18091"
