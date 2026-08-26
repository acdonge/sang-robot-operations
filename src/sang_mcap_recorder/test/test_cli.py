import pytest

from sang_mcap_recorder.cli import parse_args


def test_clock_sync_timeout_must_be_positive():
    with pytest.raises(SystemExit):
        parse_args(["--profile", "profile.yaml", "--clock-sync-timeout-s", "0"])


def test_clock_sync_host_rejects_urls():
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--profile",
                "profile.yaml",
                "--clock-sync-host",
                "https://clock.example",
            ]
        )
