from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import ssl
import threading
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

from fleet_control_plane.agent_ingress import create_server_tls_context
from fleet_control_plane.models import AgentHeartbeatV1
from fleet_control_plane.operations_models import (
    AgentCredentialCreate,
    AgentRegistrationCreate,
    AssetCreate,
    ConnectionProfileCreate,
    CredentialRevoke,
    ObservedBinding,
    ProcessingNodeCreate,
)
from fleet_control_plane.recording_auto_upload import (
    UploadCheckpointStore,
    scan_once,
)
from fleet_control_plane.recording_ingress import RecordingIngressServer
from fleet_control_plane.recording_storage import FilesystemObjectStore
from fleet_control_plane.recording_upload import create_client_context, upload_session
from fleet_control_plane.store import FleetStore
from sang_security.certificates import certificate_sha256
from sang_security.pki import initialize_pki, issue_certificate


AGENT_ID = "processing-lab1"
ROBOT_ID = "rb5-850e-01"
NOW = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)


def observed_heartbeat() -> AgentHeartbeatV1:
    topic = {
        "topic": "/rbpodo/joint_states",
        "age_ms": 20.0,
        "rate_hz": 30.0,
        "minimum_rate_hz": 10.0,
        "rate_ok": True,
        "seq": 10,
        "fresh": True,
    }
    return AgentHeartbeatV1.model_validate(
        {
            "schema_version": 1,
            "agent_id": AGENT_ID,
            "agent_name": "Processing Lab 1",
            "sw_version": "0.1.0",
            "sent_at": NOW.isoformat(),
            "storage": {
                "disk_free_gb": 100,
                "ram_free_gb": 20,
                "cpu_pct": 10,
            },
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
                            "topics": [topic],
                        }
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
    )


def recording_manifest(session_id: str) -> tuple[dict, dict[str, bytes]]:
    files = {
        "metadata.yaml": b"rosbag2_bagfile_information: {}\n",
        "bag_0.mcap": b"bounded-stream-test-mcap",
    }
    entries = [
        {
            "path": path,
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for path, payload in files.items()
    ]
    source_start = 1_787_587_200_000_000_000
    source_end = source_start + 1_000_000_000
    manifest = {
        "schema_version": "1.0",
        "artifact_id": f"mcap:{session_id}",
        "session_id": session_id,
        "state": "complete",
        "storage_identifier": "mcap",
        "profile_id": "rb5-lab1-observation-v2",
        "agent_id": AGENT_ID,
        "robot_id": ROBOT_ID,
        "run_id": None,
        "task_definition": None,
        "started_at": "2026-08-24T16:00:00Z",
        "finished_at": "2026-08-24T16:00:02Z",
        "record_time": {
            "start_ns": source_start,
            "start": "2026-08-24T16:00:00Z",
            "end_ns": source_start + 2_000_000_000,
            "end": "2026-08-24T16:00:02Z",
            "duration_ns": 2_000_000_000,
        },
        "source_time_range": {
            "start_ns": source_start,
            "start": "2026-08-24T16:00:00Z",
            "end_ns": source_end,
            "end": "2026-08-24T16:00:01Z",
        },
        "clock": {"sync_source": "chrony", "quality": "LOCKED"},
        "topics": [
            {
                "key": "joints",
                "name": "/rbpodo/joint_states",
                "type": "sensor_msgs/msg/JointState",
                "asset_id": "rb5-arm-01",
                "calibration_ref": "rb5-controller-native",
                "timestamp_provenance": "RB5 controller timestamp mapped to host",
                "message_count": 10,
                "source_timestamp": {
                    "message_count": 10,
                    "valid_header_count": 10,
                    "zero_header_count": 0,
                    "timestamp_regressions": 0,
                    "first_header_ns": source_start,
                    "last_header_ns": source_end,
                    "minimum_header_ns": source_start,
                    "maximum_header_ns": source_end,
                },
            }
        ],
        "files": entries,
        "total_size_bytes": sum(len(payload) for payload in files.values()),
        "checksum_algorithm": "sha256",
        "parent_artifacts": [],
        "provenance": {"recorder": "sang_mcap_recorder"},
        "recorder_diagnostics": {},
        "safety": {
            "read_only_observation": True,
            "robot_command_interfaces": False,
            "vendor_command_channel": False,
        },
    }
    return manifest, files


def configure_store(store: FleetStore, certificate: Path) -> None:
    store.upsert(observed_heartbeat(), received_at=NOW)
    store.create_agent_registration(
        AgentRegistrationCreate(agent_id=AGENT_ID, display_name="Processing Lab 1"),
        occurred_at=NOW,
    )
    store.create_agent_credential(
        AgentCredentialCreate(
            credential_id="processing-lab1-https-01",
            agent_id=AGENT_ID,
            certificate_sha256=certificate_sha256(certificate),
            certificate_ref="secret://https/agents/processing-lab1.pem",
            not_before=NOW - timedelta(days=1),
            not_after=NOW + timedelta(days=30),
        ),
        occurred_at=NOW,
    )
    store.create_connection_profile(
        ConnectionProfileCreate(
            profile_id="processing-lab1-heartbeat",
            owner_kind="agent",
            agent_id=AGENT_ID,
            protocol="https_agent",
            desired_endpoint="https://central.sang.local:8443",
            security_mode="MutualTLS",
            security_policy="TLS1.2+",
            client_certificate_ref="secret://https/client/chain.pem",
            client_private_key_ref="secret://https/client/key.pem",
            trust_list_ref="secret://https/trust/ca-crl.pem",
            crl_ref="secret://https/trust/ca-crl.pem",
        ),
        occurred_at=NOW,
    )
    store.create_processing_node(
        ProcessingNodeCreate(
            node_id="processing-node-lab1",
            display_name="Processing Lab 1",
            agent_ids=[AGENT_ID],
        ),
        occurred_at=NOW,
    )
    store.create_asset(
        AssetCreate(
            asset_id="rb5-arm-01",
            asset_type="robot",
            display_name="RB5-850E",
            node_id="processing-node-lab1",
            binding=ObservedBinding(agent_id=AGENT_ID, robot_id=ROBOT_ID),
            capabilities=["telemetry"],
        ),
        occurred_at=NOW,
    )


def request_json(
    url: str,
    *,
    context: ssl.SSLContext,
    method: str,
    payload: dict | None = None,
    body: bytes | None = None,
) -> dict:
    headers = {"Accept": "application/json"}
    data = body
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif body is not None:
        headers["Content-Type"] = "application/octet-stream"
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=5.0, context=context) as response:
        return json.loads(response.read())


