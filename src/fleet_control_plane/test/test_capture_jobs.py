from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from fastapi.testclient import TestClient

from fleet_control_plane.app import create_app
from fleet_control_plane.auth import AuthManager, ServiceIdentityCreate
from fleet_control_plane.capture_jobs import (
    CaptureJobReport,
    CaptureJobStore,
    CaptureWorkerPoll,
)
from fleet_control_plane.recording_storage import FilesystemObjectStore
from fleet_control_plane.store import FleetStore


NOW = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)
RUN_ID = "rb5-capture-001"
AGENT_ID = "processing-lab1"
ROBOT_ID = "rb5-lab1-eval-01"
WORKER_ID = "processing-lab1-rb5-capture"
PROFILE_ID = "rb5-lab1-observation-v2"
ASSET_IDS = ["rb5-lab1", "rb5-lab1-d455"]


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
        "agent_id": AGENT_ID,
        "agent_name": "Processing Lab 1",
        "sw_version": "0.1.0",
        "sent_at": NOW.isoformat(),
        "storage": {"disk_free_gb": 100, "ram_free_gb": 20, "cpu_pct": 10},
        "datasets": {"count": 0, "total_episodes": 0, "total_size_mb": 0},
        "robots": [
            {
                "robot_id": ROBOT_ID,
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
                        "topics": [{**topic, "topic": "/rbpodo/joint_states"}],
                    },
                    {
                        "key": "d455_color",
                        "kind": "camera",
                        "required": True,
                        "freshness_ms": 1000,
                        "fresh": True,
                        "rate_ok": True,
                        "topics": [{**topic, "topic": "/camera/d455/compressed"}],
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


def configured_client(tmp_path):
    store = FleetStore(tmp_path / "fleet.db")
    objects = FilesystemObjectStore(tmp_path / "objects")
    app = create_app(
        store,
        now=lambda: NOW,
        auth_enabled=False,
        object_store=objects,
    )
    client = TestClient(app)
    assert client.post(
        "/api/v1/agents/heartbeat", json=observed_heartbeat()
    ).status_code == 202
    assert client.post(
        "/api/v1/config/agent-registrations",
        json={"agent_id": AGENT_ID, "display_name": "Processing Lab 1"},
    ).status_code == 201
    assert client.post(
        "/api/v1/config/processing-nodes",
        json={
            "node_id": "processing-node-lab1",
            "display_name": "Processing Lab 1",
            "agent_ids": [AGENT_ID],
        },
    ).status_code == 201
    assets = (
        {
            "asset_id": ASSET_IDS[0],
            "asset_type": "robot",
            "display_name": "RB5-850E",
            "node_id": "processing-node-lab1",
            "binding": {
                "agent_id": AGENT_ID,
                "robot_id": ROBOT_ID,
                "source_key": None,
            },
            "capabilities": ["telemetry"],
        },
        {
            "asset_id": ASSET_IDS[1],
            "asset_type": "camera",
            "display_name": "D455",
            "node_id": "processing-node-lab1",
            "binding": {
                "agent_id": AGENT_ID,
                "robot_id": ROBOT_ID,
                "source_key": "d455_color",
            },
            "capabilities": ["rgb", "depth"],
        },
    )
    for asset in assets:
        assert client.post("/api/v1/config/assets", json=asset).status_code == 201
    assert client.post(
        "/api/v1/config/task-definitions",
        json={
            "task_definition_id": "rb5-observe",
            "version": 1,
            "display_name": "RB5 observation",
            "roles": [
                {"role": "arm", "asset_type": "robot", "access_mode": "shared_read"},
                {
                    "role": "vision",
                    "asset_type": "camera",
                    "access_mode": "shared_read",
                },
            ],
        },
    ).status_code == 201
    assert client.post(
        "/api/v1/config/task-runs",
        json={
            "run_id": RUN_ID,
            "task_definition_id": "rb5-observe",
            "task_version": 1,
            "display_name": "RB5 observation 001",
            "state": "READY",
            "assignments": [
                {
                    "role": "arm",
                    "asset_id": ASSET_IDS[0],
                    "access_mode": "shared_read",
                },
                {
                    "role": "vision",
                    "asset_id": ASSET_IDS[1],
                    "access_mode": "shared_read",
                },
            ],
        },
    ).status_code == 201
    assert client.post(
        f"/api/v1/config/task-runs/{RUN_ID}/transition",
        json={"expected_revision": 1, "state": "ACTIVE"},
    ).status_code == 200
    jobs = CaptureJobStore(store.path)
    jobs.register_worker_and_claim(
        CaptureWorkerPoll(
            worker_id=WORKER_ID,
            profile_id=PROFILE_ID,
            robot_id=ROBOT_ID,
            asset_ids=ASSET_IDS,
        ),
        agent_id=AGENT_ID,
        occurred_at=NOW,
    )
    return client, store, objects, jobs


def recording_payload(session_id):
    metadata = b"rosbag2_bagfile_information: {}\n"
    mcap = b"read-only-observation-mcap"

    def file_entry(path, payload):
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
        "first_header_ns": 1_787_587_200_000_000_000,
        "last_header_ns": 1_787_587_201_000_000_000,
        "minimum_header_ns": 1_787_587_200_000_000_000,
        "maximum_header_ns": 1_787_587_201_000_000_000,
    }
    files = [file_entry("metadata.yaml", metadata), file_entry("bag_0.mcap", mcap)]
    topics = [
        {
            "key": "joints",
            "name": "/rbpodo/joint_states",
            "type": "sensor_msgs/msg/JointState",
            "asset_id": ASSET_IDS[0],
            "calibration_ref": "vendor-controller-measured-state",
            "timestamp_provenance": "read-only controller state",
            "message_count": 10,
            "source_timestamp": timestamp,
        },
        {
            "key": "d455",
            "name": "/camera/d455/compressed",
            "type": "sensor_msgs/msg/CompressedImage",
            "asset_id": ASSET_IDS[1],
            "calibration_ref": "d455-calibration",
            "timestamp_provenance": "camera header",
            "message_count": 10,
            "source_timestamp": timestamp,
        },
    ]
    return {
        "schema_version": "1.0",
        "artifact_id": f"mcap:{session_id}",
        "session_id": session_id,
        "state": "complete",
        "storage_identifier": "mcap",
        "profile_id": PROFILE_ID,
        "agent_id": AGENT_ID,
        "robot_id": ROBOT_ID,
        "run_id": RUN_ID,
        "task_definition": {"id": "rb5-observe", "version": 1},
        "started_at": "2026-08-24T16:00:00Z",
        "finished_at": "2026-08-24T16:00:02Z",
        "record_time": {
            "start_ns": 1_787_587_200_000_000_000,
            "start": "2026-08-24T16:00:00Z",
            "end_ns": 1_787_587_202_000_000_000,
            "end": "2026-08-24T16:00:02Z",
            "duration_ns": 2_000_000_000,
        },
        "source_time_range": {
            "start_ns": timestamp["minimum_header_ns"],
            "start": "2026-08-24T16:00:00Z",
            "end_ns": timestamp["maximum_header_ns"],
            "end": "2026-08-24T16:00:01Z",
        },
        "clock": {"sync_source": "chrony", "quality": "LOCKED"},
        "topics": topics,
        "files": files,
        "total_size_bytes": sum(item["size_bytes"] for item in files),
        "checksum_algorithm": "sha256",
        "parent_artifacts": [],
        "provenance": {"host": AGENT_ID, "image_payload_recorded": True},
        "recorder_diagnostics": {"transport_messages_lost": 0},
        "safety": {
            "read_only_observation": True,
            "robot_command_interfaces": False,
            "vendor_command_channel": False,
        },
    }, {"metadata.yaml": metadata, "bag_0.mcap": mcap}


def test_capture_job_is_idempotent_durable_and_completed_only_by_catalog(tmp_path):
    client, store, _, jobs = configured_client(tmp_path)
    request = {
        "request_id": "ui-request-001",
        "worker_id": WORKER_ID,
        "duration_s": 60,
    }

    created = client.post(f"/api/v1/task-runs/{RUN_ID}/capture-jobs", json=request)
    assert created.status_code == 201, created.text
    job = created.json()
    assert job["run_id"] == RUN_ID
    assert job["session_id"] == job["job_id"]
    assert job["state"] == "PENDING"
    assert job["idempotent_replay"] is False

    replay = client.post(f"/api/v1/task-runs/{RUN_ID}/capture-jobs", json=request)
    assert replay.status_code == 201
    assert replay.json()["job_id"] == job["job_id"]
    assert replay.json()["idempotent_replay"] is True
    conflict = client.post(
        f"/api/v1/task-runs/{RUN_ID}/capture-jobs",
        json={**request, "duration_s": 120},
    )
    assert conflict.status_code == 409
    second = client.post(
        f"/api/v1/task-runs/{RUN_ID}/capture-jobs",
        json={**request, "request_id": "ui-request-002"},
    )
    assert second.status_code == 409

    claimed = jobs.register_worker_and_claim(
        CaptureWorkerPoll(
            worker_id=WORKER_ID,
            profile_id=PROFILE_ID,
            robot_id=ROBOT_ID,
            asset_ids=ASSET_IDS,
        ),
        agent_id=AGENT_ID,
        occurred_at=NOW,
    )["job"]
    lease_id = claimed["lease_id"]
    jobs.report_job(
        job["job_id"],
        CaptureJobReport(
            worker_id=WORKER_ID,
            lease_id=lease_id,
            state="RECORDING",
        ),
        agent_id=AGENT_ID,
        occurred_at=NOW,
    )

    reopened = CaptureJobStore(store.path).get_job(job["job_id"])
    assert reopened["state"] == "RECORDING"
    assert "lease_id" not in reopened
    payload, files = recording_payload(job["session_id"])
    registered = client.post("/api/v1/recording-sessions", json=payload)
    assert registered.status_code == 201, registered.text
    assert client.get(f"/api/v1/capture-jobs/{job['job_id']}").json()["state"] == "UPLOADING"

    for path, content in files.items():
        uploaded = client.put(
            f"/api/v1/recording-sessions/{job['session_id']}/artifacts/{path}",
            content=content,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert uploaded.status_code == 200, uploaded.text
    before_finalize = client.get(f"/api/v1/capture-jobs/{job['job_id']}").json()
    assert before_finalize["state"] == "UPLOADING"

    finalized = client.post(
        f"/api/v1/recording-sessions/{job['session_id']}/finalize",
        json={"expected_manifest_sha256": registered.json()["manifest_sha256"]},
    )
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["state"] == "COMPLETE"
    completed = client.get(f"/api/v1/capture-jobs/{job['job_id']}").json()
    assert completed["state"] == "COMPLETE"
    assert completed["recording"]["completed_file_count"] == 2
    assert completed["recording"]["manifest_sha256"] == registered.json()["manifest_sha256"]
    assert len(completed["events"]) == 5

    replay_after_complete = jobs.report_job(
        job["job_id"],
        CaptureJobReport(
            worker_id=WORKER_ID,
            lease_id=lease_id,
            state="UPLOADING",
        ),
        agent_id=AGENT_ID,
        occurred_at=NOW,
    )
    assert replay_after_complete["state"] == "COMPLETE"


def test_pending_stop_is_terminal_and_allows_a_new_capture(tmp_path):
    client, _, _, _ = configured_client(tmp_path)
    first = client.post(
        f"/api/v1/task-runs/{RUN_ID}/capture-jobs",
        json={
            "request_id": "stop-request-001",
            "worker_id": WORKER_ID,
            "duration_s": 60,
        },
    ).json()
    stopped = client.post(f"/api/v1/capture-jobs/{first['job_id']}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["state"] == "CANCELLED"

    replacement = client.post(
        f"/api/v1/task-runs/{RUN_ID}/capture-jobs",
        json={
            "request_id": "stop-request-002",
            "worker_id": WORKER_ID,
            "duration_s": 60,
        },
    )
    assert replacement.status_code == 201, replacement.text


def test_capture_gated_run_completion_requires_latest_quality_pass(tmp_path):
    client, store, _, jobs = configured_client(tmp_path)
    created = client.post(
        f"/api/v1/task-runs/{RUN_ID}/capture-jobs",
        json={
            "request_id": "run-completion-001",
            "worker_id": WORKER_ID,
            "duration_s": 60,
        },
    ).json()
    blocked_active = client.post(
        f"/api/v1/task-runs/{RUN_ID}/complete-after-capture",
        json={"expected_revision": 2, "state": "COMPLETE"},
    )
    assert blocked_active.status_code == 409

    claimed = jobs.register_worker_and_claim(
        CaptureWorkerPoll(
            worker_id=WORKER_ID,
            profile_id=PROFILE_ID,
            robot_id=ROBOT_ID,
            asset_ids=ASSET_IDS,
        ),
        agent_id=AGENT_ID,
        occurred_at=NOW,
    )["job"]
    jobs.report_job(
        created["job_id"],
        CaptureJobReport(
            worker_id=WORKER_ID,
            lease_id=claimed["lease_id"],
            state="RECORDING",
        ),
        agent_id=AGENT_ID,
        occurred_at=NOW,
    )
    payload, files = recording_payload(created["session_id"])
    registered = client.post("/api/v1/recording-sessions", json=payload)
    assert registered.status_code == 201
    for path, content in files.items():
        assert client.put(
            f"/api/v1/recording-sessions/{created['session_id']}/artifacts/{path}",
            content=content,
            headers={"Content-Type": "application/octet-stream"},
        ).status_code == 200
    assert client.post(
        f"/api/v1/recording-sessions/{created['session_id']}/finalize",
        json={"expected_manifest_sha256": registered.json()["manifest_sha256"]},
    ).status_code == 200

    blocked_pending_quality = client.post(
        f"/api/v1/task-runs/{RUN_ID}/complete-after-capture",
        json={"expected_revision": 2, "state": "COMPLETE"},
    )
    assert blocked_pending_quality.status_code == 409
    assert "PENDING" in blocked_pending_quality.json()["error"]

    summary = {
        "stream_count": 2,
        "pass_stream_count": 1,
        "warning_stream_count": 0,
        "fail_stream_count": 1,
        "failed_check_count": 1,
        "warning_check_count": 0,
    }
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO recording_quality_reports (
                session_id, state, report_schema_version, policy_sha256,
                report_sha256, report_size_bytes, object_key, clock_state,
                stream_count, pass_stream_count, warning_stream_count,
                fail_stream_count, failed_check_count, warning_check_count,
                summary_json, analyzed_at
            ) VALUES (?, 'FAIL', '1.0', ?, ?, 2, ?, 'LOCKED', 2, 1, 0, 1, 1, 0, ?, ?)
            """,
            (
                created["session_id"],
                "a" * 64,
                "b" * 64,
                f"quality-reports/{created['session_id']}/{'b' * 64}.json",
                json.dumps(summary, separators=(",", ":"), sort_keys=True),
                NOW.isoformat(),
            ),
        )
    blocked_failed_quality = client.post(
        f"/api/v1/task-runs/{RUN_ID}/complete-after-capture",
        json={"expected_revision": 2, "state": "COMPLETE"},
    )
    assert blocked_failed_quality.status_code == 409
    assert "FAIL" in blocked_failed_quality.json()["error"]

    summary.update(
        pass_stream_count=2,
        fail_stream_count=0,
        failed_check_count=0,
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE recording_quality_reports
            SET state = 'PASS', pass_stream_count = 2, fail_stream_count = 0,
                failed_check_count = 0, summary_json = ?
            WHERE session_id = ?
            """,
            (
                json.dumps(summary, separators=(",", ":"), sort_keys=True),
                created["session_id"],
            ),
        )
    completed = client.post(
        f"/api/v1/task-runs/{RUN_ID}/complete-after-capture",
        json={"expected_revision": 2, "state": "COMPLETE"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["state"] == "COMPLETE"
    assert completed.json()["revision"] == 3
    assert all(not item["reserved"] for item in completed.json()["assignments"])


def test_failed_report_replay_does_not_require_a_retained_lease(tmp_path):
    client, _, _, jobs = configured_client(tmp_path)
    created = client.post(
        f"/api/v1/task-runs/{RUN_ID}/capture-jobs",
        json={
            "request_id": "failure-request-001",
            "worker_id": WORKER_ID,
            "duration_s": 60,
        },
    ).json()
    claimed = jobs.register_worker_and_claim(
        CaptureWorkerPoll(
            worker_id=WORKER_ID,
            profile_id=PROFILE_ID,
            robot_id=ROBOT_ID,
            asset_ids=ASSET_IDS,
        ),
        agent_id=AGENT_ID,
        occurred_at=NOW,
    )["job"]
    report = CaptureJobReport(
        worker_id=WORKER_ID,
        lease_id=claimed["lease_id"],
        state="FAILED",
        error="worker restarted with a preserved partial session",
    )
    assert jobs.report_job(
        created["job_id"], report, agent_id=AGENT_ID, occurred_at=NOW
    )["state"] == "FAILED"
    assert jobs.report_job(
        created["job_id"], report, agent_id=AGENT_ID, occurred_at=NOW
    )["state"] == "FAILED"


def test_capture_job_creation_requires_operator_role(tmp_path):
    _, store, objects, _ = configured_client(tmp_path)
    auth = AuthManager(store.path)
    auth.bootstrap_admin(
        principal_id="lab-admin",
        display_name="Lab Administrator",
        password="correct horse battery staple",
        occurred_at=NOW,
    )
    viewer = auth.create_service_identity(
        ServiceIdentityCreate(
            principal_id="capture-viewer",
            display_name="Capture Viewer",
            role="viewer",
            token_id="capture-viewer-token",
        ),
        actor_id="lab-admin",
        occurred_at=NOW,
    )
    operator = auth.create_service_identity(
        ServiceIdentityCreate(
            principal_id="capture-operator",
            display_name="Capture Operator",
            role="operator",
            token_id="capture-operator-token",
            agent_ids=[AGENT_ID],
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
    body = {
        "request_id": "rbac-request-001",
        "worker_id": WORKER_ID,
        "duration_s": 60,
    }
    denied = client.post(
        f"/api/v1/task-runs/{RUN_ID}/capture-jobs",
        json=body,
        headers={"Authorization": f"Bearer {viewer['token']}"},
    )
    assert denied.status_code == 403
    allowed = client.post(
        f"/api/v1/task-runs/{RUN_ID}/capture-jobs",
        json=body,
        headers={"Authorization": f"Bearer {operator['token']}"},
    )
    assert allowed.status_code == 201, allowed.text
