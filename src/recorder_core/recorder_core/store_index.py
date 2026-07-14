"""Read-only index of finalized datasets in a recorder store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _session_index(sessions_root: Path) -> dict[str, dict[str, Any]]:
    sessions: dict[str, dict[str, Any]] = {}
    if not sessions_root.is_dir():
        return sessions
    for path in sorted(sessions_root.glob("*/session.json")):
        session = _read_json(path)
        dataset_id = session.get("dataset_id")
        if not isinstance(dataset_id, str) or not dataset_id:
            continue
        current = sessions.get(dataset_id)
        if current is None or str(session.get("updated_at", "")) >= str(
            current.get("updated_at", "")
        ):
            sessions[dataset_id] = {**session, "_path": str(path)}
    return sessions


def _first_value(*values: Any, default: Any = None) -> Any:
    for value in values:
        if value is not None:
            return value
    return default


def _integer(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _size_mb(root: Path) -> float:
    total = 0
    try:
        paths = root.rglob("*")
        for path in paths:
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return round(total / (1024 * 1024), 3)


def scan_store(store_root: str | Path) -> list[dict[str, Any]]:
    """Return finalized datasets under ``store/datasets/raw``.

    Metadata files are never created or modified. Missing or malformed optional
    metadata leaves conservative default values while keeping the dataset
    visible to operators.
    """
    root = Path(store_root)
    raw_root = root / "datasets" / "raw"
    if not raw_root.is_dir():
        return []

    sessions = _session_index(root / "sessions")
    datasets: list[dict[str, Any]] = []
    for dataset_path in sorted(raw_root.glob("*/*/*/*")):
        if not dataset_path.is_dir() or dataset_path.name.endswith(".partial"):
            continue
        relative = dataset_path.relative_to(raw_root)
        if len(relative.parts) != 4:
            continue
        contract, task, domain, dataset_id = relative.parts
        info = _read_json(dataset_path / "meta" / "info.json")
        validation = _read_json(dataset_path / "validation.json")
        session = sessions.get(dataset_id, {})

        created = _first_value(
            info.get("created_at"),
            info.get("created"),
            session.get("created_at"),
            session.get("started_at"),
            session.get("updated_at"),
            validation.get("validated_at"),
        )
        validation_status = validation.get("status")
        if not isinstance(validation_status, str) or not validation_status:
            validation_status = "UNKNOWN"
        session_id = session.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            session_id = None

        datasets.append(
            {
                "dataset_id": dataset_id,
                "contract": contract,
                "task": task,
                "domain": domain,
                "path": str(dataset_path),
                "episodes": _integer(
                    _first_value(
                        info.get("total_episodes"),
                        validation.get("episode_count"),
                        default=0,
                    )
                ),
                "frames": _integer(
                    _first_value(
                        info.get("total_frames"),
                        validation.get("frame_count"),
                        session.get("frame_count"),
                        default=0,
                    )
                ),
                "fps": _integer(
                    _first_value(
                        info.get("fps"), validation.get("fps"), default=0
                    )
                ),
                "size_mb": _size_mb(dataset_path),
                "created": created,
                "validation_status": validation_status,
                "session_id": session_id,
            }
        )

    return sorted(
        datasets,
        key=lambda item: (str(item["created"] or ""), item["dataset_id"]),
        reverse=True,
    )


def session_path_for_dataset(
    store_root: str | Path, dataset_id: str
) -> Path | None:
    """Locate the session lineage file for a dataset without modifying it."""
    session = _session_index(Path(store_root) / "sessions").get(dataset_id)
    if session is None:
        return None
    return Path(session["_path"])
