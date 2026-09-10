import ast
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[3]


def test_g3_g6_runtime_contains_no_physical_transport_or_publisher():
    modules = [
        "agent_execution.py",
        "cell_orchestrator.py",
        "commissioning_evidence.py",
        "commissioning_readiness.py",
        "control_authority.py",
        "control_contracts.py",
        "control_signing.py",
        "control_transport.py",
        "demand_arbiter.py",
        "simulation_adapters.py",
    ]
    root = REPOSITORY / "src/fleet_control_plane/fleet_control_plane"
    forbidden_imports = {"rclpy", "socket", "serial"}
    forbidden_tokens = {
        "create_publisher",
        "rbpodo",
        "send_action(",
        "digital_output",
        "analog_output",
    }
    for module in modules:
        source = (root / module).read_text(encoding="utf-8")
        content = source.lower()
        imported_modules = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module.split(".", 1)[0])
        assert not (forbidden_imports & imported_modules), module
        assert not {token for token in forbidden_tokens if token in content}, module


def test_sang_control_msgs_is_interface_only():
    root = REPOSITORY / "src/sang_control_msgs"
    executable_sources = list(root.rglob("*.py")) + list(root.rglob("*.cpp"))
    assert executable_sources == []
    assert (root / "action/ExecuteSkill.action").is_file()
    assert (root / "srv/QueryExecution.srv").is_file()
    assert (root / "srv/RenewLease.srv").is_file()
    assert (root / "srv/CancelExecution.srv").is_file()


def test_execute_skill_transport_carries_the_complete_signed_request():
    root = REPOSITORY / "src/sang_control_msgs"
    action = (root / "action/ExecuteSkill.action").read_text(encoding="utf-8")
    renewal = (root / "srv/RenewLease.srv").read_text(encoding="utf-8")
    cancellation = (root / "srv/CancelExecution.srv").read_text(encoding="utf-8")
    policy_demand = (root / "msg/PolicyDemand.msg").read_text(encoding="utf-8")

    assert "string execute_skill_request_json" in action
    assert "string execute_skill_request_sha256" in action
    assert "string renewed_execute_skill_request_json" in renewal
    assert "string renewed_execute_skill_request_sha256" in renewal
    assert "string cancel_execution_request_json" in cancellation
    assert "string cancel_execution_request_sha256" in cancellation
    assert "string signed_policy_demand_json" in policy_demand
    assert "string signed_policy_demand_sha256" in policy_demand


def test_unity_inventory_package_contains_no_command_or_network_transport():
    root = REPOSITORY / "unity/Sang.Observer"
    sources = list(root.rglob("*.cs"))
    assert sources
    forbidden_imports = {"System.Net", "System.Net.Sockets"}
    forbidden_tokens = {
        "UdpClient",
        "TcpClient",
        "HttpClient",
        "send(",
        "sendasync(",
        "create_publisher",
        "rbpodo",
        "digital_output",
        "analog_output",
    }
    for source_path in sources:
        source = source_path.read_text(encoding="utf-8")
        lowered = source.lower()
        assert not {name for name in forbidden_imports if f"using {name}" in source}
        assert not {token for token in forbidden_tokens if token.lower() in lowered}
