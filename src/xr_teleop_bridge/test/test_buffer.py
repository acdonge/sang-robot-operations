import math

from xr_teleop_bridge.buffer import LatestFrameBuffer
from xr_teleop_bridge.mock_sender import sample_frame


def test_buffer_is_latest_only_and_counts_replacements():
    buffer = LatestFrameBuffer()
    first = buffer.update(sample_frame(1, 0.0, "test"), 10.0)
    second = buffer.update(sample_frame(2, 0.1, "test"), 10.1)

    assert first.generation == 1
    assert second.generation == 2
    assert buffer.replacements == 1
    assert buffer.take_latest() == second
    assert buffer.take_latest() is None
    assert buffer.latest() == second
    assert buffer.age_ms(10.35) == 250.0


def test_empty_buffer_has_infinite_age():
    buffer = LatestFrameBuffer()

    assert buffer.latest() is None
    assert buffer.take_latest() is None
    assert math.isinf(buffer.age_ms(10.0))