def write_session(capture_root: Path, session_id: str) -> tuple[Path, dict, dict[str, bytes]]:
    manifest, files = recording_manifest(session_id)
    session_dir = capture_root / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "session-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    for path, payload in files.items():
        (session_dir / path).write_bytes(payload)
    return session_dir, manifest, files


def test_recording_ingress_mtls_ownership_streaming_and_revocation(tmp_path):
    pki = tmp_path / "pki"
    initialize_pki(pki, key_size=2048)
    server_material = issue_certificate(
        pki,
        issuer="https",
        name="recording-central",
        role="https-server",
        dns_names=("localhost",),
        ip_addresses=("127.0.0.1",),
    )
    client_material = issue_certificate(
        pki,
        issuer="https",
        name="processing-lab1",
        role="https-client",
        uri=f"urn:sang:agent:{AGENT_ID}",
    )
    store = FleetStore(tmp_path / "fleet.db")
    configure_store(store, Path(client_material["certificate"]))
    objects = FilesystemObjectStore(tmp_path / "objects")
    server = RecordingIngressServer(
        ("127.0.0.1", 0),
        store=store,
        object_store=objects,
        tls_context=create_server_tls_context(
            certificate_chain=Path(server_material["chain"]),
            private_key=Path(server_material["private_key"]),
            client_ca_crl_bundle=pki / "bundles" / "https-ca-crl.pem",
        ),
        now=lambda: NOW,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = create_client_context(
        ca_bundle=pki / "bundles" / "https-ca-crl.pem",
        client_certificate=Path(client_material["chain"]),
        client_private_key=Path(client_material["private_key"]),
    )
    base = f"https://127.0.0.1:{server.server_port}/api/v1/recording-sessions"
    manifest, files = recording_manifest("rb5-mtls-session-001")
    try:
        created = request_json(base, context=context, method="POST", payload=manifest)
        assert created["state"] == "STAGING"

        wrong_agent = {**manifest, "agent_id": "processing-other"}
        wrong_agent["session_id"] = "rb5-wrong-agent-session"
        wrong_agent["artifact_id"] = "mcap:rb5-wrong-agent-session"
        with pytest.raises(HTTPError) as denied:
            request_json(base, context=context, method="POST", payload=wrong_agent)
        assert denied.value.code == 403

        for path, payload in files.items():
            result = request_json(
                f"{base}/{quote(manifest['session_id'], safe='')}/artifacts/"
                f"{quote(path, safe='/')}",
                context=context,
                method="PUT",
                body=payload,
            )
            assert result["completed_file_count"] >= 1

        finalized = request_json(
            f"{base}/{quote(manifest['session_id'], safe='')}/finalize",
            context=context,
            method="POST",
            payload={"expected_manifest_sha256": created["manifest_sha256"]},
        )
        assert finalized["state"] == "COMPLETE"
        assert request_json(
            base,
            context=context,
            method="POST",
            payload=manifest,
        )["state"] == "COMPLETE"
        for path, payload in files.items():
            assert (
                objects.root / "sessions" / manifest["session_id"] / path
            ).read_bytes() == payload

        capture_root = tmp_path / "captures"
        session_dir, second_manifest, second_files = write_session(
            capture_root,
            "rb5-mtls-session-002",
        )
        uploaded = upload_session(
            session_dir,
            central_url=f"https://127.0.0.1:{server.server_port}",
            context=context,
        )
        assert uploaded["state"] == "COMPLETE"
        (session_dir / "bag_0.mcap").write_bytes(b"locally-corrupted-after-upload")
        assert upload_session(
            session_dir,
            central_url=f"https://127.0.0.1:{server.server_port}",
            context=context,
        )["state"] == "COMPLETE"
        for path, payload in second_files.items():
            assert (
                objects.root / "sessions" / second_manifest["session_id"] / path
            ).read_bytes() == payload

        store.revoke_agent_credential(
            "processing-lab1-https-01",
            CredentialRevoke(reason="test revocation"),
            occurred_at=NOW,
        )
        revoked_manifest, _ = recording_manifest("rb5-revoked-session")
        with pytest.raises(HTTPError) as revoked:
            request_json(base, context=context, method="POST", payload=revoked_manifest)
        assert revoked.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)
    assert not thread.is_alive()


