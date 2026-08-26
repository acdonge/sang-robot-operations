import struct

import pytest

from fleet_control_plane.recording_playback import (
    MAX_FRAME_BYTES,
    PlaybackInvalid,
    decode_compressed_image,
)


def compressed_image_cdr(
    *,
    timestamp_ns: int = 1_787_310_000_500_000_000,
    frame_id: str = "camera",
    image_format: str = "jpeg",
    image: bytes = b"\xff\xd8payload\xff\xd9",
    little_endian: bool = True,
) -> bytes:
    endian = "<" if little_endian else ">"
    payload = bytearray(b"\x00\x01\x00\x00" if little_endian else b"\x00\x00\x00\x00")
    payload.extend(
        struct.pack(
            f"{endian}iI",
            timestamp_ns // 1_000_000_000,
            timestamp_ns % 1_000_000_000,
        )
    )

    def add_string(value: str) -> None:
        raw = value.encode("utf-8") + b"\x00"
        payload.extend(struct.pack(f"{endian}I", len(raw)))
        payload.extend(raw)
        while (len(payload) - 4) % 4:
            payload.append(0)

    add_string(frame_id)
    add_string(image_format)
    payload.extend(struct.pack(f"{endian}I", len(image)))
    payload.extend(image)
    return bytes(payload)


@pytest.mark.parametrize("little_endian", [True, False])
def test_decode_compressed_image_preserves_jpeg_and_source_stamp(little_endian):
    source_ns = 1_787_310_000_500_000_000
    jpeg = b"\xff\xd8jpeg-body\xff\xd9"

    result = decode_compressed_image(
        compressed_image_cdr(
            timestamp_ns=source_ns,
            frame_id="d405_color",
            image_format="bgr8; jpeg compressed bgr8",
            image=jpeg,
            little_endian=little_endian,
        )
    )

    assert result.source_time_ns == source_ns
    assert result.frame_id == "d405_color"
    assert result.format == "bgr8; jpeg compressed bgr8"
    assert result.data == jpeg


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x00\x05\x00\x00",
        compressed_image_cdr(image_format="png"),
        compressed_image_cdr(image=b"not-a-jpeg"),
    ],
)
def test_decode_compressed_image_rejects_unsupported_or_truncated_payload(payload):
    with pytest.raises(PlaybackInvalid):
        decode_compressed_image(payload)


def test_decode_compressed_image_bounds_frame_size():
    payload = compressed_image_cdr(image=b"\xff\xd8x\xff\xd9")
    data_length_offset = payload.index(b"\xff\xd8") - 4
    changed = bytearray(payload)
    changed[data_length_offset : data_length_offset + 4] = struct.pack(
        "<I", MAX_FRAME_BYTES + 1
    )

    with pytest.raises(PlaybackInvalid, match="data length"):
        decode_compressed_image(bytes(changed))
