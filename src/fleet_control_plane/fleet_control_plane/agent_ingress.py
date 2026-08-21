"""Dedicated mTLS-only HTTPS ingress for remote Agent heartbeat v2."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import ssl
import sys
from typing import Any

from cryptography import x509
from pydantic import ValidationError

from fleet_control_plane.models import AgentHeartbeatV1
from fleet_control_plane.status import isoformat_z, utc_now
from fleet_control_plane.store import (
    AgentAuthenticationError,
    AgentReplayError,
    FleetStore,
)
from sang_security.certificates import (
    CertificateError,
    certificate_sha256,
    certificate_uri_sans,
)
from sang_security.refs import SecretReferenceError, SecretResolver


MAX_HEARTBEAT_BYTES = 256 * 1024
AGENT_URI_PREFIX = "urn:sang:agent:"


class AgentIngressError(ValueError):
    """The mTLS request is malformed before registry authorization."""


def agent_identity_uri(certificate: x509.Certificate) -> str:
    identities = [
        value
        for value in certificate_uri_sans(certificate)
        if value.startswith(AGENT_URI_PREFIX)
    ]
    if len(identities) != 1:
        raise AgentIngressError(
            "Agent client certificate must contain exactly one urn:sang:agent URI"
        )
    agent_id = identities[0].removeprefix(AGENT_URI_PREFIX)
    if not agent_id or identities[0] != f"{AGENT_URI_PREFIX}{agent_id}":
        raise AgentIngressError("Agent certificate identity URI is malformed")
    return identities[0]


def create_server_tls_context(
    *,
    certificate_chain: Path,
    private_key: Path,
    client_ca_crl_bundle: Path,
) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(
        certfile=str(certificate_chain),
        keyfile=str(private_key),
    )
    context.load_verify_locations(cafile=str(client_ca_crl_bundle))
    context.verify_flags |= (
        ssl.VERIFY_CRL_CHECK_LEAF | ssl.VERIFY_X509_PARTIAL_CHAIN
    )
    return context


class AgentIngressServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        *,
        store: FleetStore,
        tls_context: ssl.SSLContext | None = None,
        tls_context_factory: Callable[[], ssl.SSLContext] | None = None,
        now: Callable[[], datetime] = utc_now,
        maximum_age_s: float = 12.0,
        maximum_future_s: float = 2.0,
        connection_timeout_s: float = 3.0,
    ) -> None:
        if (tls_context is None) == (tls_context_factory is None):
            raise ValueError("provide exactly one TLS context or TLS context factory")
        self.store = store
        self.now = now
        self.maximum_age_s = maximum_age_s
        self.maximum_future_s = maximum_future_s
        self.connection_timeout_s = connection_timeout_s
        self._tls_context = tls_context
        self._tls_context_factory = tls_context_factory
        super().__init__(address, AgentIngressHandler)

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
        if isinstance(error, (ssl.SSLError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class AgentIngressHandler(BaseHTTPRequestHandler):
    server: AgentIngressServer
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

    def _observed_endpoint(self) -> str:
        host, port = self.connection.getsockname()[:2]
        rendered_host = f"[{host}]" if ":" in host else host
        return f"https://{rendered_host}:{port}"

    def do_GET(self) -> None:
        if self.path != "/healthz":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._json(
            HTTPStatus.OK,
            {"status": "ok", "scope": "agent-mtls-ingress"},
        )

    def do_POST(self) -> None:
        if self.path != "/api/v1/agents/heartbeat":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "content-type must be application/json"},
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid content-length"})
            return
        if not 1 <= content_length <= MAX_HEARTBEAT_BYTES:
            self._json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "heartbeat body must contain 1 to 262144 bytes"},
            )
            return
        try:
            peer_der = self.connection.getpeercert(binary_form=True)
            if not peer_der:
                raise AgentIngressError("TLS peer certificate is unavailable")
            certificate = x509.load_der_x509_certificate(peer_der)
            identity_uri = agent_identity_uri(certificate)
            fingerprint = certificate_sha256(certificate)
            raw = json.loads(self.rfile.read(content_length))
            heartbeat = AgentHeartbeatV1.model_validate(raw)
            received_at = self.server.now()
            accepted = self.server.store.secure_upsert(
                heartbeat,
                certificate_sha256=fingerprint,
                certificate_identity_uri=identity_uri,
                received_at=received_at,
                maximum_age_s=self.server.maximum_age_s,
                maximum_future_s=self.server.maximum_future_s,
            )
            profile = self.server.store.get_agent_connection_profile(
                heartbeat.agent_id,
                "https_agent",
            )
            if profile is not None and profile["enabled"]:
                self.server.store.record_connection_observation(
                    profile["profile_id"],
                    observed_at=received_at,
                    observed_endpoint=self._observed_endpoint(),
                    observed_application_uri=identity_uri,
                    peer_certificate_sha256=fingerprint,
                    peer_address=f"{self.client_address[0]}:{self.client_address[1]}",
                    succeeded=True,
                )
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return
        except ValidationError as exc:
            self._json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {
                    "error": "heartbeat schema validation failed",
                    "details": exc.errors(include_context=False),
                },
            )
            return
        except (CertificateError, AgentIngressError) as exc:
            self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
            return
        except AgentAuthenticationError as exc:
            self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
            return
        except AgentReplayError as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        self._json(
            HTTPStatus.ACCEPTED,
            {
                "received_at": isoformat_z(received_at),
                "next_interval_s": 5,
                "accepted_sequence": accepted["sequence"],
            },
        )


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SANG remote Agent mTLS ingress")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path.home() / ".local" / "share" / "sang-fleet" / "fleet.db",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--secret-root", type=Path, required=True)
    parser.add_argument("--server-certificate-ref", required=True)
    parser.add_argument("--server-private-key-ref", required=True)
    parser.add_argument("--client-ca-crl-bundle-ref", required=True)
    parser.add_argument("--maximum-age-s", type=float, default=12.0)
    parser.add_argument("--maximum-future-s", type=float, default=2.0)
    parser.add_argument("--connection-timeout-s", type=float, default=3.0)
    options = parser.parse_args(args)
    if not 1 <= options.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if any(
        not math.isfinite(value) or value <= 0.0
        for value in (
            options.maximum_age_s,
            options.maximum_future_s,
            options.connection_timeout_s,
        )
    ):
        parser.error("heartbeat age bounds must be positive")
    return options


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    try:
        resolver = SecretResolver(options.secret_root)
        certificate_chain = resolver.resolve(options.server_certificate_ref)
        private_key = resolver.resolve(
            options.server_private_key_ref,
            private=True,
        )
        client_ca_crl_bundle = resolver.resolve(
            options.client_ca_crl_bundle_ref
        )
        create_server_tls_context(
            certificate_chain=certificate_chain,
            private_key=private_key,
            client_ca_crl_bundle=client_ca_crl_bundle,
        )
    except (SecretReferenceError, OSError, ssl.SSLError, ValueError) as exc:
        raise SystemExit(f"invalid Agent ingress TLS material: {exc}") from exc
    server = AgentIngressServer(
        (options.host, options.port),
        store=FleetStore(options.db_path),
        tls_context_factory=lambda: create_server_tls_context(
            certificate_chain=certificate_chain,
            private_key=private_key,
            client_ca_crl_bundle=client_ca_crl_bundle,
        ),
        maximum_age_s=options.maximum_age_s,
        maximum_future_s=options.maximum_future_s,
        connection_timeout_s=options.connection_timeout_s,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
