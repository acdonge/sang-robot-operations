from dataclasses import FrozenInstanceError
import math

import pytest

from robot_agent.health import (
    CAMERA_FORMAT_ALIASES,
    HealthSourceConfig,
    HealthTopicConfig,
    HealthTracker,
    MAX_RATE_SAMPLES,
    camera_format_is_valid,
    joint_sample_is_valid,
)


JOINT_NAMES = ("shoulder", "elbow", "wrist", "gripper")


def topic(
    name: str,
    *,
    freshness_ms: int = 1000,
    minimum_rate_hz: float | None = 10.0,
) -> HealthTopicConfig:
    return HealthTopicConfig(name, freshness_ms, minimum_rate_hz)


def source(
    key: str,
    *topics: HealthTopicConfig,
    required: bool = True,
) -> HealthSourceConfig:
    return HealthSourceConfig(key, required, tuple(topics))


def record_rate(
    tracker: HealthTracker,
    topic_name: str,
    *timestamps: float,
    valid: bool = True,
) -> None:
    for timestamp in timestamps:
        tracker.record(
            topic_name,
            valid=valid,
            received_monotonic=timestamp,
        )


def test_joint_validator_accepts_exact_finite_sample_without_range_policy():
    assert joint_sample_is_valid(
        JOINT_NAMES,
        JOINT_NAMES,
        (-1_000_000.0, 0, 1.5, 1_000_000.0),
    )


@pytest.mark.parametrize(
    ("names", "values"),
    [
        (("elbow", "shoulder", "wrist", "gripper"), (0.0,) * 4),
        (JOINT_NAMES[:-1], (0.0,) * 3),
        ((*JOINT_NAMES, "extra"), (0.0,) * 5),
        (JOINT_NAMES, (0.0,) * 3),
        (("shoulder", "elbow", "wrist", "wrist"), (0.0,) * 4),
    ],
)
def test_joint_validator_rejects_name_order_and_length_mismatches(names, values):
    assert not joint_sample_is_valid(JOINT_NAMES, names, values)


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf, True, "1.0"])
def test_joint_validator_rejects_non_finite_or_non_numeric_values(invalid):
    values = (0.0, 0.0, 0.0, invalid)

    assert not joint_sample_is_valid(JOINT_NAMES, JOINT_NAMES, values)


@pytest.mark.parametrize(
    "alias",
    ["jpeg", "JPG", " mjpg ", "rgb8; jpeg compressed bgr8"],
)
def test_camera_validator_accepts_configured_aliases(alias):
    assert camera_format_is_valid(alias)


@pytest.mark.parametrize(
    "message_format",
    ["", "png", "mjpeg", "16UC1; compressedDepth png", None, b"jpeg"],
)
def test_camera_validator_rejects_uncontracted_formats(message_format):
    assert not camera_format_is_valid(message_format)


def test_camera_alias_contract_is_explicit_and_immutable():
    assert CAMERA_FORMAT_ALIASES == {"jpeg", "jpg", "mjpg"}


def test_structural_validator_result_drives_invalid_sample_health():
    tracker = HealthTracker(
        (source("state", topic("/state", minimum_rate_hz=5.0)),)
    )
    record_rate(tracker, "/state", 1.0, 1.1)
    valid = joint_sample_is_valid(
        JOINT_NAMES,
        tuple(reversed(JOINT_NAMES)),
        (0.0,) * len(JOINT_NAMES),
    )
    tracker.record("/state", valid=valid, received_monotonic=1.2)

    measured = tracker.snapshot(now_monotonic=1.2).sources["state"].topics[
        "/state"
    ]

    assert measured.sequence == 3
    assert measured.rate_ok is True
    assert measured.last_sample_valid is False
    assert measured.fresh is False


def test_all_required_sources_are_healthy_with_valid_fresh_rate_data():
    tracker = HealthTracker(
        (
            source("state", topic("/left"), topic("/right")),
            source("camera", topic("/camera", minimum_rate_hz=5.0)),
        )
    )
    for name in ("/left", "/right", "/camera"):
        record_rate(tracker, name, 10.0, 10.05)

    result = tracker.snapshot(now_monotonic=10.1)

    assert result.sources_fresh is True
    assert result.rates_ok is True
    assert result.stale_required == ()
    assert result.rate_not_ok_required == ()
    assert result.sources["state"].rate_ok is True
    assert result.sources["state"].topics["/left"].sequence == 2
    assert result.sources["state"].topics["/left"].age_ms == pytest.approx(50.0)
    assert result.sources["state"].topics["/left"].rate_hz == pytest.approx(20.0)


def test_optional_idle_source_does_not_degrade_required_health():
    tracker = HealthTracker(
        (
            source("state", topic("/state")),
            source("action", topic("/action"), required=False),
        )
    )
    record_rate(tracker, "/state", 1.0, 1.05)

    result = tracker.snapshot(now_monotonic=1.1)

    assert result.sources["action"].fresh is False
    assert result.sources["action"].rate_ok is None
    assert result.sources_fresh is True
    assert result.rates_ok is True


