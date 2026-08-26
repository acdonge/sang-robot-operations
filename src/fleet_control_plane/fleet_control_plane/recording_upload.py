"""Upload one finalized Edge recording session to Central."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import ssl
import stat
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from pydantic import ValidationError

from fleet_control_plane.recording_models import RecordingManifest


MAX_MANIFEST_BYTES = 256 * 1024


def private_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular non-symlink file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ValueError(f"{label} must not be accessible by group or other users")
    return path


def regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular non-symlink file")
    return path


def read_manifest(session_dir: Path) -> RecordingManifest:
    if session_dir.is_symlink() or not session_dir.is_dir():
        raise ValueError("session directory must be a regular non-symlink directory")
    path = session_dir / "session-manifest.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("finalized session-manifest.json is missing")
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("session manifest exceeds 256 KiB")
    try:
        manifest = RecordingManifest.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise ValueError(f"invalid session manifest: {exc}") from exc
    if session_dir.name != manifest.session_id:
        raise ValueError("session directory name must match session_id")
    return manifest


def verify_local_artifact(session_dir: Path, item) -> Path:
    resolved_root = session_dir.resolve()
    path = session_dir / item.path
    if path.is_symlink():
        raise ValueError(f"artifact may not be a symbolic link: {item.path}")
    resolved = path.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"artifact escapes the session: {item.path}") from exc
    if not resolved.is_file() or resolved.stat().st_size != item.size_bytes:
        raise ValueError(f"artifact size differs from manifest: {item.path}")
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != item.sha256:
        raise ValueError(f"artifact SHA-256 differs from manifest: {item.path}")
    return resolved


def verify_local_artifacts(session_dir: Path, manifest: RecordingManifest) -> None:
    for item in manifest.files:
        verify_local_artifact(session_dir, item)


def create_client_context(
    *,
    ca_bundle: Path | None,
    client_certificate: Path | None = None,
    client_private_key: Path | None = None,
) -> ssl.SSLContext:
    if (client_certificate is None) != (client_private_key is None):
        raise ValueError("client certificate and private key must be provided together")
    if client_certificate is not None and ca_bundle is None:
        raise ValueError("mTLS recording upload requires a CA and CRL bundle")
    verified_ca = None if ca_bundle is None else regular_file(ca_bundle, "CA bundle")
    context = ssl.create_default_context(
        purpose=ssl.Purpose.SERVER_AUTH,
        cafile=None if verified_ca is None else str(verified_ca),
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if client_certificate is not None:
        certificate = regular_file(client_certificate, "client certificate chain")
        private_key = private_file(client_private_key, "client private key")
        context.load_cert_chain(certfile=str(certificate), keyfile=str(private_key))
        context.verify_flags |= (
            ssl.VERIFY_CRL_CHECK_LEAF | ssl.VERIFY_X509_PARTIAL_CHAIN
        )
    return context


def request_json(
    url: str,
    *,
    method: str,
    token: str | None,
    context: ssl.SSLContext,
    payload: dict | None = None,
    data=None,
    content_length: int | None = None,
) -> dict:
    headers = {
        "Accept": "application/json",
        "User-Agent": "sang-recording-upload/1",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    encoded = data
    if payload is not None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(encoded))
    elif content_length is not None:
        headers["Content-Type"] = "application/octet-stream"
        headers["Content-Length"] = str(content_length)
    request = Request(url, data=encoded, headers=headers, method=method)
    try:
        with urlopen(request, timeout=60.0, context=context) as response:
            body = response.read(1024 * 1024 + 1)
    except HTTPError as exc:
        detail = exc.read(16 * 1024).decode("utf-8", errors="replace")
        raise RuntimeError(f"Central returned HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Central recording upload failed: {exc}") from exc
    if len(body) > 1024 * 1024:
        raise RuntimeError("Central recording response exceeds 1 MiB")
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Central returned an invalid JSON response") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Central returned an invalid recording response")
    return result


def validate_central_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("central-url must not contain credentials, query or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("central-url contains an invalid port") from exc
    if parsed.hostname is None or port is None:
        raise ValueError("central-url must include host and port")
    if parsed.path not in {"", "/"}:
        raise ValueError("central-url must contain only the scheme, host and port")
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("remote Central recording upload requires HTTPS")
    return value.rstrip("/")


def upload_session(
    session_dir: Path,
    *,
    central_url: str,
    context: ssl.SSLContext,
    token: str | None = None,
) -> dict:
    manifest = read_manifest(session_dir)
    base = f"{validate_central_url(central_url)}/api/v1/recording-sessions"
    registered = request_json(
        base,
        method="POST",
        token=token,
        context=context,
        payload=manifest.model_dump(mode="json"),
    )
    if registered.get("state") == "COMPLETE":
        return registered
    completed = {
        item["path"]
        for item in registered.get("artifacts", [])
        if item.get("state") == "COMPLETE"
    }
    for item in manifest.files:
        if item.path in completed:
            continue
        artifact = verify_local_artifact(session_dir, item)
        artifact_url = (
            f"{base}/{quote(manifest.session_id, safe='')}/artifacts/"
            f"{quote(item.path, safe='/')}"
        )
        with artifact.open("rb") as stream:
            request_json(
                artifact_url,
                method="PUT",
                token=token,
                context=context,
                data=stream,
                content_length=item.size_bytes,
            )
    return request_json(
        f"{base}/{quote(manifest.session_id, safe='')}/finalize",
        method="POST",
        token=token,
        context=context,
        payload={
            "expected_manifest_sha256": registered["manifest_sha256"],
        },
    )


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload one finalized SANG MCAP session to Central"
    )
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--central-url", required=True)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--ca-bundle", type=Path)
    parser.add_argument("--client-certificate", type=Path)
    parser.add_argument("--client-private-key", type=Path)
    options = parser.parse_args(args)
    try:
        options.central_url = validate_central_url(options.central_url)
    except ValueError as exc:
        parser.error(str(exc))
    has_token = options.token_file is not None
    has_certificate = options.client_certificate is not None
    has_private_key = options.client_private_key is not None
    if has_certificate != has_private_key:
        parser.error("client certificate and private key must be provided together")
    if has_token == has_certificate:
        parser.error("choose exactly one authentication mode: token or mTLS")
    if has_certificate and options.ca_bundle is None:
        parser.error("mTLS recording upload requires --ca-bundle")
    if has_certificate and urlsplit(options.central_url).scheme != "https":
        parser.error("mTLS recording upload requires HTTPS")
    return options


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    token = None
    try:
        if options.token_file is not None:
            token_path = private_file(options.token_file, "service token file")
            token = token_path.read_text(encoding="utf-8").strip()
            if not token:
                raise ValueError("service token file is empty")
        context = create_client_context(
            ca_bundle=options.ca_bundle,
            client_certificate=options.client_certificate,
            client_private_key=options.client_private_key,
        )
        completed_session = upload_session(
            options.session_dir,
            central_url=options.central_url,
            context=context,
            token=token,
        )
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        token = None
    print(json.dumps(completed_session, indent=2, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1:])
