from datetime import datetime, timezone
import asyncio
from io import BytesIO
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import ssl
import struct
import threading
import time

from fastapi.testclient import TestClient
import pytest
from mcap.writer import CompressionType, Writer

from fleet_control_plane.app import create_app
from fleet_control_plane.auth import AuthManager, ServiceIdentityCreate
from fleet_control_plane.recording_storage import FilesystemObjectStore
from fleet_control_plane import recording_storage
from fleet_control_plane.recording_upload import (
    parse_args as parse_upload_args,
    read_manifest,
    request_json,
    verify_local_artifacts,
)
from fleet_control_plane.backup import (
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)
from fleet_control_plane.store import FleetStore


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def observed_heartbeat():
    topic = {
        "age_ms": 20.0,
        "rate_hz": 30.0,
        "minimum_rate_hz": 10.0,
        "rate_ok": True,
        "seq": 10,
        "fresh": True,
    }
    return {
        "schema_version": 1,
        "agent_id": "processing-lab1",
        "agent_name": "Processing Lab 1",
        "sw_version": "0.1.0",
        "sent_at": NOW.isoformat(),
        "storage": {"disk_free_gb": 100, "ram_free_gb": 20, "cpu_pct": 10},
        "datasets": {"count": 0, "total_episodes": 0, "total_size_mb": 0},
        "robots": [
            {
                "robot_id": "rb5-850e-01",
                "model": "rb5_850e",
                "fsm_state": "READY",
                "phase": "monitoring",
                "sources_fresh": True,
                "rates_ok": True,
                "sources": [
                    {
                        "key": "observation.state",
                        "kind": "state",
                        "required": True,
                        "freshness_ms": 1000,
                        "fresh": True,
                        "rate_ok": True,
                        "topics": [{**topic, "topic": "/rb5/joint_states"}],
                    },
                    {
                        "key": "wrist",
                        "kind": "camera",
                        "required": True,
                        "freshness_ms": 1000,
                        "fresh": True,
                        "rate_ok": True,
                        "topics": [{**topic, "topic": "/rb5/wrist/compressed"}],
                    },
                ],
                "fault": None,
                "recording": {
                    "active": False,
                    "episode_index": 0,
                    "episodes_total": 0,
                    "frames": 0,
                    "elapsed_s": 0,
                    "current_instruction": None,
                },
            }
        ],
    }


def configured_client(tmp_path, *, quality_policy_root=None):
    store = FleetStore(tmp_path / "fleet.db")
    objects = FilesystemObjectStore(tmp_path / "objects")
    app = create_app(
        store,
        now=lambda: NOW,
        auth_enabled=False,
        object_store=objects,
        quality_policy_root=quality_policy_root,
    )
    client = TestClient(app)
    assert client.post("/api/v1/agents/heartbeat", json=observed_heartbeat()).status_code == 202
    assert client.post(
        "/api/v1/config/processing-nodes",
        json={
            "node_id": "processing-node-lab1",
            "display_name": "Processing Lab 1",
            "agent_ids": ["processing-lab1"],
        },
    ).status_code == 201
    for asset in (
        {
            "asset_id": "rb5-arm-01",
            "asset_type": "robot",
            "display_name": "RB5-850E",
            "node_id": "processing-node-lab1",
            "binding": {
                "agent_id": "processing-lab1",
                "robot_id": "rb5-850e-01",
                "source_key": None,
            },
            "capabilities": ["telemetry"],
        },
        {
            "asset_id": "rb5-wrist-camera-01",
            "asset_type": "camera",
            "display_name": "RB5 Wrist D405",
            "node_id": "processing-node-lab1",
            "binding": {
                "agent_id": "processing-lab1",
                "robot_id": "rb5-850e-01",
                "source_key": "wrist",
            },
            "capabilities": ["rgb", "depth"],
        },
    ):
        assert client.post("/api/v1/config/assets", json=asset).status_code == 201
    assert client.post(
        "/api/v1/config/agent-registrations",
        json={
            "agent_id": "processing-lab1",
            "display_name": "Processing Lab 1",
        },
    ).status_code == 201
    assert client.post(
        "/api/v1/config/task-definitions",
        json={
            "task_definition_id": "rb5-observe",
            "version": 1,
            "display_name": "RB5 observation",
            "roles": [
                {"role": "arm", "asset_type": "robot", "access_mode": "shared_read"},
                {"role": "vision", "asset_type": "camera", "access_mode": "shared_read"},
            ],
        },
    ).status_code == 201
    assert client.post(
        "/api/v1/config/task-runs",
        json={
            "run_id": "rb5-observe-001",
            "task_definition_id": "rb5-observe",
            "task_version": 1,
            "display_name": "RB5 observation 001",
            "state": "READY",
            "assignments": [
                {"role": "arm", "asset_id": "rb5-arm-01", "access_mode": "shared_read"},
                {
                    "role": "vision",
                    "asset_id": "rb5-wrist-camera-01",
                    "access_mode": "shared_read",
                },
            ],
        },
    ).status_code == 201
    return client, store, objects


