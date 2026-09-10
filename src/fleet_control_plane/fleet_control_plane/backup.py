"""Verified backup and restore for Central SQLite and immutable objects."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import sys
from typing import Any

from fleet_control_plane.recording_storage import FilesystemObjectStore, ObjectStoreError


BACKUP_SCHEMA_VERSION = 1
MANIFEST_NAME = "backup-manifest.json"


class BackupError(RuntimeError):
    """Backup material is incomplete, unsafe or corrupt."""


def _immutable_connection(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def sha256_file(path: Path) -> tuple[int, str]:
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"backup input is not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            size += len(block)
            digest.update(block)
    finally:
        os.close(descriptor)
    return size, digest.hexdigest()


def validate_database(path: Path) -> list[int]:
    if path.is_symlink() or not path.is_file():
        raise BackupError("fleet database must be a regular non-symlink file")
    connection = _immutable_connection(path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise BackupError(f"SQLite integrity check failed: {integrity}")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise BackupError("SQLite foreign key check failed")
        versions = [
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
    except sqlite3.Error as exc:
        raise BackupError(f"cannot validate fleet database: {exc}") from exc
    finally:
        connection.close()
    return versions


def sqlite_online_backup(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise BackupError("fleet database must be a regular non-symlink file")
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        destination_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite online backup failed: {exc}") from exc
    finally:
        destination_connection.close()
        source_connection.close()
    os.chmod(destination, 0o600)


def referenced_objects(database: Path) -> list[dict[str, Any]]:
    connection = _immutable_connection(database)
    connection.row_factory = sqlite3.Row
    try:
        staging = connection.execute(
            "SELECT session_id FROM recording_sessions WHERE state = 'STAGING' ORDER BY session_id"
        ).fetchall()
        if staging:
            raise BackupError(
                "finalize or discard STAGING recording sessions before backup: "
                + ", ".join(row["session_id"] for row in staging)
            )
        objects = [
            {
                "object_key": row["object_key"],
                "size_bytes": row["expected_size_bytes"],
                "sha256": row["expected_sha256"],
            }
            for row in connection.execute(
                """
                SELECT artifacts.object_key, artifacts.expected_size_bytes,
                       artifacts.expected_sha256
                FROM recording_artifacts AS artifacts
                JOIN recording_sessions AS sessions
                  ON sessions.session_id = artifacts.session_id
                WHERE sessions.state = 'COMPLETE' AND artifacts.state = 'COMPLETE'
                ORDER BY artifacts.object_key
                """
            ).fetchall()
        ]
        for row in connection.execute(
            """
            SELECT session_id, manifest_json, manifest_sha256
            FROM recording_sessions
            WHERE state = 'COMPLETE'
            ORDER BY session_id
            """
        ).fetchall():
            payload = row["manifest_json"].encode("utf-8")
            objects.append(
                {
                    "object_key": f"sessions/{row['session_id']}/session-manifest.json",
                    "size_bytes": len(payload),
                    "sha256": row["manifest_sha256"],
                }
            )
        quality_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'recording_quality_reports'"
        ).fetchone()
        if quality_table is not None:
            objects.extend(
                {
                    "object_key": row["object_key"],
                    "size_bytes": row["report_size_bytes"],
                    "sha256": row["report_sha256"],
                }
                for row in connection.execute(
                    """
                    SELECT quality.object_key, quality.report_size_bytes,
                           quality.report_sha256
                    FROM recording_quality_reports AS quality
                    JOIN recording_sessions AS sessions
                      ON sessions.session_id = quality.session_id
                    WHERE sessions.state = 'COMPLETE'
                    ORDER BY quality.object_key
                    """
                ).fetchall()
            )
        derivation_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'dataset_derivations'"
        ).fetchone()
        if derivation_table is not None:
            for row in connection.execute(
                """
                SELECT output_object_key, output_size_bytes, output_sha256,
                       manifest_object_key, manifest_size_bytes, manifest_sha256
                FROM dataset_derivations
                WHERE state = 'COMPLETE'
                ORDER BY derivation_id
                """
            ).fetchall():
                objects.extend(
                    (
                        {
                            "object_key": row["output_object_key"],
                            "size_bytes": row["output_size_bytes"],
                            "sha256": row["output_sha256"],
                        },
                        {
                            "object_key": row["manifest_object_key"],
                            "size_bytes": row["manifest_size_bytes"],
                            "sha256": row["manifest_sha256"],
                        },
                    )
                )
        aas_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'aas_artifacts'"
        ).fetchone()
        if aas_table is not None:
            for row in connection.execute(
                """
                SELECT object_key, size_bytes, sha256,
                       environment_object_key, environment_size_bytes,
                       environment_sha256
                FROM aas_artifacts
                ORDER BY sha256
                """
            ).fetchall():
                objects.extend(
                    (
                        {
                            "object_key": row["object_key"],
                            "size_bytes": row["size_bytes"],
                            "sha256": row["sha256"],
                        },
                        {
                            "object_key": row["environment_object_key"],
                            "size_bytes": row["environment_size_bytes"],
                            "sha256": row["environment_sha256"],
                        },
                    )
                )
        g6b_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'g6b_evidence_submissions'
            """
        ).fetchone()
        g6b_artifact_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'g6b_supporting_artifacts'
            """
        ).fetchone()
        if g6b_artifact_table is not None:
            objects.extend(
                {
                    "object_key": row["object_key"],
                    "size_bytes": row["size_bytes"],
                    "sha256": row["sha256"],
                }
                for row in connection.execute(
                    """
                    SELECT object_key, size_bytes, sha256
                    FROM g6b_supporting_artifacts
                    ORDER BY artifact_id
                    """
                ).fetchall()
            )
        if g6b_table is not None:
            objects.extend(
                {
                    "object_key": row["object_key"],
                    "size_bytes": row["payload_size_bytes"],
                    "sha256": row["payload_sha256"],
                }
                for row in connection.execute(
                    """
                    SELECT object_key, payload_size_bytes, payload_sha256
                    FROM g6b_evidence_submissions
                    ORDER BY case_id, revision
                    """
                ).fetchall()
            )
    except sqlite3.Error as exc:
        raise BackupError(f"cannot enumerate recording objects: {exc}") from exc
    finally:
        connection.close()
    unique: dict[str, dict[str, Any]] = {}
    for item in objects:
        existing = unique.get(item["object_key"])
        if existing is not None and existing != item:
            raise BackupError(
                f"Object Store key has conflicting catalog metadata: {item['object_key']}"
            )
        unique[item["object_key"]] = item
    return sorted(unique.values(), key=lambda item: item["object_key"])


async def copy_object(
    source: Path,
    destination: FilesystemObjectStore,
    item: dict[str, Any],
) -> None:
    expected_size = int(item["size_bytes"])
    expected_sha256 = str(item["sha256"])
    actual_size, actual_sha256 = sha256_file(source)
    if actual_size != expected_size or actual_sha256 != expected_sha256:
        raise BackupError(f"source object integrity mismatch: {item['object_key']}")

    async def blocks():
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    return
                yield block
        finally:
            os.close(descriptor)

    await destination.put_stream(
        item["object_key"],
        blocks(),
        size_bytes=expected_size,
        sha256=expected_sha256,
    )


async def create_backup(
    *,
    db_path: Path,
    object_store_root: Path,
    output: Path,
) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        raise BackupError("backup output already exists")
    if object_store_root.is_symlink() or not object_store_root.is_dir():
        raise BackupError("Object Store root must already exist and may not be a symlink")
    partial = output.with_name(output.name + ".partial")
    if partial.exists() or partial.is_symlink():
        raise BackupError("partial backup output already exists")
    partial.mkdir(mode=0o700, parents=True)
    try:
        backup_db = partial / "fleet.db"
        sqlite_online_backup(db_path, backup_db)
        versions = validate_database(backup_db)
        objects = referenced_objects(backup_db)
        source_store = FilesystemObjectStore(object_store_root)
        backup_store = FilesystemObjectStore(partial / "objects")
        for item in objects:
            source_path = source_store.open_path(
                item["object_key"],
                size_bytes=item["size_bytes"],
                sha256=item["sha256"],
            )
            await copy_object(source_path, backup_store, item)
        db_size, db_sha256 = sha256_file(backup_db)
        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "database": {
                "path": "fleet.db",
                "size_bytes": db_size,
                "sha256": db_sha256,
                "schema_versions": versions,
            },
            "objects": objects,
        }
        manifest_path = partial / MANIFEST_NAME
        with manifest_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(manifest_path, 0o600)
        descriptor = os.open(partial, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        partial.rename(output)
        return manifest
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def load_backup_manifest(backup: Path) -> dict[str, Any]:
    if backup.is_symlink() or not backup.is_dir():
        raise BackupError("backup must be a regular non-symlink directory")
    path = backup / MANIFEST_NAME
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise BackupError("backup manifest is missing or unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"invalid backup manifest: {exc}") from exc
    if payload.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise BackupError("unsupported backup schema version")
    database = payload.get("database")
    objects = payload.get("objects")
    if not isinstance(database, dict) or not isinstance(objects, list):
        raise BackupError("backup manifest shape is invalid")
    return payload


def verify_backup(backup: Path) -> dict[str, Any]:
    manifest = load_backup_manifest(backup)
    database = manifest["database"]
    db_path = backup / database["path"]
    db_size, db_sha256 = sha256_file(db_path)
    if db_size != database["size_bytes"] or db_sha256 != database["sha256"]:
        raise BackupError("backup database integrity mismatch")
    versions = validate_database(db_path)
    if versions != database["schema_versions"]:
        raise BackupError("backup database schema versions differ from the manifest")
    store = FilesystemObjectStore(backup / "objects")
    expected_paths = set()
    try:
        for item in manifest["objects"]:
            key = item.get("object_key")
            if not isinstance(key, str):
                raise BackupError("backup object key is invalid")
            store.open_path(
                key,
                size_bytes=int(item["size_bytes"]),
                sha256=str(item["sha256"]),
            )
            expected_paths.add(PurePosixPath(key).as_posix())
    except ObjectStoreError as exc:
        raise BackupError(f"backup Object Store integrity check failed: {exc}") from exc
    actual_paths = set()
    for path in store.root.rglob("*"):
        if path.is_symlink():
            raise BackupError("backup Object Store contains a symbolic link")
        if path.is_file():
            actual_paths.add(path.relative_to(store.root).as_posix())
    if actual_paths != expected_paths:
        raise BackupError("backup Object Store contains missing or unexpected files")
    return {
        "passed": True,
        "schema_versions": versions,
        "object_count": len(expected_paths),
        "object_size_bytes": sum(int(item["size_bytes"]) for item in manifest["objects"]),
        "database_sha256": db_sha256,
    }


async def restore_backup(
    *,
    backup: Path,
    db_path: Path,
    object_store_root: Path,
) -> dict[str, Any]:
    result = verify_backup(backup)
    if db_path.exists() or db_path.is_symlink():
        raise BackupError("restore database destination already exists")
    if object_store_root.exists() or object_store_root.is_symlink():
        raise BackupError("restore Object Store destination already exists")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    object_store_root.parent.mkdir(parents=True, exist_ok=True)
    db_partial = db_path.with_name(db_path.name + ".partial")
    object_partial = object_store_root.with_name(object_store_root.name + ".partial")
    if db_partial.exists() or object_partial.exists():
        raise BackupError("partial restore destination already exists")
    manifest = load_backup_manifest(backup)
    source_store = FilesystemObjectStore(backup / "objects")
    destination_store = FilesystemObjectStore(object_partial)
    objects_published = False
    try:
        for item in manifest["objects"]:
            source = source_store.open_path(
                item["object_key"],
                size_bytes=item["size_bytes"],
                sha256=item["sha256"],
            )
            await copy_object(source, destination_store, item)
        source_db = backup / manifest["database"]["path"]
        with source_db.open("rb") as source, db_partial.open("xb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(db_partial, 0o600)
        validate_database(db_partial)
        object_partial.rename(object_store_root)
        objects_published = True
        db_partial.rename(db_path)
    except BaseException:
        db_partial.unlink(missing_ok=True)
        shutil.rmtree(object_partial, ignore_errors=True)
        if objects_published and not db_path.exists():
            shutil.rmtree(object_store_root, ignore_errors=True)
        raise
    return result


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SANG Central backup and restore")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--db-path", type=Path, required=True)
    backup.add_argument("--object-store-root", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--backup", type=Path, required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--db-path", type=Path, required=True)
    restore.add_argument("--object-store-root", type=Path, required=True)
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    try:
        if options.command == "backup":
            result = asyncio.run(
                create_backup(
                    db_path=options.db_path,
                    object_store_root=options.object_store_root,
                    output=options.output,
                )
            )
        elif options.command == "verify":
            result = verify_backup(options.backup)
        else:
            result = asyncio.run(
                restore_backup(
                    backup=options.backup,
                    db_path=options.db_path,
                    object_store_root=options.object_store_root,
                )
            )
    except (BackupError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1:])
