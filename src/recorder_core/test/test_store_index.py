import json
from pathlib import Path

from recorder_core.store_index import scan_store, session_path_for_dataset


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_scan_store_collects_dataset_and_session_metadata_read_only(tmp_path):
    dataset_id = "contract_v1__pick_box__real__20260714_120000"
    dataset = (
        tmp_path
        / "datasets"
        / "raw"
        / "contract_v1"
        / "pick_box"
        / "real"
        / dataset_id
    )
    write_json(
        dataset / "meta" / "info.json",
        {"total_episodes": 3, "total_frames": 90, "fps": 30},
    )
    write_json(
        dataset / "validation.json",
        {
            "status": "PASS",
            "validated_at": "2026-07-14T12:01:00+00:00",
            "episode_count": 3,
            "frame_count": 90,
            "fps": 30,
        },
    )
    session_path = tmp_path / "sessions" / "session-1" / "session.json"
    write_json(
        session_path,
        {
            "session_id": "session-1",
            "dataset_id": dataset_id,
            "updated_at": "2026-07-14T12:02:00+00:00",
            "frame_count": 90,
        },
    )
    payload = b"x" * (1024 * 1024)
    data_path = dataset / "data" / "chunk.bin"
    data_path.parent.mkdir(parents=True)
    data_path.write_bytes(payload)
    before = {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*")}

    assert scan_store(tmp_path) == [
        {
            "dataset_id": dataset_id,
            "contract": "contract_v1",
            "task": "pick_box",
            "domain": "real",
            "path": str(dataset),
            "episodes": 3,
            "frames": 90,
            "fps": 30,
            "size_mb": 1.0,
            "created": "2026-07-14T12:02:00+00:00",
            "validation_status": "PASS",
            "session_id": "session-1",
        }
    ]
    assert session_path_for_dataset(tmp_path, dataset_id) == session_path
    assert before == {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*")}


def test_scan_store_falls_back_to_validation_and_ignores_partial(tmp_path):
    raw = tmp_path / "datasets" / "raw" / "contract_v1" / "task"
    dataset = raw / "sim" / "dataset-2"
    write_json(
        dataset / "validation.json",
        {
            "status": "FAIL",
            "episode_count": 2,
            "frame_count": 12,
            "fps": 6,
            "validated_at": "2026-07-14T10:00:00+00:00",
        },
    )
    write_json(raw / "sim" / "ignored.partial" / "validation.json", {"status": "PASS"})

    result = scan_store(tmp_path)

    assert len(result) == 1
    assert result[0]["dataset_id"] == "dataset-2"
    assert result[0]["episodes"] == 2
    assert result[0]["frames"] == 12
    assert result[0]["fps"] == 6
    assert result[0]["validation_status"] == "FAIL"
    assert result[0]["session_id"] is None


def test_scan_store_handles_missing_or_malformed_metadata(tmp_path):
    dataset = (
        tmp_path
        / "datasets"
        / "raw"
        / "contract"
        / "task"
        / "real"
        / "dataset"
    )
    dataset.mkdir(parents=True)
    (dataset / "validation.json").write_text("not-json", encoding="utf-8")

    result = scan_store(tmp_path)

    assert result[0]["episodes"] == 0
    assert result[0]["frames"] == 0
    assert result[0]["fps"] == 0
    assert result[0]["created"] is None
    assert result[0]["validation_status"] == "UNKNOWN"
    assert scan_store(tmp_path / "missing") == []