def manifest_payload(
    *,
    session_id="rb5-session-001",
    camera=b"jpeg-stream",
    mcap_payload=None,
):
    metadata = b"rosbag2_bagfile_information: {}\n"
    mcap = camera + b"-mcap" if mcap_payload is None else mcap_payload

    def entry(path, payload):
        return {
            "path": path,
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    timestamp = {
        "message_count": 10,
        "valid_header_count": 10,
        "zero_header_count": 0,
        "timestamp_regressions": 0,
        "first_header_ns": 1_787_310_000_000_000_000,
        "last_header_ns": 1_787_310_001_000_000_000,
        "minimum_header_ns": 1_787_310_000_000_000_000,
        "maximum_header_ns": 1_787_310_001_000_000_000,
    }
    files = [entry("metadata.yaml", metadata), entry("bag_0.mcap", mcap)]
    manifest = {
        "schema_version": "1.0",
        "artifact_id": f"mcap:{session_id}",
        "session_id": session_id,
        "state": "complete",
        "storage_identifier": "mcap",
        "profile_id": "rb5-sync-profile",
        "agent_id": "processing-lab1",
        "robot_id": "rb5-850e-01",
        "run_id": "rb5-observe-001",
        "task_definition": {"id": "rb5-observe", "version": 1},
        "started_at": "2026-08-21T12:00:00Z",
        "finished_at": "2026-08-21T12:00:02Z",
        "record_time": {
            "start_ns": 1_787_310_000_000_000_000,
            "start": "2026-08-21T12:20:00Z",
            "end_ns": 1_787_310_002_000_000_000,
            "end": "2026-08-21T12:20:02Z",
            "duration_ns": 2_000_000_000,
        },
        "source_time_range": {
            "start_ns": 1_787_310_000_000_000_000,
            "start": "2026-08-21T12:20:00Z",
            "end_ns": 1_787_310_001_000_000_000,
            "end": "2026-08-21T12:20:01Z",
        },
        "clock": {
            "sync_source": "chrony",
            "quality": "LOCKED",
            "provenance": "host clock",
        },
        "topics": [
            {
                "key": "joints",
                "name": "/rb5/joint_states",
                "type": "sensor_msgs/msg/JointState",
                "asset_id": "rb5-arm-01",
                "calibration_ref": "rb5-calibration-v1",
                "timestamp_provenance": "controller clock mapped to host",
                "message_count": 10,
                "source_timestamp": timestamp,
            },
            {
                "key": "wrist",
                "name": "/rb5/wrist/compressed",
                "type": "sensor_msgs/msg/CompressedImage",
                "asset_id": "rb5-wrist-camera-01",
                "calibration_ref": "d405-calibration-v1",
                "timestamp_provenance": "camera header",
                "message_count": 10,
                "source_timestamp": timestamp,
            },
        ],
        "files": files,
        "total_size_bytes": sum(item["size_bytes"] for item in files),
        "checksum_algorithm": "sha256",
        "parent_artifacts": [],
        "provenance": {"host": "processing-lab1", "image_payload_recorded": True},
        "recorder_diagnostics": {"transport_messages_lost": 0},
        "safety": {
            "read_only_observation": True,
            "robot_command_interfaces": False,
            "vendor_command_channel": False,
        },
    }
    return manifest, {"metadata.yaml": metadata, "bag_0.mcap": mcap}


def playback_mcap_payload():
    base_ns = 1_787_310_000_000_000_000
    output = BytesIO()
    writer = Writer(output, compression=CompressionType.NONE)
    writer.start(profile="ros2")
    schema_id = writer.register_schema(
        "sensor_msgs/msg/CompressedImage",
        "ros2msg",
        b"std_msgs/Header header\nstring format\nuint8[] data\n",
    )
    channel_id = writer.register_channel(
        "/rb5/wrist/compressed",
        "cdr",
        schema_id,
    )
    expected = {}
    for index in range(10):
        timestamp_ns = base_ns + index * 100_000_000
        jpeg = b"\xff\xd8" + bytes([index]) * 12 + b"\xff\xd9"
        payload = bytearray(b"\x00\x01\x00\x00")
        payload.extend(
            struct.pack(
                "<iI",
                timestamp_ns // 1_000_000_000,
                timestamp_ns % 1_000_000_000,
            )
        )
        for value in ("wrist", "jpeg"):
            raw = value.encode("utf-8") + b"\x00"
            payload.extend(struct.pack("<I", len(raw)))
            payload.extend(raw)
            while (len(payload) - 4) % 4:
                payload.append(0)
        payload.extend(struct.pack("<I", len(jpeg)))
        payload.extend(jpeg)
        writer.add_message(
            channel_id,
            log_time=timestamp_ns,
            publish_time=timestamp_ns,
            sequence=index,
            data=bytes(payload),
        )
        expected[index * 100] = jpeg
    writer.finish()
    return output.getvalue(), expected


def quality_mcap_payload():
    base_ns = 1_787_310_000_000_000_000
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
    camera_channel = writer.register_channel(
        "/rb5/wrist/compressed", "cdr", camera_schema
    )

    def header(timestamp_ns, frame_id):
        payload = bytearray(b"\x00\x01\x00\x00")
        payload.extend(
            struct.pack(
                "<iI",
                timestamp_ns // 1_000_000_000,
                timestamp_ns % 1_000_000_000,
            )
        )
        raw = frame_id.encode() + b"\x00"
        payload.extend(struct.pack("<I", len(raw)))
        payload.extend(raw)
        while (len(payload) - 4) % 4:
            payload.append(0)
        return payload

    jpeg = (
        b"\xff\xd8"
        b"\xff\xc0\x00\x11\x08\x01\xe0\x02\x80\x03"
        b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
        b"\xff\xd9"
    )
    for index in range(10):
        timestamp_ns = base_ns + index * 100_000_000
        joint = header(timestamp_ns, "base")
        camera = header(timestamp_ns, "wrist")
        raw_format = b"jpeg\x00"
        camera.extend(struct.pack("<I", len(raw_format)))
        camera.extend(raw_format)
        while (len(camera) - 4) % 4:
            camera.append(0)
        camera.extend(struct.pack("<I", len(jpeg)))
        camera.extend(jpeg)
        writer.add_message(
            joint_channel,
            log_time=timestamp_ns,
            publish_time=timestamp_ns,
            sequence=index,
            data=bytes(joint),
        )
        writer.add_message(
            camera_channel,
            log_time=timestamp_ns,
            publish_time=timestamp_ns,
            sequence=index,
            data=bytes(camera),
        )
    writer.finish()
    return output.getvalue()


def test_complete_recording_quality_is_durable_searchable_and_immutable(tmp_path):
    policies = tmp_path / "manifests"
    policies.mkdir()
    (policies / "rb5-850e-01.yaml").write_text(
        """
schema_version: "0.1"
robot:
  robot_id: rb5-850e-01
  model: rb5_850e
interfaces:
  state:
    - key: observation.state
      topics: [/rb5/joint_states]
      minimum_rate_hz: 9
  cameras:
    - key: wrist
      topic: /rb5/wrist/compressed
      width: 640
      height: 480
      minimum_rate_hz: 9
monitoring:
  required_sources: [observation.state, wrist]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    client, _, objects = configured_client(
        tmp_path,
        quality_policy_root=policies,
    )
    manifest, files = manifest_payload(mcap_payload=quality_mcap_payload())
    created = client.post("/api/v1/recording-sessions", json=manifest).json()
    upload_all(client, manifest["session_id"], files)
    assert client.post(
        f"/api/v1/recording-sessions/{manifest['session_id']}/finalize",
        json={"expected_manifest_sha256": created["manifest_sha256"]},
    ).status_code == 200

    analyzed = client.post(
        f"/api/v1/recording-sessions/{manifest['session_id']}/quality/analyze"
    )
    assert analyzed.status_code == 200, analyzed.text
    result = analyzed.json()
    assert result["state"] == "PASS"
    assert result["summary"]["pass_stream_count"] == 2
    camera = next(item for item in result["streams"] if item["kind"] == "camera")
    assert camera["measured"]["rate_hz"] == pytest.approx(10.0)
    assert camera["measured"]["width"] == 640
    assert camera["measured"]["height"] == 480
    assert camera["measured"]["p95_joint_skew_ms"] == 0.0

    listed = client.get("/api/v1/recording-sessions").json()
    assert listed[0]["quality"]["state"] == "PASS"
    report = client.get(result["report_url"])
    assert report.status_code == 200
    assert hashlib.sha256(report.content).hexdigest() == result["report_sha256"]
    assert (
        objects.root
        / "quality-reports"
        / manifest["session_id"]
        / f"{result['report_sha256']}.json"
    ).is_file()
    assert client.post(
        f"/api/v1/recording-sessions/{manifest['session_id']}/quality/analyze"
    ).json()["report_sha256"] == result["report_sha256"]


def upload_all(client, session_id, files):
    for path, payload in files.items():
        response = client.put(
            f"/api/v1/recording-sessions/{session_id}/artifacts/{path}",
            content=payload,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 200, response.text


def test_object_store_streaming_write_does_not_block_the_event_loop(tmp_path, monkeypatch):
    objects = FilesystemObjectStore(tmp_path / "objects")
    payload = b"camera-frame" * 1024
    digest = hashlib.sha256(payload).hexdigest()
    original_write = recording_storage._write_block

    def slow_write(descriptor, hasher, block):
        time.sleep(0.25)
        original_write(descriptor, hasher, block)

    monkeypatch.setattr(recording_storage, "_write_block", slow_write)

    async def exercise():
        async def blocks():
            yield payload

        started = time.monotonic()
        upload = asyncio.create_task(
            objects.put_stream(
                "sessions/nonblocking/bag_0.mcap",
                blocks(),
                size_bytes=len(payload),
                sha256=digest,
            )
        )
        await asyncio.sleep(0.02)
        assert time.monotonic() - started < 0.15
        await upload

    asyncio.run(exercise())


def test_recording_session_upload_finalize_download_and_retry_are_immutable(tmp_path):
    client, _, objects = configured_client(tmp_path)
    manifest, files = manifest_payload()

    created = client.post("/api/v1/recording-sessions", json=manifest)
    assert created.status_code == 201, created.text
    assert created.json()["state"] == "STAGING"
    assert created.json()["completed_file_count"] == 0
    digest = created.json()["manifest_sha256"]

    upload_all(client, manifest["session_id"], files)
    finalized = client.post(
        f"/api/v1/recording-sessions/{manifest['session_id']}/finalize",
        json={"expected_manifest_sha256": digest},
    )
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["state"] == "COMPLETE"
    assert finalized.json()["completed_file_count"] == 2

    downloaded = client.get(
        f"/api/v1/recording-sessions/{manifest['session_id']}/artifacts/bag_0.mcap"
    )
    assert downloaded.status_code == 200
    assert downloaded.content == files["bag_0.mcap"]
    assert downloaded.headers["etag"] == (
        f'"sha256:{hashlib.sha256(files["bag_0.mcap"]).hexdigest()}"'
    )
    assert (objects.root / "sessions/rb5-session-001/session-manifest.json").is_file()

    retried = client.post("/api/v1/recording-sessions", json=manifest)
    assert retried.status_code == 201
    assert retried.json()["state"] == "COMPLETE"
    assert client.post(
        f"/api/v1/recording-sessions/{manifest['session_id']}/finalize",
        json={"expected_manifest_sha256": digest},
    ).status_code == 200


def test_complete_recording_exposes_indexed_jpeg_playback(tmp_path):
    client, _, _ = configured_client(tmp_path)
    mcap, expected = playback_mcap_payload()
    manifest, files = manifest_payload(mcap_payload=mcap)
    created = client.post("/api/v1/recording-sessions", json=manifest)
    assert created.status_code == 201, created.text
    upload_all(client, manifest["session_id"], files)
    assert client.post(
        f"/api/v1/recording-sessions/{manifest['session_id']}/finalize",
        json={"expected_manifest_sha256": created.json()["manifest_sha256"]},
    ).status_code == 200

    described = client.get(
        f"/api/v1/recording-sessions/{manifest['session_id']}/playback"
    )
    assert described.status_code == 200, described.text
    assert described.json()["duration_ms"] == 900
    assert described.json()["frame_step_ms"] == 100
    assert described.json()["max_camera_views"] == 4
    assert described.json()["cameras"] == [
        {
            "key": "wrist",
            "topic": "/rb5/wrist/compressed",
            "asset_id": "rb5-wrist-camera-01",
            "message_count": 10,
            "source_start_ns": "1787310000000000000",
            "source_end_ns": "1787310001000000000",
        }
    ]

    frame = client.get(
        f"/api/v1/recording-sessions/{manifest['session_id']}/playback/frame",
        params={"topic_key": "wrist", "offset_ms": 451},
    )
    assert frame.status_code == 200, frame.text
    assert frame.headers["content-type"] == "image/jpeg"
    assert frame.headers["x-sang-frame-offset-ms"] == "500"
    assert frame.headers["x-sang-source-offset-ms"] == "500"
    assert frame.headers["x-sang-skew-ms"] == "0"
    assert frame.content == expected[500]

    unknown = client.get(
        f"/api/v1/recording-sessions/{manifest['session_id']}/playback/frame",
        params={"topic_key": "not-declared", "offset_ms": 0},
    )
    assert unknown.status_code == 404
    outside = client.get(
        f"/api/v1/recording-sessions/{manifest['session_id']}/playback/frame",
        params={"topic_key": "wrist", "offset_ms": 901},
    )
    assert outside.status_code == 422


def test_operations_v1_authenticated_recording_e2e(tmp_path):
    _, store, objects = configured_client(tmp_path)
    manager = AuthManager(store.path)
    manager.bootstrap_admin(
        principal_id="lab-admin",
        display_name="Lab Administrator",
        password="correct horse battery staple",
        occurred_at=NOW,
    )
    service = manager.create_service_identity(
        ServiceIdentityCreate(
            principal_id="processing-lab1-uploader",
            display_name="Processing Lab1 Uploader",
            role="operator",
            token_id="processing-lab1-uploader-01",
            agent_ids=["processing-lab1"],
        ),
        actor_id="lab-admin",
        occurred_at=NOW,
    )
    client = TestClient(
        create_app(
            store,
            now=lambda: NOW,
            object_store=objects,
        )
    )
    assert client.get("/api/v1/recording-sessions").status_code == 401
    manifest, files = manifest_payload(session_id="rb5-authenticated-session")
    bearer = {"Authorization": f"Bearer {service['token']}"}
    created = client.post(
        "/api/v1/recording-sessions",
        json=manifest,
        headers=bearer,
    )
    assert created.status_code == 201, created.text
    wrong_scope, _ = manifest_payload(session_id="wrong-agent-session")
    wrong_scope["agent_id"] = "processing-other"
    denied = client.post(
        "/api/v1/recording-sessions",
        json=wrong_scope,
        headers=bearer,
    )
    assert denied.status_code == 403
    assert "not authorized" in denied.json()["error"]
    for path, payload in files.items():
        uploaded = client.put(
            f"/api/v1/recording-sessions/{manifest['session_id']}/artifacts/{path}",
            content=payload,
            headers={**bearer, "Content-Type": "application/octet-stream"},
        )
        assert uploaded.status_code == 200, uploaded.text
    finalized = client.post(
        f"/api/v1/recording-sessions/{manifest['session_id']}/finalize",
        json={"expected_manifest_sha256": created.json()["manifest_sha256"]},
        headers=bearer,
    )
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["state"] == "COMPLETE"

    logged_in = client.post(
        "/api/v1/auth/login",
        json={
            "principal_id": "lab-admin",
            "password": "correct horse battery staple",
        },
    )
    assert logged_in.status_code == 200
    catalog = client.get("/api/v1/recording-sessions")
    assert [item["session_id"] for item in catalog.json()] == [
        "rb5-authenticated-session"
    ]


def test_recording_rejects_partial_hash_conflict_and_changed_manifest(tmp_path):
    client, _, objects = configured_client(tmp_path)
    manifest, files = manifest_payload()
    created = client.post("/api/v1/recording-sessions", json=manifest)
    digest = created.json()["manifest_sha256"]

    partial = client.post(
        f"/api/v1/recording-sessions/{manifest['session_id']}/finalize",
        json={"expected_manifest_sha256": digest},
    )
    assert partial.status_code == 422

    wrong = client.put(
        f"/api/v1/recording-sessions/{manifest['session_id']}/artifacts/bag_0.mcap",
        content=b"x" * len(files["bag_0.mcap"]),
    )
    assert wrong.status_code == 422
    assert not (objects.root / "sessions/rb5-session-001/bag_0.mcap").exists()

    changed, _ = manifest_payload(camera=b"changed-data")
    conflict = client.post("/api/v1/recording-sessions", json=changed)
    assert conflict.status_code == 409


def test_recording_rejects_unregistered_artifact_and_unsafe_namespace(tmp_path):
    client, _, objects = configured_client(tmp_path)
    manifest, _ = manifest_payload()
    assert client.post("/api/v1/recording-sessions", json=manifest).status_code == 201

    undeclared = client.put(
        "/api/v1/recording-sessions/rb5-session-001/artifacts/not-declared.mcap",
        content=b"",
    )
    assert undeclared.status_code == 404

    session_root = objects.root / "sessions/rb5-session-001"
    outside = tmp_path / "outside"
    outside.mkdir()
    session_root.parent.mkdir(parents=True)
    session_root.symlink_to(outside, target_is_directory=True)
    unsafe = client.put(
        "/api/v1/recording-sessions/rb5-session-001/artifacts/metadata.yaml",
        content=b"rosbag2_bagfile_information: {}\n",
    )
    assert unsafe.status_code == 500
    assert list(outside.iterdir()) == []


def test_recording_manifest_rejects_traversal_and_wrong_asset_scope(tmp_path):
    client, _, _ = configured_client(tmp_path)
    manifest, _ = manifest_payload()
    manifest["files"][0]["path"] = "../metadata.yaml"
    assert client.post("/api/v1/recording-sessions", json=manifest).status_code == 422

    manifest, _ = manifest_payload()
    manifest["topics"][0]["asset_id"] = "missing-asset"
    invalid = client.post("/api/v1/recording-sessions", json=manifest)
    assert invalid.status_code == 422
    assert "unknown assets" in invalid.json()["error"]


def test_recording_manifest_rejects_incoherent_timestamp_statistics(tmp_path):
    client, _, _ = configured_client(tmp_path)
    manifest, _ = manifest_payload()
    manifest["topics"][0]["source_timestamp"]["message_count"] = 9
    mismatch = client.post("/api/v1/recording-sessions", json=manifest)
    assert mismatch.status_code == 422

    manifest, _ = manifest_payload()
    manifest["source_time_range"]["end_ns"] += 1
    wrong_range = client.post("/api/v1/recording-sessions", json=manifest)
    assert wrong_range.status_code == 422


def test_recording_manifest_body_is_bounded_before_json_parsing(tmp_path):
    client, _, _ = configured_client(tmp_path)
    response = client.post(
        "/api/v1/recording-sessions",
        content=b"x" * (256 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {"error": "recording manifest exceeds 256 KiB"}


def test_backup_restore_reproduces_catalog_and_object_hashes(tmp_path):
    client, store, objects = configured_client(tmp_path)
    manifest, files = manifest_payload()
    created = client.post("/api/v1/recording-sessions", json=manifest).json()
    upload_all(client, manifest["session_id"], files)
    assert client.post(
        f"/api/v1/recording-sessions/{manifest['session_id']}/finalize",
        json={"expected_manifest_sha256": created["manifest_sha256"]},
    ).status_code == 200

    backup = tmp_path / "backup-001"
    result = asyncio.run(
        create_backup(
            db_path=store.path,
            object_store_root=objects.root,
            output=backup,
        )
    )
    assert result["database"]["schema_versions"] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    verified = verify_backup(backup)
    assert verified["passed"] is True
    assert verified["object_count"] == 3

    restored_db = tmp_path / "restored/fleet.db"
    restored_objects = tmp_path / "restored/objects"
    asyncio.run(
        restore_backup(
            backup=backup,
            db_path=restored_db,
            object_store_root=restored_objects,
        )
    )
    restored_store = FleetStore(restored_db)
    assert restored_store.schema_versions() == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    assert (
        restored_objects / "sessions/rb5-session-001/bag_0.mcap"
    ).read_bytes() == files["bag_0.mcap"]


def test_backup_rejects_staging_and_detects_tampering(tmp_path):
    client, store, objects = configured_client(tmp_path)
    manifest, files = manifest_payload()
    created = client.post("/api/v1/recording-sessions", json=manifest).json()
    with pytest.raises(BackupError, match="STAGING"):
        asyncio.run(
            create_backup(
                db_path=store.path,
                object_store_root=objects.root,
                output=tmp_path / "blocked-backup",
            )
        )

    upload_all(client, manifest["session_id"], files)
    assert client.post(
        f"/api/v1/recording-sessions/{manifest['session_id']}/finalize",
        json={"expected_manifest_sha256": created["manifest_sha256"]},
    ).status_code == 200
    backup = tmp_path / "backup-tamper"
    asyncio.run(
        create_backup(
            db_path=store.path,
            object_store_root=objects.root,
            output=backup,
        )
    )
    (backup / "objects/sessions/rb5-session-001/bag_0.mcap").write_bytes(b"tampered")
    with pytest.raises(BackupError, match="integrity"):
        verify_backup(backup)


def test_edge_uploader_validates_local_session_and_streams_file_body(tmp_path):
    manifest, files = manifest_payload()
    session_dir = tmp_path / manifest["session_id"]
    session_dir.mkdir()
    (session_dir / "session-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    for path, payload in files.items():
        (session_dir / path).write_bytes(payload)

    parsed = read_manifest(session_dir)
    verify_local_artifacts(session_dir, parsed)
    (session_dir / "bag_0.mcap").write_bytes(b"x" * len(files["bag_0.mcap"]))
    with pytest.raises(ValueError, match="SHA-256"):
        verify_local_artifacts(session_dir, parsed)
    (session_dir / "bag_0.mcap").write_bytes(files["bag_0.mcap"])

    received = {}

    class UploadHandler(BaseHTTPRequestHandler):
        def do_PUT(self):
            length = int(self.headers["Content-Length"])
            received["body"] = self.rfile.read(length)
            received["authorization"] = self.headers["Authorization"]
            payload = b'{"state":"COMPLETE"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            del format, args

    server = HTTPServer(("127.0.0.1", 0), UploadHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with (session_dir / "bag_0.mcap").open("rb") as stream:
            result = request_json(
                f"http://127.0.0.1:{server.server_port}/artifact",
                method="PUT",
                token="test-token",
                context=ssl.create_default_context(),
                data=stream,
                content_length=len(files["bag_0.mcap"]),
            )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert result == {"state": "COMPLETE"}
    assert received == {
        "body": files["bag_0.mcap"],
        "authorization": "Bearer test-token",
    }


def test_edge_uploader_refuses_plaintext_remote_central():
    with pytest.raises(SystemExit):
        parse_upload_args(
            [
                "--session-dir",
                "/tmp/session",
                "--central-url",
                "http://10.0.0.10:8090",
                "--token-file",
                "/tmp/token",
            ]
        )
