"""Dedicated mTLS data ingress for finalized Edge recording sessions."""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import sqlite3
import ssl
import sys
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit

from cryptography import x509
from pydantic import ValidationError

from fleet_control_plane.agent_ingress import (
    AgentIngressError,
    agent_identity_uri,
    create_server_tls_context,
)
from fleet_control_plane.recording_models import RecordingFinalize, RecordingManifest
from fleet_control_plane.recording_quality import RecordingQualityService
from fleet_control_plane.recording_storage import (
    FilesystemObjectStore,
    ObjectStoreError,
    RecordingCatalog,
    RecordingConflict,
    RecordingInvalid,
    RecordingNotFound,
)
from fleet_control_plane.status import utc_now
from fleet_control_plane.store import AgentAuthenticationError, FleetStore
from sang_security.certificates import CertificateError, certificate_sha256
from sang_security.refs import SecretReferenceError, SecretResolver


MAX_MANIFEST_BYTES = 256 * 1024
STREAM_BLOCK_BYTES = 1024 * 1024
RECORDING_BASE_PATH = "/api/v1/recording-sessions"


class LengthRequired(RecordingInvalid):
    """An upload request omitted its required exact body length."""


class ManifestTooLarge(ValueError):
    """A JSON control document exceeds the fixed ingress bound."""


class UnsupportedMediaType(ValueError):
    """A JSON control request used a different media type."""


def _decode_path(value: str) -> str:
    try:
        return unquote_to_bytes(value).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("request path is not valid UTF-8") from exc


class RecordingIngressServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        *,
        store: FleetStore,
        object_store: FilesystemObjectStore,
        tls_context: ssl.SSLContext | None = None,
        tls_context_factory: Callable[[], ssl.SSLContext] | None = None,
        now: Callable[[], datetime] = utc_now,
        connection_timeout_s: float = 30.0,
        quality_policy_root: Path | None = None,
    ) -> None:
        if (tls_context is None) == (tls_context_factory is None):
            raise ValueError("provide exactly one TLS context or TLS context factory")
        self.store = store
        self.catalog = RecordingCatalog(store.path)
        self.object_store = object_store
        self.now = now
        self.connection_timeout_s = connection_timeout_s
        self.quality = (
            None
            if quality_policy_root is None
            else RecordingQualityService(
                self.catalog,
                object_store,
                quality_policy_root,
            )
        )
        self._quality_executor = (
            None
            if self.quality is None
            else ThreadPoolExecutor(max_workers=1, thread_name_prefix="recording-quality")
        )
        self._tls_context = tls_context
        self._tls_context_factory = tls_context_factory
        super().__init__(address, RecordingIngressHandler)

    @staticmethod
    def _quality_done(session_id: str, future: Future) -> None:
        try:
            future.result()
        except Exception as exc:
            print(
                f"recording quality analysis failed for {session_id}: {exc}",
                file=sys.stderr,
            )

    def schedule_quality(self, session_id: str) -> None:
        if self.quality is None or self._quality_executor is None:
            return
        future = self._quality_executor.submit(self.quality.analyze, session_id)
        future.add_done_callback(lambda result: self._quality_done(session_id, result))

    def server_close(self) -> None:
        super().server_close()
        if self._quality_executor is not None:
            self._quality_executor.shutdown(wait=True, cancel_futures=True)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.connection_timeout_s)
        try:
            context = (
                self._tls_context_factory()
                if self._tls_context_factory is not None
                else self._tls_context
            )
            secured = context.wrap_socket(
                connection,
                server_side=True,
                do_handshake_on_connect=False,
            )
        except BaseException:
            connection.close()
            raise
        return secured, address

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (ssl.SSLError, TimeoutError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class RecordingIngressHandler(BaseHTTPRequestHandler):
    server: RecordingIngressServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        del format, args

    def _json(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    def _request_path(self) -> str:
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            raise ValueError("recording ingress paths may not contain query or fragment")
        return parsed.path

    def _content_length(self, *, maximum: int | None = None) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise LengthRequired("request requires Content-Length")
        try:
            length = int(raw)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0:
            raise ValueError("Content-Length cannot be negative")
        if maximum is not None and length > maximum:
            raise ManifestTooLarge("recording manifest exceeds 256 KiB")
        return length

    def _read_json(self, *, maximum: int) -> Any:
        if self.headers.get_content_type() != "application/json":
            raise UnsupportedMediaType("content-type must be application/json")
        content_length = self._content_length(maximum=maximum)
        if content_length == 0:
            raise ValueError("JSON body may not be empty")
        payload = self.rfile.read(content_length)
        if len(payload) != content_length:
            raise ValueError("request body ended before Content-Length")
        return json.loads(payload)

    def _authenticated_agent(self) -> str:
        peer_der = self.connection.getpeercert(binary_form=True)
        if not peer_der:
            raise AgentIngressError("TLS peer certificate is unavailable")
        certificate = x509.load_der_x509_certificate(peer_der)
        identity_uri = agent_identity_uri(certificate)
        agent_id = identity_uri.removeprefix("urn:sang:agent:")
        self.server.store.authenticate_agent_certificate(
            agent_id=agent_id,
            certificate_sha256=certificate_sha256(certificate),
            certificate_identity_uri=identity_uri,
            occurred_at=self.server.now(),
        )
        return agent_id

    @staticmethod
    def _require_owner(agent_id: str, session: dict[str, Any]) -> None:
        if session["agent_id"] != agent_id:
            raise AgentAuthenticationError(
                "Agent certificate cannot access another Agent's recording session"
            )

    async def _body_blocks(self, content_length: int) -> AsyncIterator[bytes]:
        remaining = content_length
        while remaining:
            block = self.rfile.read(min(STREAM_BLOCK_BYTES, remaining))
            if not block:
                return
            remaining -= len(block)
            yield block

    def _register(self, agent_id: str) -> None:
        manifest = RecordingManifest.model_validate(
            self._read_json(maximum=MAX_MANIFEST_BYTES)
        )
        if manifest.agent_id != agent_id:
            raise AgentAuthenticationError(
                "recording manifest agent_id differs from the Agent certificate"
            )
        result = self.server.catalog.register(
            manifest,
            object_store=self.server.object_store,
            created_by=None,
            occurred_at=self.server.now(),
        )
        self._json(HTTPStatus.CREATED, result)

    def _finalize(self, agent_id: str, session_id: str) -> None:
        session = self.server.catalog.get_session(session_id)
        self._require_owner(agent_id, session)
        body = RecordingFinalize.model_validate(
            self._read_json(maximum=MAX_MANIFEST_BYTES)
        )
        result = asyncio.run(
            self.server.catalog.finalize_async(
                session_id,
                expected_manifest_sha256=body.expected_manifest_sha256,
                object_store=self.server.object_store,
                occurred_at=self.server.now(),
            )
        )
        self.server.schedule_quality(session_id)
        self._json(HTTPStatus.OK, result)

    def _upload(self, agent_id: str, session_id: str, artifact_path: str) -> None:
        session = self.server.catalog.get_session(session_id)
        self._require_owner(agent_id, session)
        expected = self.server.catalog.artifact_expectation(
            session_id,
            artifact_path,
        )
        content_length = self._content_length()
        if content_length != expected["expected_size_bytes"]:
            raise RecordingInvalid(
                "artifact Content-Length differs from the recording manifest"
            )
        asyncio.run(
            self.server.object_store.put_stream(
                expected["object_key"],
                self._body_blocks(content_length),
                size_bytes=expected["expected_size_bytes"],
                sha256=expected["expected_sha256"],
            )
        )
        result = self.server.catalog.mark_uploaded(
            session_id,
            artifact_path,
            occurred_at=self.server.now(),
        )
        self._json(HTTPStatus.OK, result)

    def _dispatch(self, action: Callable[[], None]) -> None:
        try:
            action()
        except ManifestTooLarge as exc:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)})
        except UnsupportedMediaType as exc:
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": str(exc)})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
        except ValidationError as exc:
            self._json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {
                    "error": "recording schema validation failed",
                    "details": exc.errors(include_context=False),
                },
            )
        except (CertificateError, AgentIngressError, AgentAuthenticationError) as exc:
            self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except RecordingNotFound as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except RecordingConflict as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except LengthRequired as exc:
            self._json(HTTPStatus.LENGTH_REQUIRED, {"error": str(exc)})
        except RecordingInvalid as exc:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except TimeoutError:
            self._json(HTTPStatus.REQUEST_TIMEOUT, {"error": "upload body timed out"})
        except ObjectStoreError as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        except (OSError, sqlite3.Error) as exc:
            print(f"recording ingress internal error: {exc}", file=sys.stderr)
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "recording ingress internal error"},
            )

    def do_GET(self) -> None:
        def handle() -> None:
            if self._request_path() != "/healthz":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self._json(
                HTTPStatus.OK,
                {"status": "ok", "scope": "recording-mtls-ingress"},
            )

        self._dispatch(handle)

    def do_POST(self) -> None:
        def handle() -> None:
            path = self._request_path()
            agent_id = self._authenticated_agent()
            if path == RECORDING_BASE_PATH:
                self._register(agent_id)
                return
            prefix = f"{RECORDING_BASE_PATH}/"
            suffix = "/finalize"
            if path.startswith(prefix) and path.endswith(suffix):
                encoded_session_id = path[len(prefix) : -len(suffix)]
                if not encoded_session_id or "/" in encoded_session_id:
                    raise RecordingNotFound("unknown recording ingress route")
                self._finalize(agent_id, _decode_path(encoded_session_id))
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        self._dispatch(handle)

    def do_PUT(self) -> None:
        def handle() -> None:
            path = self._request_path()
            agent_id = self._authenticated_agent()
            prefix = f"{RECORDING_BASE_PATH}/"
            if not path.startswith(prefix):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            remainder = path[len(prefix) :]
            encoded_session_id, separator, encoded_artifact_path = remainder.partition(
                "/artifacts/"
            )
            if (
                not separator
                or not encoded_session_id
                or "/" in encoded_session_id
                or not encoded_artifact_path
            ):
                raise RecordingNotFound("unknown recording ingress route")
            self._upload(
                agent_id,
                _decode_path(encoded_session_id),
                _decode_path(encoded_artifact_path),
            )

        self._dispatch(handle)


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SANG Agent mTLS recording ingress")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path.home() / ".local" / "share" / "sang-fleet" / "fleet.db",
    )
    parser.add_argument("--object-store-root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8444)
    parser.add_argument("--secret-root", type=Path, required=True)
    parser.add_argument("--server-certificate-ref", required=True)
    parser.add_argument("--server-private-key-ref", required=True)
    parser.add_argument("--client-ca-crl-bundle-ref", required=True)
    parser.add_argument("--connection-timeout-s", type=float, default=30.0)
    parser.add_argument("--quality-policy-root", type=Path)
    options = parser.parse_args(args)
    if not 1 <= options.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if not math.isfinite(options.connection_timeout_s) or options.connection_timeout_s <= 0:
        parser.error("connection-timeout-s must be positive")
    return options


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    try:
        resolver = SecretResolver(options.secret_root)
        certificate_chain = resolver.resolve(options.server_certificate_ref)
        private_key = resolver.resolve(options.server_private_key_ref, private=True)
        client_ca_crl_bundle = resolver.resolve(options.client_ca_crl_bundle_ref)
        create_server_tls_context(
            certificate_chain=certificate_chain,
            private_key=private_key,
            client_ca_crl_bundle=client_ca_crl_bundle,
        )
        object_store = FilesystemObjectStore(options.object_store_root)
    except (
        ObjectStoreError,
        SecretReferenceError,
        OSError,
        ssl.SSLError,
        ValueError,
    ) as exc:
        raise SystemExit(f"invalid recording ingress configuration: {exc}") from exc
    server = RecordingIngressServer(
        (options.host, options.port),
        store=FleetStore(options.db_path),
        object_store=object_store,
        tls_context_factory=lambda: create_server_tls_context(
            certificate_chain=certificate_chain,
            private_key=private_key,
            client_ca_crl_bundle=client_ca_crl_bundle,
        ),
        connection_timeout_s=options.connection_timeout_s,
        quality_policy_root=options.quality_policy_root,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main(sys.argv[1:])
