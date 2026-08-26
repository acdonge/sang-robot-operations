import asyncio
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import struct
import tarfile

from mcap.writer import CompressionType, Writer
from fastapi.testclient import TestClient
import pytest

from fleet_control_plane.app import create_app
from fleet_control_plane.backup import create_backup, verify_backup
from fleet_control_plane.lerobot_derivation import (
    CameraObservation,
    DatasetDerivationCatalog,
    DerivationError,
    JointObservation,
    decode_joint_state,
    prepare_derivation,
    run_derivation,
)
from fleet_control_plane.recording_storage import FilesystemObjectStore, RecordingCatalog
from fleet_control_plane.store import FleetStore


NOW = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)
SESSION_ID = "derivation-source-001"
BASE_NS = 1_787_742_000_000_000_000
JOINT_NAMES = ("base", "shoulder", "elbow", "wrist1", "wrist2", "wrist3")


def _align(payload: bytearray, size: int) -> None:
    while (len(payload) - 4) % size:
        payload.append(0)


def _string(payload: bytearray, value: str, endian: str) -> None:
    _align(payload, 4)
    raw = value.encode("utf-8") + b"\x00"
    payload.extend(struct.pack(f"{endian}I", len(raw)))
    payload.extend(raw)


def _header(payload: bytearray, timestamp_ns: int, frame_id: str, endian: str) -> None:
    _align(payload, 4)
    payload.extend(
        struct.pack(
            f"{endian}iI",
            timestamp_ns // 1_000_000_000,
            timestamp_ns % 1_000_000_000,
        )
    )
    _string(payload, frame_id, endian)


def joint_state_cdr(timestamp_ns: int, index: int, little_endian: bool = True) -> bytes:
    endian = "<" if little_endian else ">"
    payload = bytearray(b"\x00\x01\x00\x00" if little_endian else b"\x00\x00\x00\x00")
    _header(payload, timestamp_ns, "joint", endian)
    _align(payload, 4)
    payload.extend(struct.pack(f"{endian}I", len(JOINT_NAMES)))
    for name in JOINT_NAMES:
        _string(payload, name, endian)
    _align(payload, 4)
    positions = tuple(index + joint / 10 for joint in range(len(JOINT_NAMES)))
    payload.extend(struct.pack(f"{endian}I", len(positions)))
    payload.extend(struct.pack(f"{endian}{len(positions)}d", *positions))
    _align(payload, 4)
    payload.extend(struct.pack(f"{endian}I", 0))
    _align(payload, 4)
    payload.extend(struct.pack(f"{endian}I", 0))
    return bytes(payload)


def compressed_image_cdr(timestamp_ns: int, index: int) -> bytes:
    payload = bytearray(b"\x00\x01\x00\x00")
    _header(payload, timestamp_ns, "camera", "<")
    _string(payload, "jpeg", "<")
    _align(payload, 4)
    jpeg = b"\xff\xd8" + bytes([index]) * 12 + b"\xff\xd9"
    payload.extend(struct.pack("<I", len(jpeg)))
    payload.extend(jpeg)
    return bytes(payload)


def mcap_payload() -> bytes:
    output = BytesIO()
    writer = Writer(output, compression=CompressionType.NONE)
    writer.start(profile="ros2")
    joint_schema = writer.register_schema(
        "sensor_msgs/msg/JointState",
        "ros2msg",
        b"std_msgs/Header header\nstring[] name\nfloat64[] position\nfloat64[] velocity\nfloat64[] effort\n",
    )
    camera_schema = writer.register_schema(
        "sensor_msgs/msg/CompressedImage",
        "ros2msg",
        b"std_msgs/Header header\nstring format\nuint8[] data\n",
    )
    joint_channel = writer.register_channel("/rb5/joint_states", "cdr", joint_schema)
    camera_channel = writer.register_channel("/rb5/camera/compressed", "cdr", camera_schema)
    for index in range(5):
        timestamp_ns = BASE_NS + index * 100_000_000
        writer.add_message(
            joint_channel,
            log_time=timestamp_ns + 1_000_000,
            publish_time=timestamp_ns,
            sequence=index,
            data=joint_state_cdr(timestamp_ns, index),
        )
        writer.add_message(
            camera_channel,
            log_time=timestamp_ns + 2_000_000,
            publish_time=timestamp_ns,
            sequence=index,
            data=compressed_image_cdr(timestamp_ns, index),
        )
    writer.finish()
    return output.getvalue()


