"""Bounded, indexed playback of JPEG camera observations from immutable MCAP."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
from pathlib import Path
import struct
import threading
from typing import Any

from mcap.exceptions import McapError
from mcap.reader import SeekingReader

from fleet_control_plane.recording_storage import (
    FilesystemObjectStore,
    RecordingCatalog,
)


COMPRESSED_IMAGE_TYPE = "sensor_msgs/msg/CompressedImage"
FRAME_STEP_MS = 100
MAX_CAMERA_VIEWS = 4
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_MCAP_RECORD_BYTES = 64 * 1024 * 1024
MAX_INDEXED_SESSIONS = 8
MAX_FRAME_CACHE_BYTES = 64 * 1024 * 1024


class PlaybackInvalid(RuntimeError):
    """The requested session, topic, offset, or serialized frame is invalid."""


class PlaybackNotFound(RuntimeError):
    """A requested playable camera or frame does not exist."""


class PlaybackUnavailable(RuntimeError):
    """The immutable session cannot currently be played."""


class PlaybackBusy(RuntimeError):
    """The bounded playback reader has no free read slot."""


@dataclass(frozen=True)
class CompressedImage:
    source_time_ns: int
    frame_id: str
    format: str
    data: bytes


@dataclass(frozen=True)
class RosHeader:
    source_time_ns: int
    frame_id: str


@dataclass(frozen=True)
class PlaybackFrame:
    data: bytes
    topic_key: str
    log_time_ns: int
    source_time_ns: int
    frame_offset_ms: int
    source_offset_ms: int
    skew_ms: int


@dataclass(frozen=True)
class McapSource:
    path: Path
    inode: int
    mtime_ns: int
    size_bytes: int
    start_time_ns: int
    end_time_ns: int
    topics: frozenset[str]


@dataclass(frozen=True)
class PlaybackIndex:
    session_id: str
    manifest_sha256: str
    start_time_ns: int
    end_time_ns: int
    cameras: tuple[dict[str, Any], ...]
    sources: tuple[McapSource, ...]


class _CdrReader:
    def __init__(self, payload: bytes) -> None:
        if len(payload) < 4:
            raise PlaybackInvalid("CompressedImage CDR payload is truncated")
        kind = int.from_bytes(payload[:2], byteorder="big")
        if kind == 0:
            self.endian = ">"
        elif kind == 1:
            self.endian = "<"
        else:
            raise PlaybackInvalid("CompressedImage uses an unsupported CDR encoding")
        self.payload = payload
        self.offset = 4
        self.origin = 4

    def _align(self, size: int) -> None:
        relative = self.offset - self.origin
        self.offset = self.origin + ((relative + size - 1) // size) * size

    def _read(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self.payload):
            raise PlaybackInvalid("CompressedImage CDR payload is truncated")
        value = self.payload[self.offset:end]
        self.offset = end
        return value

    def int32(self) -> int:
        self._align(4)
        return struct.unpack(f"{self.endian}i", self._read(4))[0]

    def uint32(self) -> int:
        self._align(4)
        return struct.unpack(f"{self.endian}I", self._read(4))[0]

    def string(self, *, maximum_bytes: int) -> str:
        length = self.uint32()
        if not 1 <= length <= maximum_bytes:
            raise PlaybackInvalid("CompressedImage CDR string length is invalid")
        raw = self._read(length)
        if raw[-1] != 0:
            raise PlaybackInvalid("CompressedImage CDR string is not terminated")
        try:
            return raw[:-1].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PlaybackInvalid("CompressedImage CDR string is not UTF-8") from exc

    def byte_sequence(self, *, maximum_bytes: int) -> bytes:
        length = self.uint32()
        if not 1 <= length <= maximum_bytes:
            raise PlaybackInvalid("CompressedImage data length is invalid")
        return self._read(length)


def decode_compressed_image(payload: bytes) -> CompressedImage:
    reader = _CdrReader(payload)
    header = _decode_header(reader)
    image_format = reader.string(maximum_bytes=256)
    image = reader.byte_sequence(maximum_bytes=MAX_FRAME_BYTES)
    normalized = image_format.lower()
    if "jpeg" not in normalized and "jpg" not in normalized:
        raise PlaybackInvalid("only JPEG CompressedImage playback is supported")
    if not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
        raise PlaybackInvalid("CompressedImage does not contain a complete JPEG")
    return CompressedImage(
        source_time_ns=header.source_time_ns,
        frame_id=header.frame_id,
        format=image_format,
        data=image,
    )


def _decode_header(reader: _CdrReader) -> RosHeader:
    seconds = reader.int32()
    nanoseconds = reader.uint32()
    if seconds < 0 or nanoseconds >= 1_000_000_000:
        raise PlaybackInvalid("ROS source timestamp is invalid")
    frame_id = reader.string(maximum_bytes=4096)
    return RosHeader(
        source_time_ns=seconds * 1_000_000_000 + nanoseconds,
        frame_id=frame_id,
    )


def decode_ros_header(payload: bytes) -> RosHeader:
    """Decode the leading std_msgs/Header shared by supported observations."""
    return _decode_header(_CdrReader(payload))


class RecordingPlayback:
    """Read nearest indexed camera frames without materializing an MCAP file."""

    def __init__(
        self,
        catalog: RecordingCatalog,
        object_store: FilesystemObjectStore,
        *,
        read_slots: int = 8,
    ) -> None:
        self.catalog = catalog
        self.object_store = object_store
        self._slots = threading.BoundedSemaphore(value=read_slots)
        self._lock = threading.Lock()
        self._indexes: OrderedDict[str, PlaybackIndex] = OrderedDict()
        self._frames: OrderedDict[tuple[str, str, int], PlaybackFrame] = OrderedDict()
        self._frame_cache_bytes = 0

    @staticmethod
    def _source_unchanged(source: McapSource) -> bool:
        try:
            stat = source.path.stat()
        except OSError:
            return False
        return (
            stat.st_ino == source.inode
            and stat.st_mtime_ns == source.mtime_ns
            and stat.st_size == source.size_bytes
            and source.path.is_file()
            and not source.path.is_symlink()
        )

    def _cached_index(self, session_id: str, manifest_sha256: str) -> PlaybackIndex | None:
        with self._lock:
            index = self._indexes.get(session_id)
            if index is None or index.manifest_sha256 != manifest_sha256:
                return None
            if not all(self._source_unchanged(source) for source in index.sources):
                self._indexes.pop(session_id, None)
                return None
            self._indexes.move_to_end(session_id)
            return index

    def _build_index(self, session: dict[str, Any]) -> PlaybackIndex:
        if session["state"] != "COMPLETE":
            raise PlaybackUnavailable("recording playback requires a complete session")
        cameras = tuple(
            topic
            for topic in session.get("topics", [])
            if topic.get("type") == COMPRESSED_IMAGE_TYPE
            and int(topic.get("message_count") or 0) > 0
        )
        if not cameras:
            raise PlaybackUnavailable("recording session has no JPEG camera topics")

        sources: list[McapSource] = []
        available_topics: set[str] = set()
        for item in session.get("artifacts", []):
            if item.get("state") != "COMPLETE" or not item.get("path", "").endswith(".mcap"):
                continue
            artifact = self.catalog.artifact(session["session_id"], item["path"])
            path = self.object_store.open_path(
                artifact["object_key"],
                size_bytes=artifact["expected_size_bytes"],
                sha256=artifact["expected_sha256"],
            )
            stat = path.stat()
            try:
                with path.open("rb") as stream:
                    summary = SeekingReader(
                        stream,
                        record_size_limit=MAX_MCAP_RECORD_BYTES,
                    ).get_summary()
            except (McapError, OSError, ValueError) as exc:
                raise PlaybackUnavailable("recording MCAP index cannot be read") from exc
            if summary is None or summary.statistics is None:
                raise PlaybackUnavailable("recording MCAP does not contain a seek index")
            source_topics: set[str] = set()
            for channel in summary.channels.values():
                schema = summary.schemas.get(channel.schema_id)
                if (
                    channel.message_encoding == "cdr"
                    and schema is not None
                    and schema.name == COMPRESSED_IMAGE_TYPE
                    and schema.encoding == "ros2msg"
                ):
                    source_topics.add(channel.topic)
            available_topics.update(source_topics)
            sources.append(
                McapSource(
                    path=path,
                    inode=stat.st_ino,
                    mtime_ns=stat.st_mtime_ns,
                    size_bytes=stat.st_size,
                    start_time_ns=summary.statistics.message_start_time,
                    end_time_ns=summary.statistics.message_end_time,
                    topics=frozenset(source_topics),
                )
            )
        if not sources:
            raise PlaybackUnavailable("recording session has no complete MCAP object")
        missing = [camera["key"] for camera in cameras if camera["name"] not in available_topics]
        if missing:
            raise PlaybackUnavailable(
                f"recording MCAP is missing declared camera topics: {', '.join(missing)}"
            )
        start_time_ns = min(source.start_time_ns for source in sources)
        end_time_ns = max(source.end_time_ns for source in sources)
        if end_time_ns < start_time_ns:
            raise PlaybackUnavailable("recording MCAP time range is invalid")
        return PlaybackIndex(
            session_id=session["session_id"],
            manifest_sha256=session["manifest_sha256"],
            start_time_ns=start_time_ns,
            end_time_ns=end_time_ns,
            cameras=cameras,
            sources=tuple(sorted(sources, key=lambda item: item.start_time_ns)),
        )

    def _index(self, session_id: str) -> PlaybackIndex:
        session = self.catalog.get_session(session_id)
        cached = self._cached_index(session_id, session["manifest_sha256"])
        if cached is not None:
            return cached
        index = self._build_index(session)
        with self._lock:
            self._indexes[session_id] = index
            self._indexes.move_to_end(session_id)
            while len(self._indexes) > MAX_INDEXED_SESSIONS:
                removed, _ = self._indexes.popitem(last=False)
                for key in [key for key in self._frames if key[0] == removed]:
                    self._frame_cache_bytes -= len(self._frames.pop(key).data)
        return index

    @staticmethod
    def _camera(index: PlaybackIndex, topic_key: str) -> dict[str, Any]:
        for camera in index.cameras:
            if camera["key"] == topic_key:
                return camera
        raise PlaybackNotFound("camera topic is not playable in this session")

    def describe(self, session_id: str) -> dict[str, Any]:
        index = self._index(session_id)
        duration_ms = math.ceil((index.end_time_ns - index.start_time_ns) / 1_000_000)
        return {
            "session_id": index.session_id,
            "timestamp_basis": "mcap_log_time",
            "start_time_ns": str(index.start_time_ns),
            "end_time_ns": str(index.end_time_ns),
            "duration_ms": duration_ms,
            "frame_step_ms": FRAME_STEP_MS,
            "max_camera_views": MAX_CAMERA_VIEWS,
            "cameras": [
                {
                    "key": camera["key"],
                    "topic": camera["name"],
                    "asset_id": camera["asset_id"],
                    "message_count": camera["message_count"],
                    "source_start_ns": str(camera.get("source_start_ns") or ""),
                    "source_end_ns": str(camera.get("source_end_ns") or ""),
                }
                for camera in index.cameras
            ],
        }

    def _cached_frame(
        self,
        session_id: str,
        topic_key: str,
        bucket_ms: int,
    ) -> PlaybackFrame | None:
        key = (session_id, topic_key, bucket_ms)
        with self._lock:
            frame = self._frames.get(key)
            if frame is not None:
                self._frames.move_to_end(key)
            return frame

    def _remember_frame(
        self,
        session_id: str,
        topic_key: str,
        bucket_ms: int,
        frame: PlaybackFrame,
    ) -> None:
        key = (session_id, topic_key, bucket_ms)
        with self._lock:
            replaced = self._frames.pop(key, None)
            if replaced is not None:
                self._frame_cache_bytes -= len(replaced.data)
            self._frames[key] = frame
            self._frame_cache_bytes += len(frame.data)
            while self._frames and self._frame_cache_bytes > MAX_FRAME_CACHE_BYTES:
                _, removed = self._frames.popitem(last=False)
                self._frame_cache_bytes -= len(removed.data)

    @staticmethod
    def _nearest_message(source: McapSource, topic: str, target_ns: int):
        if topic not in source.topics:
            return None
        try:
            with source.path.open("rb") as stream:
                reader = SeekingReader(
                    stream,
                    record_size_limit=MAX_MCAP_RECORD_BYTES,
                )
                candidates = []
                if target_ns >= source.start_time_ns:
                    previous = next(
                        reader.iter_messages(
                            topics=[topic],
                            start_time=source.start_time_ns,
                            end_time=min(target_ns + 1, source.end_time_ns + 1),
                            reverse=True,
                        ),
                        None,
                    )
                    if previous is not None:
                        candidates.append(previous)
                if target_ns <= source.end_time_ns:
                    following = next(
                        reader.iter_messages(
                            topics=[topic],
                            start_time=max(target_ns, source.start_time_ns),
                            end_time=source.end_time_ns + 1,
                        ),
                        None,
                    )
                    if following is not None:
                        candidates.append(following)
        except (McapError, OSError, ValueError) as exc:
            raise PlaybackUnavailable("recording MCAP frame cannot be read") from exc
        if not candidates:
            return None
        return min(candidates, key=lambda item: abs(item[2].log_time - target_ns))

    def frame(self, session_id: str, topic_key: str, offset_ms: int) -> PlaybackFrame:
        index = self._index(session_id)
        camera = self._camera(index, topic_key)
        duration_ms = math.ceil((index.end_time_ns - index.start_time_ns) / 1_000_000)
        if not 0 <= offset_ms <= duration_ms:
            raise PlaybackInvalid("playback offset is outside the session timeline")
        bucket_ms = min(duration_ms, ((offset_ms + FRAME_STEP_MS // 2) // FRAME_STEP_MS) * FRAME_STEP_MS)
        cached = self._cached_frame(session_id, topic_key, bucket_ms)
        if cached is not None:
            return cached
        if not self._slots.acquire(blocking=False):
            raise PlaybackBusy("recording playback is busy; retry the frame")
        try:
            target_ns = index.start_time_ns + bucket_ms * 1_000_000
            candidates = [
                candidate
                for source in index.sources
                if (candidate := self._nearest_message(source, camera["name"], target_ns))
                is not None
            ]
            if not candidates:
                raise PlaybackNotFound("camera has no frame in this recording")
            schema, channel, message = min(
                candidates,
                key=lambda item: abs(item[2].log_time - target_ns),
            )
            if (
                channel.topic != camera["name"]
                or channel.message_encoding != "cdr"
                or schema is None
                or schema.name != COMPRESSED_IMAGE_TYPE
            ):
                raise PlaybackInvalid("MCAP camera channel contract changed")
            decoded = decode_compressed_image(message.data)
            frame = PlaybackFrame(
                data=decoded.data,
                topic_key=topic_key,
                log_time_ns=message.log_time,
                source_time_ns=decoded.source_time_ns,
                frame_offset_ms=round((message.log_time - index.start_time_ns) / 1_000_000),
                source_offset_ms=round((decoded.source_time_ns - index.start_time_ns) / 1_000_000),
                skew_ms=round((message.log_time - target_ns) / 1_000_000),
            )
            self._remember_frame(session_id, topic_key, bucket_ms, frame)
            return frame
        finally:
            self._slots.release()
