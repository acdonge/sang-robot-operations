import json
import math

import pytest

from xr_teleop_bridge.mock_sender import sample_frame
from xr_teleop_bridge.protocol import ProtocolError, decode_frame, encode_frame


def document(sequence=7):
    return json.loads(encode_frame(sample_frame(sequence, 0.5, "test_xr")))


def test_frame_round_trip_preserves_synchronized_input():
    original = sample_frame(7, 0.5, "test_xr")

    decoded = decode_frame(encode_frame(original).rstrip(b"\n"))

    assert decoded.sequence == 7
    assert decoded.source == "test_xr"
    assert decoded.frame_id == "xr_local"
    assert decoded.head.tracked
    assert decoded.left.connected
    assert decoded.right.trigger == pytest.approx(original.right.trigger)
    assert decoded.right.grip.position == pytest.approx(
        original.right.grip.position
    )


def test_quaternion_is_normalized_at_the_transport_boundary():
    payload = document()
    payload["head"]["orientation"] = [0.0, 0.0, 0.0, 2.0]

    decoded = decode_frame(json.dumps(payload))

    assert decoded.head.orientation == (0.0, 0.0, 0.0, 1.0)
    assert math.isclose(
        sum(value * value for value in decoded.head.orientation), 1.0
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda item: item.update(schema="unknown"), "schema must be"),
        (lambda item: item.update(sequence=True), "sequence must be an integer"),
        (lambda item: item.update(frame_id="/xr_local"), "frame_id must be relative"),
        (
            lambda item: item["head"].update(valid=False, tracked=True),
            "tracked cannot be true",
        ),
        (
            lambda item: item["left"].update(trigger=1.5),
            "left.trigger must be at most 1.0",
        ),
        (
            lambda item: item["right"]["grip"].update(
                orientation=[0.0, 0.0, 0.0, 0.0]
            ),
            "quaternion has zero magnitude",
        ),
    ],
)
def test_invalid_frames_are_rejected(mutation, message):
    payload = document()
    mutation(payload)

    with pytest.raises(ProtocolError, match=message):
        decode_frame(json.dumps(payload))


def test_non_finite_input_is_rejected_even_if_json_decoder_accepts_it():
    payload = document()
    payload["right"]["linear_velocity"][0] = float("nan")

    with pytest.raises(ProtocolError, match="must be finite"):
        decode_frame(json.dumps(payload))


def test_malformed_json_and_utf8_are_rejected():
    with pytest.raises(ProtocolError, match="not valid JSON"):
        decode_frame("{")
    with pytest.raises(ProtocolError, match="not valid UTF-8"):
        decode_frame(b"\xff")