def test_auto_uploader_retries_filters_and_persists_completion(tmp_path):
    capture_root = tmp_path / "captures"
    session_dir, manifest, _ = write_session(
        capture_root,
        "rb5-auto-upload-session",
    )
    old_dir, old_manifest, _ = write_session(
        capture_root,
        "rb5-old-profile-session",
    )
    old_manifest["profile_id"] = "rb5-lab1-observation-v1"
    (old_dir / "session-manifest.json").write_text(
        json.dumps(old_manifest),
        encoding="utf-8",
    )
    checkpoints = UploadCheckpointStore(tmp_path / "upload-state.db")
    current = [NOW]
    calls = []
    events = []

    def upload(path):
        calls.append(path.name)
        if len(calls) == 1:
            raise RuntimeError("Central is temporarily unavailable")
        return {"state": "COMPLETE", "session_id": path.name}

    first = scan_once(
        capture_root,
        agent_id=AGENT_ID,
        profile_ids={"rb5-lab1-observation-v2"},
        checkpoints=checkpoints,
        upload=upload,
        now=lambda: current[0],
        emit=events.append,
    )
    assert first == {
        "discovered": 2,
        "eligible": 1,
        "completed": 0,
        "failed": 1,
        "skipped": 1,
        "invalid": 0,
    }
    assert scan_once(
        capture_root,
        agent_id=AGENT_ID,
        profile_ids={"rb5-lab1-observation-v2"},
        checkpoints=checkpoints,
        upload=upload,
        now=lambda: current[0],
        emit=events.append,
    )["eligible"] == 0

    current[0] += timedelta(seconds=10)
    retried = scan_once(
        capture_root,
        agent_id=AGENT_ID,
        profile_ids={"rb5-lab1-observation-v2"},
        checkpoints=checkpoints,
        upload=upload,
        now=lambda: current[0],
        emit=events.append,
    )
    assert retried["completed"] == 1
    assert calls == [session_dir.name, session_dir.name]
    assert checkpoints.get(manifest["session_id"])["attempts"] == 2

    restarted = UploadCheckpointStore(tmp_path / "upload-state.db")
    after_restart = scan_once(
        capture_root,
        agent_id=AGENT_ID,
        profile_ids={"rb5-lab1-observation-v2"},
        checkpoints=restarted,
        upload=upload,
        now=lambda: current[0],
        emit=events.append,
    )
    assert after_restart["eligible"] == 0
    assert restarted.get(manifest["session_id"])["state"] == "COMPLETE"
    assert [event["event"] for event in events] == [
        "recording_upload_failed",
        "recording_upload_complete",
    ]
