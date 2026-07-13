import math
from concurrent.futures import ThreadPoolExecutor

from recorder_core.buffers import LatestValueBuffer


def test_latest_value_buffer_tracks_age_and_sequence():
    buffer = LatestValueBuffer()

    assert buffer.snapshot() is None
    assert math.isinf(buffer.age_ms(now_monotonic=10.0))

    first = buffer.update("first", received_monotonic=10.0)
    second = buffer.update("second", received_monotonic=10.25)

    assert first.seq == 1
    assert second.seq == 2
    assert buffer.snapshot() == second
    assert buffer.age_ms(now_monotonic=10.5) == 250.0


def test_latest_value_buffer_updates_are_thread_safe():
    buffer = LatestValueBuffer()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(buffer.update, range(200)))

    assert buffer.snapshot().seq == 200