def test_undecodable_required_sample_is_received_but_not_fresh():
    tracker = HealthTracker((source("state", topic("/state", minimum_rate_hz=5.0)),))
    record_rate(tracker, "/state", 1.0, 1.1)
    tracker.record("/state", valid=False, received_monotonic=1.2)

    result = tracker.snapshot(now_monotonic=1.2)
    measured = result.sources["state"].topics["/state"]

    assert measured.sequence == 3
    assert measured.last_sample_valid is False
    assert measured.rate_ok is True
    assert measured.fresh is False
    assert result.sources_fresh is False
    assert result.rates_ok is True


def test_required_source_never_observed_is_not_fresh_or_rates_ok():
    tracker = HealthTracker((source("state", topic("/state")),))

    result = tracker.snapshot(now_monotonic=10.0)
    measured = result.sources["state"].topics["/state"]

    assert measured.sequence == 0
    assert measured.age_ms is None
    assert measured.rate_hz is None
    assert measured.rate_ok is None
    assert measured.last_sample_valid is None
    assert result.sources_fresh is False
    assert result.rates_ok is False
    assert result.rate_not_ok_required == ("state",)


def test_rate_becomes_unmeasurable_after_window_silence_even_if_still_fresh():
    tracker = HealthTracker(
        (source("state", topic("/state", freshness_ms=10000)),)
    )
    tracker.record(
        "/state",
        valid=True,
        received_monotonic=0.0,
        rate_timestamp_s=100.0,
    )
    tracker.record(
        "/state",
        valid=True,
        received_monotonic=0.1,
        rate_timestamp_s=100.1,
    )

    result = tracker.snapshot(now_monotonic=5.2)
    measured = result.sources["state"].topics["/state"]

    assert measured.fresh is True
    assert measured.rate_hz is None
    assert measured.rate_ok is None
    assert result.sources_fresh is True
    assert result.rates_ok is False


def test_common_rate_timestamps_remove_subscriber_receipt_jitter():
    first = HealthTracker((source("state", topic("/state")),))
    second = HealthTracker((source("state", topic("/state")),))
    rate_timestamps = (100.0, 100.09, 100.18)
    for received, stamped in zip(
        (10.0, 10.08, 10.21), rate_timestamps, strict=True
    ):
        first.record(
            "/state",
            valid=True,
            received_monotonic=received,
            rate_timestamp_s=stamped,
        )
    for received, stamped in zip(
        (20.0, 20.12, 20.19), rate_timestamps, strict=True
    ):
        second.record(
            "/state",
            valid=True,
            received_monotonic=received,
            rate_timestamp_s=stamped,
        )

    first_health = first.snapshot(now_monotonic=10.25).sources["state"].topics[
        "/state"
    ]
    second_health = second.snapshot(now_monotonic=20.25).sources["state"].topics[
        "/state"
    ]

    assert first_health.rate_hz == pytest.approx(100.0 / 9.0)
    assert first_health.rate_hz == second_health.rate_hz
    assert first_health.rate_ok is second_health.rate_ok is True
    assert first_health.age_ms == pytest.approx(40.0)
    assert second_health.age_ms == pytest.approx(60.0)


def test_rate_window_is_evaluated_in_the_common_timestamp_domain():
    tracker = HealthTracker(
        (source("state", topic("/state", minimum_rate_hz=0.4)),)
    )
    for received, stamped in (
        (1.0, 100.0),
        (1.1, 104.0),
        (1.2, 106.0),
    ):
        tracker.record(
            "/state",
            valid=True,
            received_monotonic=received,
            rate_timestamp_s=stamped,
        )

    measured = tracker.snapshot(now_monotonic=1.2).sources["state"].topics[
        "/state"
    ]

    assert measured.sequence == 3
    assert measured.rate_hz == pytest.approx(0.5)
    assert measured.rate_ok is True


def test_invalid_stamped_event_counts_for_rate_but_not_freshness():
    tracker = HealthTracker(
        (source("state", topic("/state", minimum_rate_hz=5.0)),)
    )
    for received, stamped, valid in (
        (1.0, 100.0, True),
        (1.2, 100.1, True),
        (1.4, 100.2, False),
    ):
        tracker.record(
            "/state",
            valid=valid,
            received_monotonic=received,
            rate_timestamp_s=stamped,
        )

    measured = tracker.snapshot(now_monotonic=1.4).sources["state"].topics[
        "/state"
    ]

    assert measured.sequence == 3
    assert measured.rate_hz == pytest.approx(10.0)
    assert measured.rate_ok is True
    assert measured.fresh is False


def test_long_receipt_gap_resets_rate_history_before_stream_resumes():
    tracker = HealthTracker((source("state", topic("/state")),))
    tracker.record(
        "/state",
        valid=True,
        received_monotonic=0.0,
        rate_timestamp_s=100.0,
    )
    tracker.record(
        "/state",
        valid=True,
        received_monotonic=0.1,
        rate_timestamp_s=100.1,
    )
    tracker.record(
        "/state",
        valid=True,
        received_monotonic=6.0,
        rate_timestamp_s=100.2,
    )

    measured = tracker.snapshot(now_monotonic=6.0).sources["state"].topics[
        "/state"
    ]

    assert measured.sequence == 3
    assert measured.fresh is True
    assert measured.rate_hz is None
    assert measured.rate_ok is None


