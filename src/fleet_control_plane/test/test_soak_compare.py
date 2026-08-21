import pytest

from fleet_control_plane.soak_compare import ComparisonError, compare_report_sets


TOPIC = "/robot/joint_states"


def report(*, cpu=10.0, ram=8192.0, rate=20.0, passed=True):
    return {
        "passed": passed,
        "agent_id": "agent-1",
        "robot_id": "robot-1",
        "system_stats": {
            "cpu_pct": {"p95": cpu},
            "ram_free_mib": {"minimum": ram},
        },
        "topic_stats": {
            TOPIC: {
                "required": True,
                "median_observed_rate_hz": rate,
            }
        },
    }


def test_gateway_comparison_passes_within_all_budgets():
    result = compare_report_sets(
        [report(cpu=value) for value in (9.0, 10.0, 11.0)],
        [report(cpu=value, ram=8000.0, rate=19.5) for value in (14.0, 15.0, 16.0)],
    )

    assert result["passed"] is True
    assert result["cpu_pct_p95_median"]["increase_percentage_points"] == 5.0
    assert result["topic_rate_deltas"][TOPIC]["regression_percent"] == 2.5
    assert result["dds_wire_traffic_measured"] is False


def test_gateway_comparison_fails_resource_and_rate_regressions():
    result = compare_report_sets(
        [report()] * 3,
        [report(cpu=25.0, ram=7000.0, rate=18.0)] * 3,
    )

    assert result["passed"] is False
    assert len(result["failures"]) == 3


def test_gateway_comparison_requires_three_passing_runs_per_condition():
    with pytest.raises(ComparisonError, match="exactly three"):
        compare_report_sets([report()], [report()])
    with pytest.raises(ComparisonError, match="must pass"):
        compare_report_sets([report()] * 3, [report(passed=False)] * 3)