def seed_source(tmp_path: Path):
    database = tmp_path / "fleet.db"
    FleetStore(database)
    objects = FilesystemObjectStore(tmp_path / "objects")
    mcap = mcap_payload()
    mcap_sha = hashlib.sha256(mcap).hexdigest()
    manifest_json = json.dumps({"session_id": SESSION_ID}, separators=(",", ":"))
    manifest_sha = hashlib.sha256(manifest_json.encode()).hexdigest()
    quality = b'{"state":"PASS"}\n'
    quality_sha = hashlib.sha256(quality).hexdigest()
    asyncio.run(
        objects.put_bytes(
            f"sessions/{SESSION_ID}/bag_0.mcap",
            mcap,
            sha256=mcap_sha,
        )
    )
    asyncio.run(
        objects.put_bytes(
            f"sessions/{SESSION_ID}/session-manifest.json",
            manifest_json.encode(),
            sha256=manifest_sha,
        )
    )
    asyncio.run(
        objects.put_bytes(
            f"quality-reports/{SESSION_ID}/{quality_sha}.json",
            quality,
            sha256=quality_sha,
        )
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO agents (
                agent_id, agent_name, sw_version, sent_at, received_at, payload_json
            ) VALUES ('test-agent', 'Test Agent', '0.1.0', ?, ?, '{}')
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO agent_registrations (
                agent_id, display_name, identity_uri, enabled, revision,
                created_at, updated_at
            ) VALUES ('test-agent', 'Test Agent', 'urn:sang:agent:test-agent', 1, 1, ?, ?)
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
        for asset_id, asset_type, source_key in (
            ("rb5-asset", "robot", None),
            ("camera-asset", "camera", "wrist"),
        ):
            connection.execute(
                """
                INSERT INTO assets (
                    asset_id, asset_type, display_name, node_id, agent_id,
                    robot_id, source_key, capabilities_json, created_at,
                    runtime_kind, revision, lifecycle_state, updated_at
                ) VALUES (?, ?, ?, NULL, 'test-agent', 'rb5-test', ?, '[]', ?,
                          'physical', 1, 'active', ?)
                """,
                (
                    asset_id,
                    asset_type,
                    asset_id,
                    source_key,
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )
        connection.execute(
            """
            INSERT INTO recording_sessions (
                session_id, artifact_id, state, manifest_schema_version,
                manifest_sha256, manifest_json, profile_id, agent_id, robot_id,
                run_id, task_definition_id, task_definition_version,
                started_at, finished_at, record_start_ns, record_end_ns,
                source_start_ns, source_end_ns, total_size_bytes,
                expected_file_count, completed_file_count, object_prefix,
                created_by, created_at, updated_at, completed_at
            ) VALUES (?, ?, 'COMPLETE', '1.0', ?, ?, 'test-profile',
                      'test-agent', 'rb5-test', NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?,
                      1, 1, ?, NULL, ?, ?, ?)
            """,
            (
                SESSION_ID,
                f"mcap:{SESSION_ID}",
                manifest_sha,
                manifest_json,
                NOW.isoformat(),
                NOW.isoformat(),
                BASE_NS,
                BASE_NS + 500_000_000,
                BASE_NS,
                BASE_NS + 400_000_000,
                len(mcap),
                f"sessions/{SESSION_ID}",
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO recording_artifacts (
                session_id, relative_path, object_key, media_type,
                expected_size_bytes, expected_sha256, state, stored_at
            ) VALUES (?, 'bag_0.mcap', ?, 'application/octet-stream', ?, ?, 'COMPLETE', ?)
            """,
            (
                SESSION_ID,
                f"sessions/{SESSION_ID}/bag_0.mcap",
                len(mcap),
                mcap_sha,
                NOW.isoformat(),
            ),
        )
        for values in (
            (
                "joints",
                "/rb5/joint_states",
                "sensor_msgs/msg/JointState",
                "rb5-asset",
                "vendor-state",
            ),
            (
                "wrist",
                "/rb5/camera/compressed",
                "sensor_msgs/msg/CompressedImage",
                "camera-asset",
                "camera-calibration",
            ),
        ):
            connection.execute(
                """
                INSERT INTO recording_topics (
                    session_id, topic_key, topic_name, message_type, asset_id,
                    calibration_ref, timestamp_provenance, message_count,
                    source_start_ns, source_end_ns
                ) VALUES (?, ?, ?, ?, ?, ?, 'host clock', 5, ?, ?)
                """,
                (SESSION_ID, *values, BASE_NS, BASE_NS + 400_000_000),
            )
        connection.execute(
            """
            INSERT INTO recording_quality_reports (
                session_id, state, report_schema_version, policy_sha256,
                report_sha256, report_size_bytes, object_key, clock_state,
                stream_count, pass_stream_count, warning_stream_count,
                fail_stream_count, failed_check_count, warning_check_count,
                summary_json, analyzed_at
            ) VALUES (?, 'PASS', '1.0', ?, ?, ?, ?, 'LOCKED', 2, 2, 0, 0, 0, 0, '{}', ?)
            """,
            (
                SESSION_ID,
                "a" * 64,
                quality_sha,
                len(quality),
                f"quality-reports/{SESSION_ID}/{quality_sha}.json",
                NOW.isoformat(),
            ),
        )
    robot_manifest = tmp_path / "robot.yaml"
    robot_manifest.write_text(
        """
robot:
  robot_id: rb5-test
interfaces:
  state:
    - key: observation.state
      topics: [/rb5/joint_states]
      feature_names: [base, shoulder, elbow, wrist1, wrist2, wrist3]
      shape: [6]
  cameras:
    - key: wrist
      topic: /rb5/camera/compressed
      width: 2
      height: 2
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return database, objects, robot_manifest


def prepared_source(tmp_path: Path):
    database, objects, robot_manifest = seed_source(tmp_path)
    prepared = prepare_derivation(
        recordings=RecordingCatalog(database),
        object_store=objects,
        session_id=SESSION_ID,
        robot_manifest_path=robot_manifest,
        state_topic_key="joints",
        camera_topic_keys=["wrist"],
        start_offset_ms=0,
        end_offset_ms=300,
        fps=10,
        task_text="Observe RB5",
    )
    return database, objects, prepared


@pytest.mark.parametrize("little_endian", [True, False])
def test_joint_state_decoder_preserves_names_positions_and_timestamps(little_endian):
    result = decode_joint_state(
        joint_state_cdr(BASE_NS, 2, little_endian),
        log_time_ns=BASE_NS + 1_000_000,
    )
    assert result.names == JOINT_NAMES
    assert result.positions == pytest.approx((2.0, 2.1, 2.2, 2.3, 2.4, 2.5))
    assert result.source_time_ns == BASE_NS
    assert result.log_time_ns == BASE_NS + 1_000_000


def test_prepare_requires_quality_pass(tmp_path):
    database, objects, robot_manifest = seed_source(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE recording_quality_reports SET state = 'FAIL' WHERE session_id = ?",
            (SESSION_ID,),
        )
    with pytest.raises(DerivationError, match="quality PASS"):
        prepare_derivation(
            recordings=RecordingCatalog(database),
            object_store=objects,
            session_id=SESSION_ID,
            robot_manifest_path=robot_manifest,
            state_topic_key="joints",
            camera_topic_keys=["wrist"],
            start_offset_ms=0,
            end_offset_ms=300,
            fps=10,
            task_text="Observe RB5",
        )


class FakeWriter:
    def __init__(self, root, prepared):
        self.root = root
        self.prepared = prepared
        self.frames = []
        root.mkdir(parents=True)

    def add_frame(self, frame):
        assert isinstance(frame.observations["joints"], JointObservation)
        assert isinstance(frame.observations["wrist"], CameraObservation)
        self.frames.append(frame)

    def finalize(self):
        assert len(self.frames) == 4
        (self.root / "meta").mkdir()
        (self.root / "data/chunk-000").mkdir(parents=True)
        (self.root / "videos/chunk-000/wrist").mkdir(parents=True)
        (self.root / "meta/info.json").write_text(
            json.dumps(
                {
                    "codebase_version": "v3.0",
                    "features": {"observation.state": {}, "observation.images.wrist": {}},
                }
            ),
            encoding="utf-8",
        )
        (self.root / "data/chunk-000/file-000.parquet").write_bytes(b"parquet")
        (self.root / "videos/chunk-000/wrist/file-000.mp4").write_bytes(b"video")


def test_derivation_is_idempotent_immutable_and_included_in_backup(tmp_path):
    database, objects, prepared = prepared_source(tmp_path)
    catalog = DatasetDerivationCatalog(database)
    result = run_derivation(
        prepared=prepared,
        catalog=catalog,
        object_store=objects,
        work_root=tmp_path / "work",
        writer_factory=FakeWriter,
        now=lambda: NOW,
    )
    assert result["state"] == "COMPLETE"
    assert result["frame_count"] == 4
    assert result["episode_count"] == 1
    assert result["request"]["safety"]["action_feature"] is False
    archive = objects.open_path(
        result["output_object_key"],
        size_bytes=result["output_size_bytes"],
        sha256=result["output_sha256"],
    )
    with tarfile.open(archive) as stream:
        names = stream.getnames()
        assert "sang-derivation.json" in names
        assert "meta/info.json" in names
        manifest = json.load(stream.extractfile("sang-derivation.json"))
    assert manifest["action_feature"] is False
    assert manifest["frame_count"] == 4
    assert manifest["maximum_absolute_skew_ns"] == {"joints": 0, "wrist": 0}

    replay = run_derivation(
        prepared=prepared,
        catalog=catalog,
        object_store=objects,
        work_root=tmp_path / "work",
        writer_factory=lambda *_: pytest.fail("idempotent replay must not reconvert"),
        now=lambda: NOW,
    )
    assert replay["derivation_id"] == result["derivation_id"]
    assert replay["revision"] == result["revision"]

    client = TestClient(
        create_app(
            FleetStore(database),
            auth_enabled=False,
            object_store=objects,
        )
    )
    listed = client.get(f"/api/v1/dataset-derivations?session_id={SESSION_ID}")
    assert listed.status_code == 200
    assert [item["derivation_id"] for item in listed.json()] == [result["derivation_id"]]
    detail = client.get(f"/api/v1/dataset-derivations/{result['derivation_id']}")
    assert detail.status_code == 200
    downloaded = client.get(
        f"/api/v1/dataset-derivations/{result['derivation_id']}/download"
    )
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == result["output_sha256"]

    backup = tmp_path / "backup"
    asyncio.run(
        create_backup(
            db_path=database,
            object_store_root=objects.root,
            output=backup,
        )
    )
    verified = verify_backup(backup)
    assert verified["passed"] is True
    assert verified["object_count"] == 5
