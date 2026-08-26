from datetime import datetime, timezone
import subprocess

from robot_agent.clock_sync import parse_chrony_tracking, sample_chrony
from robot_agent.heartbeat import build_heartbeat


TRACKING = (
    "5BBD5B9D,91.189.91.157,3,1787601086.567671852,-0.001745865,"
    "0.000188868,0.000190886,-0.014,0.001,0.058,0.073000170,"
    "0.000912936,64.7,Normal"
)


def test_chrony_tracking_is_projected_into_versioned_heartbeat():
    sampled_at = datetime(2026, 8, 24, 19, 51, tzinfo=timezone.utc)
    sync = parse_chrony_tracking(TRACKING, sampled_at=sampled_at)

    assert sync["state"] == "locked"
    assert sync["stratum"] == 3
    assert sync["system_offset_ms"] == -1.745865
    payload = build_heartbeat(
        agent_id="processing-lab1",
        agent_name="Lab1",
        robots=[],
        storage={},
        sync=sync,
        sent_at=sampled_at,
    )
    assert payload["sync"] == sync


def test_chrony_sampler_returns_bounded_unknown_instead_of_stderr():
    def failed(*args, **kwargs):
        del args, kwargs
        return subprocess.CompletedProcess([], 1, stdout="", stderr="secret detail")

    result = sample_chrony("127.0.0.1", run=failed)

    assert result["state"] == "unknown"
    assert result["error_code"] == "query_failed"
    assert "secret" not in str(result)
