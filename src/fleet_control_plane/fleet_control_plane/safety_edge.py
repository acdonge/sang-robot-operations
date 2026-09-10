"""Loopback Unity snapshot evaluator and command-free Central forwarder."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import ssl
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import uvicorn

from fleet_control_plane.safety_models import (
    ClockQuality,
    SafetyAssessmentV1,
    SafetyPolicyV1,
    SafetySnapshotV1,
)
from fleet_control_plane.safety_shadow import evaluate_snapshot
from fleet_control_plane.safety_v2 import SafetyEvidenceIngressV2


MAX_SAFETY_SNAPSHOT_BYTES = 256 * 1024


class AssessmentSubmitter(Protocol):
    def __call__(self, assessment: SafetyAssessmentV1) -> dict[str, Any]: ...


class EvidenceSubmitterV2(Protocol):
    def __call__(self, evidence: SafetyEvidenceIngressV2) -> dict[str, Any]: ...


class CentralAssessmentSubmitter:
    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: str | None = None,
        ca_file: Path | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("Control Plane URL must be an HTTP(S) origin")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("unencrypted safety forwarding is restricted to loopback")
        self.endpoint = base_url.rstrip("/") + "/api/v1/safety/assessments"
        self.bearer_token = bearer_token
        self.timeout_s = timeout_s
        self.context = (
            None
            if parsed.scheme == "http"
            else ssl.create_default_context(cafile=None if ca_file is None else str(ca_file))
        )

    def __call__(self, assessment: SafetyAssessmentV1) -> dict[str, Any]:
        body = assessment.model_dump_json().encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = UrlRequest(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urlopen(
                request,
                timeout=self.timeout_s,
                context=self.context,
            ) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Central rejected the safety assessment with HTTP {exc.code}: {detail}"
            ) from exc
        except (URLError, OSError) as exc:
            raise RuntimeError(f"Central safety transport failed: {exc}") from exc


class CentralEvidenceSubmitterV2:
    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: str | None = None,
        ca_file: Path | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("Control Plane URL must be an HTTP(S) origin")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("unencrypted safety forwarding is restricted to loopback")
        self.endpoint = base_url.rstrip("/") + "/api/v2/safety/evidence"
        self.bearer_token = bearer_token
        self.timeout_s = timeout_s
        self.context = (
            None
            if parsed.scheme == "http"
            else ssl.create_default_context(cafile=None if ca_file is None else str(ca_file))
        )

    def __call__(self, evidence: SafetyEvidenceIngressV2) -> dict[str, Any]:
        body = evidence.model_dump_json().encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = UrlRequest(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urlopen(
                request,
                timeout=self.timeout_s,
                context=self.context,
            ) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Central rejected Safety Evidence v2 with HTTP {exc.code}: {detail}"
            ) from exc
        except (URLError, OSError) as exc:
            raise RuntimeError(f"Central Safety Evidence v2 transport failed: {exc}") from exc


def load_policy(path: str | Path) -> SafetyPolicyV1:
    return SafetyPolicyV1.model_validate_json(Path(path).read_text(encoding="utf-8"))


def create_safety_edge_app(
    policy: SafetyPolicyV1,
    submit: AssessmentSubmitter,
    *,
    submit_v2: EvidenceSubmitterV2 | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    measured_clock_quality: Callable[[], ClockQuality | None] = lambda: None,
) -> FastAPI:
    app = FastAPI(title="SANG Unity Safety Edge", version="0.1.0")

    @app.middleware("http")
    async def bound_snapshot_body(request: Request, call_next):
        if request.method == "POST" and request.url.path in {
            "/safety/v1/snapshots",
            "/safety/v2/evidence",
        }:
            raw_length = request.headers.get("content-length")
            try:
                content_length = None if raw_length is None else int(raw_length)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"error": "invalid content-length"},
                )
            if content_length is None:
                return JSONResponse(
                    status_code=411,
                    content={"error": "content-length is required"},
                )
            if content_length > MAX_SAFETY_SNAPSHOT_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"error": "safety snapshot exceeds 256 KiB"},
                )
        return await call_next(request)

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": "shadow",
            "command_capability": False,
            "policy_id": policy.policy_id,
            "policy_version": policy.version,
        }

    @app.post("/safety/v1/snapshots", status_code=202)
    async def evaluate_and_forward(snapshot: SafetySnapshotV1) -> dict[str, Any]:
        assessment = evaluate_snapshot(
            snapshot,
            policy,
            assessed_at=now(),
            evaluator_id="sang-unity-edge-shadow",
            evaluator_version="0.1.0",
            measured_clock_quality=measured_clock_quality(),
        )
        try:
            central = submit(assessment)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "assessment": assessment.model_dump(mode="json"),
            "central": central,
        }

    @app.post("/safety/v2/evidence", status_code=202)
    async def validate_and_forward_v2(evidence: SafetyEvidenceIngressV2) -> dict[str, Any]:
        if submit_v2 is None:
            raise HTTPException(status_code=503, detail="Safety Evidence v2 forwarding is disabled")
        try:
            central = submit_v2(evidence)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "snapshot_id": evidence.snapshot_id,
            "producer_id": evidence.producer_id,
            "central": central,
            "command_emitted": False,
        }

    return app


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the loopback Unity Safety Shadow Edge")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--control-plane-url", default="http://127.0.0.1:18091")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18092)
    parser.add_argument("--bearer-token-file", type=Path)
    parser.add_argument("--ca-file", type=Path)
    options = parser.parse_args(args)
    if options.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Unity Safety Edge must bind to loopback")
    if not 1 <= options.port <= 65535:
        parser.error("port must be between 1 and 65535")
    return options


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    token = (
        None
        if options.bearer_token_file is None
        else options.bearer_token_file.read_text(encoding="utf-8").strip()
    )
    app = create_safety_edge_app(
        load_policy(options.policy),
        CentralAssessmentSubmitter(
            options.control_plane_url,
            bearer_token=token,
            ca_file=options.ca_file,
        ),
        submit_v2=CentralEvidenceSubmitterV2(
            options.control_plane_url,
            bearer_token=token,
            ca_file=options.ca_file,
        ),
    )
    uvicorn.run(app, host=options.host, port=options.port, log_level="info")


if __name__ == "__main__":
    main()