def test_rate_clock_discontinuity_restarts_only_rate_estimation():
    tracker = HealthTracker((source("state", topic("/state")),))
    tracker.record(
        "/state",
        valid=True,
        received_monotonic=1.0,
        rate_timestamp_s=100.0,
    )
    tracker.record(
        "/state",
        valid=True,
        received_monotonic=1.1,
        rate_timestamp_s=100.1,
    )
    tracker.record(
        "/state",
        valid=True,
        received_monotonic=1.2,
        rate_timestamp_s=50.0,
    )

    measured = tracker.snapshot(now_monotonic=1.2).sources["state"].topics[
        "/state"
    ]

    assert measured.sequence == 3
    assert measured.fresh is True
    assert measured.rate_hz is None
    assert measured.rate_ok is None


def test_low_measured_rate_degrades_rates_without_changing_freshness():
    tracker = HealthTracker(
        (source("state", topic("/state", freshness_ms=2000)),)
    )
    record_rate(tracker, "/state", 0.0, 1.0)

    result = tracker.snapshot(now_monotonic=1.0)
    measured = result.sources["state"].topics["/state"]

    assert measured.fresh is True
    assert measured.rate_hz == pytest.approx(1.0)
    assert measured.rate_ok is False
    assert result.sources_fresh is True
    assert result.rates_ok is False


def test_multi_topic_source_requires_every_topic_to_pass():
    tracker = HealthTracker(
        (source("state", topic("/left"), topic("/right")),)
    )
    record_rate(tracker, "/left", 0.0, 0.05)
    record_rate(tracker, "/right", 0.0, 0.2)

    result = tracker.snapshot(now_monotonic=0.2)
    state = result.sources["state"]

    assert state.fresh is True
    assert state.topics["/left"].rate_ok is True
    assert state.topics["/right"].rate_ok is False
    assert state.rate_ok is False
    assert result.rates_ok is False


def test_required_source_without_rate_contract_is_rate_neutral():
    tracker = HealthTracker(
        (source("state", topic("/state", minimum_rate_hz=None)),)
    )
    tracker.record("/state", valid=True, received_monotonic=1.0)

    result = tracker.snapshot(now_monotonic=1.1)

    assert result.sources["state"].fresh is True
    assert result.sources["state"].rate_ok is None
    assert result.rates_ok is True


@pytest.mark.parametrize("timestamp", [math.nan, math.inf, -math.inf, True])
def test_invalid_event_timestamps_are_rejected(timestamp):
    tracker = HealthTracker((source("state", topic("/state")),))

    with pytest.raises(ValueError, match="received_monotonic must be finite"):
        tracker.record("/state", valid=True, received_monotonic=timestamp)


@pytest.mark.parametrize("timestamp", [math.nan, math.inf, -math.inf, True])
def test_invalid_rate_timestamps_are_rejected(timestamp):
    tracker = HealthTracker((source("state", topic("/state")),))

    with pytest.raises(ValueError, match="rate_timestamp_s must be finite"):
        tracker.record(
            "/state",
            valid=True,
            received_monotonic=1.0,
            rate_timestamp_s=timestamp,
        )


def test_backwards_event_and_evaluation_timestamps_are_rejected():
    tracker = HealthTracker((source("state", topic("/state")),))
    tracker.record("/state", valid=True, received_monotonic=2.0)

    with pytest.raises(ValueError, match="moved backwards"):
        tracker.record("/state", valid=True, received_monotonic=1.0)
    with pytest.raises(ValueError, match="precedes the last event"):
        tracker.snapshot(now_monotonic=1.0)
    with pytest.raises(ValueError, match="now_monotonic must be finite"):
        tracker.snapshot(now_monotonic=math.nan)


def test_unknown_topic_is_rejected_without_changing_known_topic():
    tracker = HealthTracker((source("state", topic("/state")),))

    with pytest.raises(KeyError, match="unknown health topic"):
        tracker.record("/missing", valid=True, received_monotonic=1.0)

    assert tracker.snapshot(now_monotonic=1.0).sources["state"].topics[
        "/state"
    ].sequence == 0


def test_rate_history_is_bounded_and_results_are_immutable():
    tracker = HealthTracker((source("state", topic("/state")),))
    for index in range(MAX_RATE_SAMPLES + 20):
        tracker.record(
            "/state",
            valid=True,
            received_monotonic=index / 1000.0,
        )

    result = tracker.snapshot(
        now_monotonic=(MAX_RATE_SAMPLES + 19) / 1000.0
    )

    assert len(tracker._topics["/state"].recent) == MAX_RATE_SAMPLES
    assert result.sources["state"].topics["/state"].sequence == (
        MAX_RATE_SAMPLES + 20
    )
    with pytest.raises(TypeError):
        result.sources["other"] = result.sources["state"]
    with pytest.raises(FrozenInstanceError):
        result.sources_fresh = False
