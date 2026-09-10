"""FastAPI application for the read-only Central Control Plane MVP."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import hmac
from pathlib import Path
import ssl
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    Request as UrlRequest,
    build_opener,
)

from fastapi import FastAPI, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from fleet_control_plane.capture_jobs import (
    ACTIVE_CAPTURE_STATES,
    CaptureJobConflict,
    CaptureJobCreate,
    CaptureJobInvalid,
    CaptureJobNotFound,
    CaptureJobStore,
)
from fleet_control_plane.aas_projection import (
    MAX_AAS_IMPORT_BYTES,
    AasProjectionCatalog,
    AasProjectionConflict,
    AasProjectionInvalid,
    AasProjectionNotFound,
)
from fleet_control_plane.commissioning_evidence import (
    MAX_G6B_ARTIFACT_BYTES,
    MAX_G6B_EVIDENCE_BYTES,
    G6bCaseCreateV1,
    G6bEvidenceCatalog,
    G6bEvidenceConflict,
    G6bEvidenceInvalid,
    G6bEvidenceNotFound,
    G6bEvidencePackageV1,
    G6bReviewRequestV1,
)
from fleet_control_plane.models import AgentHeartbeatV1
from fleet_control_plane.lerobot_derivation import (
    DatasetDerivationCatalog,
    DerivationNotFound,
)
from fleet_control_plane.recording_models import RecordingFinalize, RecordingManifest
from fleet_control_plane.recording_playback import (
    PlaybackBusy,
    PlaybackInvalid,
    PlaybackNotFound,
    PlaybackUnavailable,
    RecordingPlayback,
)
from fleet_control_plane.recording_quality import (
    QualityUnavailable,
    RecordingQualityService,
)
from fleet_control_plane.recording_storage import (
    FilesystemObjectStore,
    ObjectStoreError,
    RecordingCatalog,
    RecordingConflict,
    RecordingInvalid,
    RecordingNotFound,
)
from fleet_control_plane.safety_models import SafetyAssessmentV1, SafetyState
from fleet_control_plane.safety_shadow import (
    SafetyAssessmentConflict,
    SafetyAssessmentInvalid,
    SafetyShadowCatalog,
)
from fleet_control_plane.safety_v2 import (
    SafetyEvidenceIngressV2,
    SafetyV2Catalog,
    SafetyV2Conflict,
    SafetyV2Invalid,
)
from fleet_control_plane.control_authority import (
    ControlAuthority,
    ControlAuthorityError,
    ControlRoleBindingV1,
    ResumeApprovalV1,
    ResumeChallengeCreateV1,
    XrSessionUpdateV1,
)
from fleet_control_plane.control_signing import VerificationKeyRing
from fleet_control_plane.auth import (
    AuthManager,
    AuthenticationError,
    AuthorizationError,
    IdentityConflict,
    LoginRequest,
    Principal,
    PrincipalUpdate,
    ServiceIdentityCreate,
    TokenRevokeRequest,
    UserCreate,
)
from fleet_control_plane.operations_models import (
    AgentCredentialCreate,
    AgentRegistrationCreate,
    AgentRegistrationUpdate,
    AssetCreate,
    AssetReplace,
    AssetRuntimeKindUpdate,
    ConnectionProfileCreate,
    ConnectionProfileReplace,
    CredentialRevoke,
    DecommissionRequest,
    ProcessingNodeCreate,
    ProcessingNodeReplace,
    TaskDefinitionCreate,
    TaskRunCreate,
    TaskRunReplace,
    TaskRunTransition,
    WorkcellCreate,
    WorkcellReplace,
)
from fleet_control_plane.status import (
    FleetTTLs,
    isoformat_z,
    present_agent,
    summarize_agents,
    utc_now,
)
from fleet_control_plane.store import (
    ConfigurationConflict,
    ConfigurationInvalid,
    ConfigurationNotFound,
    FleetStore,
)
from fleet_control_plane.orchestration_api import orchestration_router
from fleet_control_plane.orchestration_models import ClockMeasurementV2
from fleet_control_plane.orchestration_store import OrchestrationStore
from fleet_control_plane.workflow_authoring import WorkflowAuthoringStore
from fleet_control_plane.workflow_authoring_api import workflow_authoring_router
from fleet_control_plane.unity_runtime_proxy import unity_runtime_router
from robot_agent.clock_sync import ClockSyncSampler, unsupported_sync


HEARTBEAT_SCHEMA_VERSION = 1
MAX_HEARTBEAT_BYTES = 256 * 1024
MAX_RECORDING_MANIFEST_BYTES = 256 * 1024
MAX_SAFETY_ASSESSMENT_BYTES = 256 * 1024
MAX_WORKFLOW_AUTHORING_BYTES = 512 * 1024
ALLOWED_ADMIN_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1", "testclient"})


def _source_rate_healthy(source: dict[str, Any]) -> bool:
    has_rate_contract = any(
        topic.get("minimum_rate_hz") is not None
        for topic in source.get("topics", [])
    )
    return not has_rate_contract or source.get("rate_ok") is True


class FleetAPIError(Exception):
    def __init__(self, status_code: int, error: str) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.error = error


class RejectPreviewRedirects(HTTPRedirectHandler):
    def _reject(self, request, response, code, message, headers):
        response.close()
        raise HTTPError(
            request.full_url,
            code,
            f"camera preview redirects are disabled: {message}",
            headers,
            None,
        )

    http_error_301 = _reject
    http_error_302 = _reject
    http_error_303 = _reject
    http_error_307 = _reject
    http_error_308 = _reject


def create_app(
    store: FleetStore,
    *,
    ui_dir: str | Path | None = None,
    operator_ui_dir: str | Path | None = None,
    ttls: FleetTTLs = FleetTTLs(),
    now: Callable[[], Any] = utc_now,
    websocket_interval_s: float = 1.0,
    auth_enabled: bool = True,
    secure_cookies: bool = False,
    preview_ssl_context: ssl.SSLContext | None = None,
    object_store: FilesystemObjectStore | None = None,
    quality_policy_root: str | Path | None = None,
    clock_sync: ClockSyncSampler | None = None,
    safety_clock: Callable[[], ClockMeasurementV2] | None = None,
    unity_build_keyring: VerificationKeyRing | None = None,
    workflow_adapter_profile_paths: list[str | Path] | None = None,
    workflow_skill_catalog_paths: list[str | Path] | None = None,
    workflow_runtime_base_url: str | None = None,
    aas_model_lock_path: str | Path | None = None,
    allowed_admin_hosts: frozenset[str] = ALLOWED_ADMIN_HOSTS,
) -> FastAPI:
    if websocket_interval_s <= 0.0:
        raise ValueError("websocket_interval_s must be positive")

    app = FastAPI(title="SANG Fleet Control Plane", version="0.1.0")
    app.state.fleet_revision = 0
    preview_slot = threading.BoundedSemaphore(value=1)
    preview_opener = build_opener(
        RejectPreviewRedirects(),
        HTTPSHandler(context=preview_ssl_context),
    )
    auth = AuthManager(store.path)
    capture_jobs = CaptureJobStore(store.path)
    recordings = RecordingCatalog(store.path)
    derivations = DatasetDerivationCatalog(store.path)
    safety = SafetyShadowCatalog(store.path)
    orchestration = OrchestrationStore(store.path)
    workflow_authoring = WorkflowAuthoringStore(
        store.path,
        adapter_profile_paths=workflow_adapter_profile_paths,
        skill_catalog_paths=workflow_skill_catalog_paths,
    )
    safety_v2 = SafetyV2Catalog(store.path, orchestration)
    control_authority = ControlAuthority(store.path, orchestration)
    aas_projections = (
        None
        if object_store is None
        else AasProjectionCatalog(
            store.path,
            object_store,
            (
                Path(__file__).resolve().parents[3]
                / "contracts/aas/metamodel.lock.yaml"
                if aas_model_lock_path is None
                else aas_model_lock_path
            ),
        )
    )
    g6b_evidence = (
        None
        if object_store is None
        else G6bEvidenceCatalog(store.path, object_store)
    )
    app.include_router(
        orchestration_router(
            orchestration,
            now=now,
            unity_build_keyring=unity_build_keyring,
        )
    )
    app.include_router(workflow_authoring_router(workflow_authoring, now=now))
    app.include_router(unity_runtime_router(workflow_runtime_base_url))
    playback = (
        None
        if object_store is None
        else RecordingPlayback(recordings, object_store)
    )
    quality = (
        None
        if object_store is None or quality_policy_root is None
        else RecordingQualityService(recordings, object_store, quality_policy_root)
    )
    if auth_enabled and auth.principal_count() == 0:
        raise RuntimeError(
            "authentication is enabled but no principals exist; "
            "run fleet_identity bootstrap-admin first"
        )

    def request_principal(request: Request) -> Principal:
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            return auth.authenticate_bearer(
                authorization.removeprefix("Bearer ").strip(),
                occurred_at=now(),
            )
        session = request.cookies.get("sang_session")
        if session is None:
            raise AuthenticationError("authentication is required")
        return auth.authenticate_session(session, occurred_at=now())

    def minimum_role(request: Request) -> str:
        if request.url.path.startswith("/api/v1/auth/") and request.url.path not in {
            "/api/v1/auth/me",
            "/api/v1/auth/logout",
        }:
            return "admin"
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return "viewer"
        if request.url.path.startswith("/api/v1/config/task-runs"):
            return "operator"
        if request.url.path.startswith("/api/v1/recording-sessions"):
            return "operator"
        if request.url.path in {
            "/api/v1/safety/assessments",
            "/api/v2/safety/evidence",
        }:
            return "operator"
        if request.url.path.startswith("/api/v1/control/xr-sessions"):
            return "operator"
        if "/resume-challenges" in request.url.path:
            return "operator"
        if request.url.path.endswith("/complete-after-capture"):
            return "operator"
        if "/capture-jobs" in request.url.path:
            return "operator"
        if request.url.path == "/api/v1/auth/logout":
            return "viewer"
        return "admin"

    async def authorize_websocket(websocket: WebSocket) -> bool:
        if not auth_enabled:
            return True
        try:
            authorization = websocket.headers.get("authorization", "")
            if authorization.startswith("Bearer "):
                principal = auth.authenticate_bearer(
                    authorization.removeprefix("Bearer ").strip(),
                    occurred_at=now(),
                )
            else:
                session = websocket.cookies.get("sang_session")
                if session is None:
                    raise AuthenticationError("authentication is required")
                principal = auth.authenticate_session(session, occurred_at=now())
            principal.require("viewer")
        except AuthenticationError:
            await websocket.close(code=4401, reason="authentication required")
            return False
        except AuthorizationError:
            await websocket.close(code=4403, reason="viewer role required")
            return False
        return True

    @app.middleware("http")
    async def reject_oversized_heartbeat(request: Request, call_next):
        if request.url.hostname not in allowed_admin_hosts:
            return JSONResponse(
                status_code=421,
                content={"error": "Admin API requires a loopback Host header"},
            )
        if request.method == "POST" and request.url.path == "/api/v1/agents/heartbeat":
            value = request.headers.get("content-length")
            try:
                content_length = None if value is None else int(value)
            except ValueError:
                return JSONResponse(status_code=400, content={"error": "invalid content-length"})
            if content_length is not None and content_length > MAX_HEARTBEAT_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"error": "heartbeat body exceeds 256 KiB"},
                )
        if request.method == "POST" and request.url.path == "/api/v1/recording-sessions":
            value = request.headers.get("content-length")
            try:
                content_length = None if value is None else int(value)
            except ValueError:
                return JSONResponse(status_code=400, content={"error": "invalid content-length"})
            if content_length is None:
                return JSONResponse(status_code=411, content={"error": "content-length is required"})
            if content_length > MAX_RECORDING_MANIFEST_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"error": "recording manifest exceeds 256 KiB"},
                )
        if request.method == "POST" and request.url.path in {
            "/api/v1/safety/assessments",
            "/api/v2/safety/evidence",
        }:
            value = request.headers.get("content-length")
            try:
                content_length = None if value is None else int(value)
            except ValueError:
                return JSONResponse(status_code=400, content={"error": "invalid content-length"})
            if content_length is None:
                return JSONResponse(status_code=411, content={"error": "content-length is required"})
            if content_length > MAX_SAFETY_ASSESSMENT_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"error": "safety assessment exceeds 256 KiB"},
                )
        if (
            request.method in {"POST", "PUT"}
            and request.url.path.startswith("/api/v1/workflow-authoring/")
        ):
            value = request.headers.get("content-length")
            try:
                content_length = None if value is None else int(value)
            except ValueError:
                return JSONResponse(status_code=400, content={"error": "invalid content-length"})
            if content_length is None:
                return JSONResponse(
                    status_code=411,
                    content={"error": "content-length is required"},
                )
            if content_length > MAX_WORKFLOW_AUTHORING_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"error": "Workflow authoring body exceeds 512 KiB"},
                )
            body = await request.body()
            if len(body) > MAX_WORKFLOW_AUTHORING_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"error": "Workflow authoring body exceeds 512 KiB"},
                )
            if content_length != len(body):
                return JSONResponse(
                    status_code=400,
                    content={"error": "content-length does not match the request body"},
                )
        if request.method == "POST" and request.url.path == "/api/v1/aas/projections":
            value = request.headers.get("content-length")
            try:
                content_length = None if value is None else int(value)
            except ValueError:
                return JSONResponse(status_code=400, content={"error": "invalid content-length"})
            if content_length is None:
                return JSONResponse(
                    status_code=411,
                    content={"error": "AAS import requires content-length"},
                )
            if not 0 < content_length <= MAX_AAS_IMPORT_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"error": "AAS import exceeds 64 MiB"},
                )
        if (
            request.method == "POST"
            and request.url.path.startswith("/api/v1/commissioning/g6b/")
        ):
            artifact_upload = request.url.path == "/api/v1/commissioning/g6b/artifacts"
            size_limit = (
                MAX_G6B_ARTIFACT_BYTES if artifact_upload else MAX_G6B_EVIDENCE_BYTES
            )
            size_label = "64 MiB" if artifact_upload else "2 MiB"
            value = request.headers.get("content-length")
            try:
                content_length = None if value is None else int(value)
            except ValueError:
                return JSONResponse(status_code=400, content={"error": "invalid content-length"})
            if content_length is None:
                return JSONResponse(
                    status_code=411,
                    content={"error": "G6b evidence requires content-length"},
                )
            if not 0 < content_length <= size_limit:
                return JSONResponse(
                    status_code=413,
                    content={"error": f"G6b upload exceeds {size_label}"},
                )
            body = await request.body()
            if len(body) > size_limit:
                return JSONResponse(
                    status_code=413,
                    content={"error": f"G6b upload exceeds {size_label}"},
                )
            if content_length != len(body):
                return JSONResponse(
                    status_code=400,
                    content={"error": "G6b content-length does not match the request body"},
                )
        public_api = request.url.path in {
            "/api/v1/healthz",
            "/api/v1/auth/login",
            "/api/v1/agents/heartbeat",
        }
        principal = None
        protected_api = request.url.path.startswith(("/api/v1/", "/api/v2/"))
        if auth_enabled and protected_api and not public_api:
            try:
                principal = request_principal(request)
                principal.require(minimum_role(request))
                if (
                    request.method not in {"GET", "HEAD", "OPTIONS"}
                    and principal.auth_kind == "session"
                ):
                    csrf_header = request.headers.get("x-csrf-token")
                    csrf_cookie = request.cookies.get("sang_csrf")
                    if csrf_header is None or not hmac.compare_digest(
                        csrf_header,
                        csrf_cookie or "",
                    ):
                        raise AuthorizationError("CSRF double-submit token mismatch")
                    auth.validate_csrf(principal.session_id or "", csrf_header)
            except AuthenticationError as exc:
                auth.record_request(
                    principal_id=None,
                    method=request.method,
                    path=request.url.path,
                    status_code=401,
                    occurred_at=now(),
                )
                return JSONResponse(status_code=401, content={"error": str(exc)})
            except AuthorizationError as exc:
                auth.record_request(
                    principal_id=None if principal is None else principal.principal_id,
                    method=request.method,
                    path=request.url.path,
                    status_code=403,
                    occurred_at=now(),
                )
                return JSONResponse(status_code=403, content={"error": str(exc)})
        request.state.principal = principal
        response = await call_next(request)
        if (
            auth_enabled
            and principal is not None
            and request.method not in {"GET", "HEAD", "OPTIONS"}
        ):
            auth.record_request(
                principal_id=principal.principal_id,
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                occurred_at=now(),
            )
        return response

    @app.middleware("http")
    async def add_browser_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; "
            "img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self' ws: wss:"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
        elif (
            request.url.path in {"/", "/operator", "/operator/"}
            or request.url.path.endswith(".html")
            or request.url.path.startswith("/antd-assets/")
        ):
            # Fixed-name UI assets must revalidate after deployment, including 304s.
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(FleetAPIError)
    async def fleet_error_handler(request: Request, exc: FleetAPIError) -> JSONResponse:
        del request
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error})

    @app.exception_handler(ConfigurationNotFound)
    async def configuration_not_found_handler(
        request: Request,
        exc: ConfigurationNotFound,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.exception_handler(ConfigurationConflict)
    async def configuration_conflict_handler(
        request: Request,
        exc: ConfigurationConflict,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(ConfigurationInvalid)
    async def configuration_invalid_handler(
        request: Request,
        exc: ConfigurationInvalid,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=422, content={"error": str(exc)})

    @app.exception_handler(AasProjectionNotFound)
    async def aas_projection_not_found_handler(
        request: Request,
        exc: AasProjectionNotFound,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.exception_handler(AasProjectionConflict)
    async def aas_projection_conflict_handler(
        request: Request,
        exc: AasProjectionConflict,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(AasProjectionInvalid)
    async def aas_projection_invalid_handler(
        request: Request,
        exc: AasProjectionInvalid,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=422, content={"error": str(exc)})

    @app.exception_handler(G6bEvidenceNotFound)
    async def g6b_evidence_not_found_handler(
        request: Request,
        exc: G6bEvidenceNotFound,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.exception_handler(G6bEvidenceConflict)
    async def g6b_evidence_conflict_handler(
        request: Request,
        exc: G6bEvidenceConflict,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(G6bEvidenceInvalid)
    async def g6b_evidence_invalid_handler(
        request: Request,
        exc: G6bEvidenceInvalid,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=422, content={"error": str(exc)})

    @app.exception_handler(IdentityConflict)
    async def identity_conflict_handler(
        request: Request,
        exc: IdentityConflict,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(AuthenticationError)
    async def identity_not_found_handler(
        request: Request,
        exc: AuthenticationError,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.exception_handler(RecordingNotFound)
    async def recording_not_found_handler(
        request: Request,
        exc: RecordingNotFound,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.exception_handler(CaptureJobNotFound)
    async def capture_job_not_found_handler(
        request: Request,
        exc: CaptureJobNotFound,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.exception_handler(CaptureJobConflict)
    async def capture_job_conflict_handler(
        request: Request,
        exc: CaptureJobConflict,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(CaptureJobInvalid)
    async def capture_job_invalid_handler(
        request: Request,
        exc: CaptureJobInvalid,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=422, content={"error": str(exc)})

    @app.exception_handler(RecordingConflict)
    async def recording_conflict_handler(
        request: Request,
        exc: RecordingConflict,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(RecordingInvalid)
    async def recording_invalid_handler(
        request: Request,
        exc: RecordingInvalid,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=422, content={"error": str(exc)})

    @app.exception_handler(ObjectStoreError)
    async def object_store_error_handler(
        request: Request,
        exc: ObjectStoreError,
    ) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=500,
            content={"error": "Object Store integrity check failed"},
        )

    @app.exception_handler(SafetyAssessmentConflict)
    async def safety_assessment_conflict_handler(
        request: Request,
        exc: SafetyAssessmentConflict,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(SafetyAssessmentInvalid)
    async def safety_assessment_invalid_handler(
        request: Request,
        exc: SafetyAssessmentInvalid,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=422, content={"error": str(exc)})

    @app.exception_handler(SafetyV2Conflict)
    async def safety_v2_conflict_handler(
        request: Request,
        exc: SafetyV2Conflict,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(SafetyV2Invalid)
    async def safety_v2_invalid_handler(
        request: Request,
        exc: SafetyV2Invalid,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=422, content={"error": str(exc)})

    @app.exception_handler(ControlAuthorityError)
    async def control_authority_error_handler(
        request: Request,
        exc: ControlAuthorityError,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=422, content={"error": str(exc)})

    def agents_at(current) -> list[dict[str, Any]]:
        agents = [
            present_agent(snapshot, now=current, ttls=ttls)
            for snapshot in store.list_snapshots()
        ]
        transitions = store.reconcile_agent_statuses(
            agents,
            occurred_at=current,
        )
        if transitions:
            app.state.fleet_revision += transitions
        event_counts = store.event_counts_by_agent()
        for agent in agents:
            agent["event_counts"] = event_counts.get(agent["agent_id"], {})
        return agents

    def agents_payload() -> list[dict[str, Any]]:
        return relay_urls(agents_at(now()))

    def shadow_agents_payload() -> list[dict[str, Any]]:
        current = now()
        return [
            present_agent(snapshot, now=current, ttls=ttls)
            for snapshot in store.list_snapshots()
        ]

    def fleet_payload() -> dict[str, Any]:
        current = now()
        agents = relay_urls(agents_at(current))
        return {
            "type": "fleet",
            "agents": agents,
            "summary": summarize_agents(agents, now=current),
            "events": store.list_events(limit=100),
        }

    @app.get("/api/v1/time-status")
    async def time_status() -> dict[str, Any]:
        central = (
            unsupported_sync()
            if clock_sync is None
            else await asyncio.to_thread(clock_sync.sample)
        )
        agents = [
            {
                "agent_id": agent["agent_id"],
                "status": agent["status"],
                "sync": agent.get("sync", unsupported_sync()),
            }
            for agent in agents_payload()
        ]
        return {"central": central, "agents": agents}

    def configuration_discovery() -> dict[str, Any]:
        assets = store.list_assets()
        claimed = {
            (
                asset["binding"]["agent_id"],
                asset["binding"]["robot_id"],
                asset["binding"]["source_key"],
            )
            for asset in assets
            if asset["binding"] is not None
            and asset.get("lifecycle_state", "active") == "active"
        }
        node_by_agent = {
            agent_id: node["node_id"]
            for node in store.list_processing_nodes()
            if node.get("lifecycle_state", "active") == "active"
            for agent_id in node["agent_ids"]
        }
        robots = []
        cameras = []
        for agent in agents_payload():
            for robot in agent["robots"]:
                robot_binding = (agent["agent_id"], robot["robot_id"], None)
                if robot_binding not in claimed:
                    robots.append(
                        {
                            "agent_id": agent["agent_id"],
                            "agent_name": agent["agent_name"],
                            "node_id": node_by_agent.get(agent["agent_id"]),
                            "robot_id": robot["robot_id"],
                            "model": robot["model"],
                            "runtime_kind": robot.get("runtime_kind"),
                            "status": agent["status"],
                        }
                    )
                for source in robot["sources"]:
                    if source["kind"] != "camera":
                        continue
                    camera_binding = (
                        agent["agent_id"],
                        robot["robot_id"],
                        source["key"],
                    )
                    if camera_binding in claimed:
                        continue
                    cameras.append(
                        {
                            "agent_id": agent["agent_id"],
                            "agent_name": agent["agent_name"],
                            "node_id": node_by_agent.get(agent["agent_id"]),
                            "robot_id": robot["robot_id"],
                            "runtime_kind": robot.get("runtime_kind"),
                            "source_key": source["key"],
                            "preview_url": source.get("preview_url"),
                            "fresh": source["fresh"],
                            "rate_ok": source.get("rate_ok"),
                            "status": agent["status"],
                        }
                    )
        return {"robots": robots, "cameras": cameras}

    def task_run_or_404(run_id: str) -> dict[str, Any]:
        task_run = store.get_task_run(run_id)
        if task_run is None:
            raise ConfigurationNotFound(f"unknown task run: {run_id}")
        return task_run

    def camera_asset_or_404(asset_id: str) -> dict[str, Any]:
        asset = next(
            (item for item in store.list_assets() if item["asset_id"] == asset_id),
            None,
        )
        if asset is None:
            raise ConfigurationNotFound(f"unknown asset: {asset_id}")
        if asset["asset_type"] != "camera" or asset["binding"] is None:
            raise ConfigurationInvalid(f"asset {asset_id} is not a bound camera")
        if asset.get("lifecycle_state", "active") != "active":
            raise ConfigurationInvalid(f"camera asset {asset_id} is decommissioned")
        return asset

    def camera_upstream_url(asset_id: str) -> str:
        asset = camera_asset_or_404(asset_id)
        binding = asset["binding"]
        snapshot = store.get_snapshot(binding["agent_id"])
        if snapshot is None:
            raise FleetAPIError(503, "camera Agent has no heartbeat snapshot")
        robot = next(
            (
                item
                for item in snapshot.get("robots", [])
                if item["robot_id"] == binding["robot_id"]
            ),
            None,
        )
        if robot is None:
            raise FleetAPIError(503, "camera robot is absent from the latest heartbeat")
        source = next(
            (
                item
                for item in robot.get("sources", [])
                if item["key"] == binding["source_key"]
            ),
            None,
        )
        if source is None or source.get("kind") != "camera":
            raise FleetAPIError(503, "camera source is absent from the latest heartbeat")
        upstream = source.get("preview_url")
        if not upstream:
            raise FleetAPIError(503, "camera Agent does not advertise a preview endpoint")
        parsed = urlsplit(upstream)
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.hostname is None
            or parsed.port is None
        ):
            raise FleetAPIError(502, "camera preview endpoint is not canonical")
        expected_path = (
            f"/robots/{quote(binding['robot_id'], safe='')}"
            f"/cameras/{quote(binding['source_key'], safe='')}.mjpg"
        )
        if parsed.path != expected_path:
            raise FleetAPIError(502, "camera preview endpoint path does not match its binding")
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme == "http" and not loopback:
            raise FleetAPIError(502, "remote camera preview requires HTTPS mutual TLS")
        if parsed.scheme == "https" and preview_ssl_context is None:
            raise FleetAPIError(503, "Central preview TLS is not configured")
        if parsed.scheme not in {"http", "https"}:
            raise FleetAPIError(502, "unsupported camera preview transport")
        if not loopback:
            profile = store.get_agent_connection_profile(
                binding["agent_id"],
                "https_agent",
            )
            observation = None
            if profile is not None and profile["enabled"]:
                observation = next(
                    (
                        item
                        for item in store.list_connection_observations()
                        if item["profile_id"] == profile["profile_id"]
                        and item["last_success_at"] is not None
                    ),
                    None,
                )
            peer_address = None if observation is None else observation["peer_address"]
            peer_host = None if not peer_address else peer_address.rsplit(":", 1)[0]
            if peer_host != parsed.hostname:
                raise FleetAPIError(
                    502,
                    "camera preview host is not the Agent's allowlisted mTLS peer",
                )
        return upstream

    def relay_urls(
        agents: list[dict[str, Any]],
        *,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        camera_assets = {
            (
                asset["binding"]["agent_id"],
                asset["binding"]["robot_id"],
                asset["binding"]["source_key"],
            ): asset["asset_id"]
            for asset in store.list_assets()
            if asset["asset_type"] == "camera"
            and asset["binding"] is not None
            and asset.get("lifecycle_state", "active") == "active"
        }
        for agent in agents:
            for robot in agent["robots"]:
                for source in robot["sources"]:
                    if source["kind"] != "camera":
                        continue
                    upstream_available = bool(source.get("preview_url"))
                    asset_id = camera_assets.get(
                        (agent["agent_id"], robot["robot_id"], source["key"])
                    )
                    source["upstream_preview_available"] = upstream_available
                    if not upstream_available or asset_id is None:
                        source["preview_url"] = None
                    elif run_id is None:
                        source["preview_url"] = (
                            f"/api/v1/cameras/{quote(asset_id, safe='')}/preview.mjpg"
                        )
                    else:
                        source["preview_url"] = (
                            f"/api/v1/task-runs/{quote(run_id, safe='')}/cameras/"
                            f"{quote(asset_id, safe='')}/preview.mjpg"
                        )
        return agents

    def filter_agent_for_run(
        agent: dict[str, Any],
        selected: dict[str, set[str] | bool],
    ) -> dict[str, Any] | None:
        robots = []
        for robot in agent["robots"]:
            robot_selection = selected.get(robot["robot_id"])
            if robot_selection is None:
                continue
            robot_view = {**robot}
            if robot_selection is not True:
                robot_view["sources"] = [
                    source
                    for source in robot["sources"]
                    if source["key"] in robot_selection
                ]
                robot_view["sources_fresh"] = all(
                    source["fresh"]
                    for source in robot_view["sources"]
                    if source["required"]
                )
                robot_view["rates_ok"] = all(
                    _source_rate_healthy(source)
                    for source in robot_view["sources"]
                    if source["required"]
                )
            robots.append(robot_view)
        if not robots:
            return None
        run_view = {**agent, "robots": robots, "event_counts": {}}
        run_view.pop("recordings", None)
        return run_view

    def run_asset_observations(
        task_run: dict[str, Any],
        agents: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        agents_by_id = {agent["agent_id"]: agent for agent in agents}
        observations = []
        for assignment in task_run["assignments"]:
            asset = assignment["asset"]
            binding = asset["binding"]
            state = "UNINSTRUMENTED"
            detail = "No live Agent binding"
            if binding is not None:
                agent = agents_by_id.get(binding["agent_id"])
                if agent is None or agent["status"] == "offline":
                    state = "OFFLINE"
                    detail = "Agent heartbeat is offline"
                else:
                    robot = next(
                        (
                            item
                            for item in agent["robots"]
                            if item["robot_id"] == binding["robot_id"]
                        ),
                        None,
                    )
                    if robot is None:
                        state = "UNAVAILABLE"
                        detail = "Robot is absent from the latest heartbeat"
                    elif (
                        asset.get("runtime_kind") is not None
                        and robot.get("runtime_kind") is not None
                        and asset["runtime_kind"] != robot["runtime_kind"]
                    ):
                        state = "IDENTITY_MISMATCH"
                        detail = (
                            f"Asset is {asset['runtime_kind']}; observed runtime is "
                            f"{robot['runtime_kind']}"
                        )
                    elif binding["source_key"] is None:
                        state = (
                            "HEALTHY"
                            if agent["status"] == "online"
                            and robot["sources_fresh"]
                            and robot["rates_ok"]
                            and not robot["fault"]
                            else "DEGRADED"
                        )
                        detail = robot["fault"] or robot["phase"]
                    else:
                        source = next(
                            (
                                item
                                for item in robot["sources"]
                                if item["key"] == binding["source_key"]
                            ),
                            None,
                        )
                        if source is None:
                            state = "UNAVAILABLE"
                            detail = "Camera source is absent from the latest heartbeat"
                        else:
                            state = (
                                "HEALTHY"
                                if agent["status"] == "online"
                                and source["fresh"]
                                and _source_rate_healthy(source)
                                else "DEGRADED"
                            )
                            detail = source["key"]
            observations.append(
                {
                    "role": assignment["role"],
                    "access_mode": assignment["access_mode"],
                    "reserved": assignment["reserved"],
                    "asset": asset,
                    "state": state,
                    "detail": detail,
                }
            )
        return observations

    def task_run_fleet_payload(run_id: str) -> dict[str, Any]:
        task_run = task_run_or_404(run_id)
        selection: dict[str, dict[str, set[str] | bool]] = {}
        assigned_agent_ids = set()
        for assignment in task_run["assignments"]:
            binding = assignment["asset"]["binding"]
            if binding is None:
                continue
            agent_id = binding["agent_id"]
            robot_id = binding["robot_id"]
            assigned_agent_ids.add(agent_id)
            robot_selection = selection.setdefault(agent_id, {})
            if binding["source_key"] is None:
                robot_selection[robot_id] = True
            elif robot_selection.get(robot_id) is not True:
                sources = robot_selection.setdefault(robot_id, set())
                assert isinstance(sources, set)
                sources.add(binding["source_key"])

        current = now()
        all_agents = agents_at(current)
        filtered_agents = []
        for agent in all_agents:
            selected = selection.get(agent["agent_id"])
            if selected is None:
                continue
            filtered = filter_agent_for_run(agent, selected)
            if filtered is not None:
                filtered_agents.append(filtered)
        def event_is_in_scope(event: dict[str, Any]) -> bool:
            selected = selection.get(event["agent_id"])
            if selected is None:
                return False
            if event["robot_id"] is None:
                return True
            robot_selection = selected.get(event["robot_id"])
            if robot_selection is True:
                return True
            return (
                isinstance(robot_selection, set)
                and event["source_key"] in robot_selection
            )

        scoped_events = [
            event
            for event in store.list_events(limit=500)
            if event_is_in_scope(event)
        ][:100]
        scoped_event_counts: dict[str, dict[str, int]] = {}
        for event in scoped_events:
            counts = scoped_event_counts.setdefault(event["agent_id"], {})
            counts[event["event_type"]] = counts.get(event["event_type"], 0) + 1
        for agent in filtered_agents:
            agent["event_counts"] = scoped_event_counts.get(agent["agent_id"], {})
        filtered_agents = relay_urls(filtered_agents, run_id=run_id)
        summary = summarize_agents(filtered_agents, now=current)
        summary.pop("recordings", None)
        return {
            "type": "task_run_fleet",
            "generated_at": isoformat_z(current),
            "task_run": task_run,
            "assets": run_asset_observations(task_run, all_agents),
            "agents": filtered_agents,
            "summary": summary,
            "events": scoped_events,
            "capture_workers": capture_jobs.list_workers_for_run(
                run_id,
                now=current,
            ),
            "capture_jobs": capture_jobs.list_jobs(run_id=run_id),
            "recording_sessions": recordings.list_sessions(run_id=run_id),
        }

    @app.get("/api/v1/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "scope": "robot-camera-operations-v1"}

    @app.post("/api/v1/auth/login")
    async def login(body: LoginRequest) -> Response:
        try:
            row = auth.authenticate_password(
                body.principal_id,
                body.password,
                occurred_at=now(),
            )
        except AuthenticationError as exc:
            return JSONResponse(status_code=401, content={"error": str(exc)})
        principal, session, csrf, expires_at = auth.create_session(
            row,
            occurred_at=now(),
        )
        response = JSONResponse(
            content={
                "principal": principal.public(),
                "expires_at": isoformat_z(expires_at),
            }
        )
        max_age = max(0, int((expires_at - now()).total_seconds()))
        response.set_cookie(
            "sang_session",
            session,
            max_age=max_age,
            httponly=True,
            secure=secure_cookies,
            samesite="strict",
            path="/",
        )
        response.set_cookie(
            "sang_csrf",
            csrf,
            max_age=max_age,
            httponly=False,
            secure=secure_cookies,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/api/v1/auth/me")
    async def current_identity(request: Request) -> dict[str, Any]:
        principal = request.state.principal
        return {"principal": principal.public()}

    @app.post("/api/v1/auth/logout")
    async def logout(request: Request) -> Response:
        principal = request.state.principal
        if principal.auth_kind != "session" or principal.session_id is None:
            raise FleetAPIError(422, "logout requires a browser session")
        auth.revoke_session(
            principal.session_id,
            principal_id=principal.principal_id,
            occurred_at=now(),
        )
        response = JSONResponse(content={"status": "logged_out"})
        response.delete_cookie("sang_session", path="/")
        response.delete_cookie("sang_csrf", path="/")
        return response

    @app.get("/api/v1/auth/principals")
    async def list_principals() -> list[dict[str, object]]:
        return auth.list_principals()

    @app.post("/api/v1/auth/users", status_code=201)
    async def create_user(body: UserCreate, request: Request) -> dict[str, object]:
        principal = request.state.principal
        return auth.create_user(
            body,
            actor_id=principal.principal_id,
            occurred_at=now(),
        )

    @app.post("/api/v1/auth/service-identities", status_code=201)
    async def create_service_identity(
        body: ServiceIdentityCreate,
        request: Request,
    ) -> dict[str, object]:
        principal = request.state.principal
        return auth.create_service_identity(
            body,
            actor_id=principal.principal_id,
            occurred_at=now(),
        )

    @app.put("/api/v1/auth/principals/{principal_id}")
    async def update_principal(
        principal_id: str,
        body: PrincipalUpdate,
        request: Request,
    ) -> dict[str, object]:
        principal = request.state.principal
        return auth.update_principal(
            principal_id,
            body,
            actor_id=principal.principal_id,
            occurred_at=now(),
        )

    @app.post("/api/v1/auth/service-tokens/{token_id}/revoke")
    async def revoke_service_token(
        token_id: str,
        body: TokenRevokeRequest,
        request: Request,
    ) -> dict[str, object]:
        principal = request.state.principal
        return auth.revoke_service_token(
            token_id,
            body,
            actor_id=principal.principal_id,
            occurred_at=now(),
        )

    @app.get("/api/v1/auth/audit")
    async def list_security_audit(
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, object]]:
        return auth.list_security_audit(limit=limit)

    @app.post("/api/v1/agents/heartbeat", status_code=202)
    async def receive_heartbeat(
        body: AgentHeartbeatV1,
        request: Request,
    ) -> dict[str, Any]:
        client_host = None if request.client is None else request.client.host
        if client_host not in LOOPBACK_CLIENTS:
            raise FleetAPIError(
                403,
                "legacy heartbeat is loopback-only; remote Agents must use mTLS ingress",
            )
        if body.schema_version != HEARTBEAT_SCHEMA_VERSION:
            raise FleetAPIError(
                400,
                f"unsupported schema_version: {body.schema_version}; "
                f"expected {HEARTBEAT_SCHEMA_VERSION}",
            )
        if store.agent_is_registered(body.agent_id):
            raise FleetAPIError(
                403,
                "registered remote Agents must use the dedicated mTLS ingress",
            )
        received_at = now()
        agents_at(received_at)
        transitions = store.upsert(body, received_at=received_at)
        app.state.fleet_revision += 1 + transitions
        return {
            "received_at": isoformat_z(received_at),
            "next_interval_s": 5,
        }

    @app.get("/api/v1/agents")
    async def list_agents() -> list[dict[str, Any]]:
        return agents_payload()

    @app.get("/api/v1/shadow/agents")
    async def list_shadow_agents() -> list[dict[str, Any]]:
        return shadow_agents_payload()

    @app.get("/api/v1/summary")
    async def fleet_summary() -> dict[str, Any]:
        current = now()
        agents = agents_at(current)
        return summarize_agents(agents, now=current)

    @app.get("/api/v1/events")
    async def list_health_events(
        limit: int = Query(default=100, ge=1, le=500),
        agent_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        ),
    ) -> list[dict[str, Any]]:
        agents_at(now())
        return store.list_events(limit=limit, agent_id=agent_id)

    @app.get("/api/v1/configuration")
    async def get_configuration() -> dict[str, Any]:
        return store.get_configuration()

    @app.get("/api/v1/config/discovery")
    async def get_configuration_discovery() -> dict[str, Any]:
        return configuration_discovery()

    @app.get("/api/v1/config/security")
    async def get_security_configuration() -> dict[str, Any]:
        return {
            "schema_versions": store.schema_versions(),
            "agent_registrations": store.list_agent_registrations(),
            "agent_credentials": store.list_agent_credentials(),
            "connection_profiles": store.list_connection_profiles(),
            "connection_observations": store.list_connection_observations(),
        }

    @app.post("/api/v1/config/agent-registrations", status_code=201)
    async def create_agent_registration(
        body: AgentRegistrationCreate,
    ) -> dict[str, Any]:
        result = store.create_agent_registration(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.put("/api/v1/config/agent-registrations/{agent_id}")
    async def update_agent_registration(
        agent_id: str,
        body: AgentRegistrationUpdate,
    ) -> dict[str, Any]:
        result = store.update_agent_registration(agent_id, body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/agent-credentials", status_code=201)
    async def create_agent_credential(
        body: AgentCredentialCreate,
    ) -> dict[str, Any]:
        result = store.create_agent_credential(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/agent-credentials/{credential_id}/revoke")
    async def revoke_agent_credential(
        credential_id: str,
        body: CredentialRevoke,
    ) -> dict[str, Any]:
        result = store.revoke_agent_credential(
            credential_id,
            body,
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/connection-profiles", status_code=201)
    async def create_connection_profile(
        body: ConnectionProfileCreate,
    ) -> dict[str, Any]:
        result = store.create_connection_profile(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.put("/api/v1/config/connection-profiles/{profile_id}")
    async def replace_connection_profile(
        profile_id: str,
        body: ConnectionProfileReplace,
    ) -> dict[str, Any]:
        result = store.replace_connection_profile(profile_id, body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/processing-nodes", status_code=201)
    async def create_processing_node(body: ProcessingNodeCreate) -> dict[str, Any]:
        result = store.create_processing_node(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.put("/api/v1/config/processing-nodes/{node_id}")
    async def replace_processing_node(
        node_id: str,
        body: ProcessingNodeReplace,
    ) -> dict[str, Any]:
        result = store.replace_processing_node(node_id, body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/processing-nodes/{node_id}/decommission")
    async def decommission_processing_node(
        node_id: str,
        body: DecommissionRequest,
    ) -> dict[str, Any]:
        result = store.decommission_processing_node(
            node_id,
            body,
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/assets", status_code=201)
    async def create_asset(body: AssetCreate) -> dict[str, Any]:
        result = store.create_asset(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.put("/api/v1/config/assets/{asset_id}")
    async def replace_asset(
        asset_id: str,
        body: AssetReplace,
    ) -> dict[str, Any]:
        result = store.replace_asset(asset_id, body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/assets/{asset_id}/decommission")
    async def decommission_asset(
        asset_id: str,
        body: DecommissionRequest,
    ) -> dict[str, Any]:
        result = store.decommission_asset(asset_id, body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.patch("/api/v1/config/assets/{asset_id}/runtime-kind")
    async def set_asset_runtime_kind(
        asset_id: str,
        body: AssetRuntimeKindUpdate,
    ) -> dict[str, Any]:
        result = store.set_asset_runtime_kind(
            asset_id,
            body,
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/workcells", status_code=201)
    async def create_workcell(body: WorkcellCreate) -> dict[str, Any]:
        result = store.create_workcell(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.put("/api/v1/config/workcells/{workcell_id}")
    async def replace_workcell(
        workcell_id: str,
        body: WorkcellReplace,
    ) -> dict[str, Any]:
        result = store.replace_workcell(workcell_id, body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/workcells/{workcell_id}/decommission")
    async def decommission_workcell(
        workcell_id: str,
        body: DecommissionRequest,
    ) -> dict[str, Any]:
        result = store.decommission_workcell(
            workcell_id,
            body,
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/task-definitions", status_code=201)
    async def create_task_definition(body: TaskDefinitionCreate) -> dict[str, Any]:
        result = store.create_task_definition(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/task-runs", status_code=201)
    async def create_task_run(body: TaskRunCreate) -> dict[str, Any]:
        result = store.create_task_run(body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.put("/api/v1/config/task-runs/{run_id}")
    async def replace_task_run(
        run_id: str,
        body: TaskRunReplace,
    ) -> dict[str, Any]:
        result = store.replace_task_run(run_id, body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/config/task-runs/{run_id}/transition")
    async def transition_task_run(
        run_id: str,
        body: TaskRunTransition,
    ) -> dict[str, Any]:
        result = store.transition_task_run(run_id, body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.get("/api/v1/task-runs/{run_id}")
    async def get_task_run(run_id: str) -> dict[str, Any]:
        return task_run_or_404(run_id)

    @app.get("/api/v1/task-runs/{run_id}/fleet")
    async def get_task_run_fleet(run_id: str) -> dict[str, Any]:
        return task_run_fleet_payload(run_id)

    @app.post("/api/v1/task-runs/{run_id}/complete-after-capture")
    async def complete_task_run_after_capture(
        run_id: str,
        body: TaskRunTransition,
    ) -> dict[str, Any]:
        task_run = task_run_or_404(run_id)
        if body.state != "COMPLETE":
            raise FleetAPIError(422, "capture-gated completion only accepts COMPLETE")
        if task_run["state"] != "ACTIVE":
            raise FleetAPIError(409, "Task Run must be ACTIVE")

        jobs = capture_jobs.list_jobs(run_id=run_id)
        if any(job["state"] in ACTIVE_CAPTURE_STATES for job in jobs):
            raise FleetAPIError(409, "Task Run has an active Capture Job")
        if not jobs or jobs[0]["state"] != "COMPLETE":
            raise FleetAPIError(409, "latest Capture Job is not complete")

        latest_job = jobs[0]
        session = next(
            (
                item
                for item in recordings.list_sessions(run_id=run_id)
                if item["session_id"] == latest_job["session_id"]
            ),
            None,
        )
        if session is None or session["state"] != "COMPLETE":
            raise FleetAPIError(409, "latest Capture Job is not stored completely")
        quality_state = (session.get("quality") or {}).get("state")
        if quality_state != "PASS":
            raise FleetAPIError(
                409,
                f"latest Capture Job quality is {quality_state or 'PENDING'}, not PASS",
            )

        result = store.transition_task_run(run_id, body, occurred_at=now())
        app.state.fleet_revision += 1
        return result

    @app.get("/api/v1/task-runs/{run_id}/capture-jobs")
    async def list_task_run_capture_jobs(run_id: str) -> list[dict[str, Any]]:
        task_run_or_404(run_id)
        return capture_jobs.list_jobs(run_id=run_id)

    @app.post("/api/v1/task-runs/{run_id}/capture-jobs", status_code=201)
    async def create_task_run_capture_job(
        run_id: str,
        body: CaptureJobCreate,
        request: Request,
    ) -> dict[str, Any]:
        principal = request.state.principal
        worker = capture_jobs.get_worker(body.worker_id)
        require_recording_agent_scope(request, worker["agent_id"])
        result = capture_jobs.create_job(
            run_id,
            body,
            created_by=(
                "local-operator"
                if principal is None
                else principal.principal_id
            ),
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    @app.get("/api/v1/capture-jobs/{job_id}")
    async def get_capture_job(job_id: str) -> dict[str, Any]:
        return capture_jobs.get_job(job_id)

    @app.post("/api/v1/capture-jobs/{job_id}/stop")
    async def stop_capture_job(
        job_id: str,
        request: Request,
    ) -> dict[str, Any]:
        principal = request.state.principal
        current = capture_jobs.get_job(job_id)
        require_recording_agent_scope(request, current["agent_id"])
        result = capture_jobs.request_stop(
            job_id,
            requested_by=(
                "local-operator"
                if principal is None
                else principal.principal_id
            ),
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    def require_object_store() -> FilesystemObjectStore:
        if object_store is None:
            raise FleetAPIError(503, "Central Object Store is not configured")
        return object_store

    def require_aas_projections() -> AasProjectionCatalog:
        if aas_projections is None:
            raise FleetAPIError(
                503,
                "AAS projection requires the Central Object Store",
            )
        return aas_projections

    def require_g6b_evidence() -> G6bEvidenceCatalog:
        if g6b_evidence is None:
            raise FleetAPIError(
                503,
                "G6b evidence requires the Central Object Store",
            )
        return g6b_evidence

    def require_recording_agent_scope(request: Request, agent_id: str) -> None:
        principal = request.state.principal
        if (
            principal is not None
            and principal.principal_kind == "service"
            and principal.role != "admin"
            and agent_id not in auth.service_agent_ids(principal.principal_id)
        ):
            raise FleetAPIError(
                403,
                f"service identity is not authorized for recording Agent {agent_id}",
            )

    def require_safety_agent_scope(request: Request, agent_id: str) -> None:
        principal = request.state.principal
        if (
            principal is not None
            and principal.principal_kind == "service"
            and principal.role != "admin"
            and agent_id not in auth.service_agent_ids(principal.principal_id)
        ):
            raise FleetAPIError(
                403,
                f"service identity is not authorized for safety Agent {agent_id}",
            )

    def require_safety_v2_producer(request: Request, producer_id: str) -> None:
        if not auth_enabled:
            return
        principal = request.state.principal
        if principal is None or principal.principal_kind != "service":
            raise FleetAPIError(
                403,
                "Safety Evidence v2 ingress requires a service identity",
            )
        if principal.principal_id != producer_id:
            raise FleetAPIError(
                403,
                "service identity does not match the bound safety producer",
            )

    def trusted_safety_clock() -> ClockMeasurementV2:
        if safety_clock is not None:
            return safety_clock()
        sampled_at = now()
        return ClockMeasurementV2(
            monitor_id="central-clock-monitor",
            reference_id="unconfigured-reference",
            sampled_at=sampled_at,
            offset_ms=0.0,
            uncertainty_ms=0.0,
            state="unknown",
        )

    @app.post("/api/v1/safety/assessments", status_code=202)
    async def accept_safety_assessment(
        body: SafetyAssessmentV1,
        request: Request,
    ) -> dict[str, Any]:
        require_safety_agent_scope(request, body.agent_id)
        result = safety.accept(body, received_at=now())
        if result["transition_recorded"]:
            app.state.fleet_revision += 1
        return result

    @app.post("/api/v2/safety/evidence", status_code=202)
    async def accept_safety_evidence_v2(
        body: SafetyEvidenceIngressV2,
        request: Request,
    ) -> dict[str, Any]:
        require_safety_v2_producer(request, body.producer_id)
        result = safety_v2.accept(
            body,
            received_at=now(),
            measured_clock=trusted_safety_clock(),
        )
        if result.get("run_state") == "HELD":
            app.state.fleet_revision += 1
        return result

    @app.get("/api/v2/safety/assessments")
    async def list_safety_assessments_v2(
        run_id: str = Query(
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        ),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return safety_v2.list_assessments(run_id, limit=limit)

    def authenticated_control_principal(
        request: Request,
        declared_principal_id: str | None,
    ) -> str:
        principal = request.state.principal
        if principal is None:
            if auth_enabled or declared_principal_id is None:
                raise FleetAPIError(403, "an authenticated control principal is required")
            return declared_principal_id
        if (
            declared_principal_id is not None
            and declared_principal_id != principal.principal_id
        ):
            raise FleetAPIError(403, "declared control principal does not match authentication")
        return principal.principal_id

    @app.post("/api/v1/control/role-bindings", status_code=201)
    async def bind_control_role(body: ControlRoleBindingV1) -> dict[str, Any]:
        return control_authority.bind_role(body.principal_id, body.role, now=now())

    @app.post("/api/v1/control/xr-sessions", status_code=202)
    async def update_xr_intervention_session(
        body: XrSessionUpdateV1,
        request: Request,
    ) -> dict[str, Any]:
        authenticated_control_principal(request, body.principal_id)
        return control_authority.update_xr_session(body, received_at=now())

    @app.post(
        "/api/v1/orchestration/runs/{run_id}/resume-challenges",
        status_code=201,
    )
    async def begin_run_resume_challenge(
        run_id: str,
        body: ResumeChallengeCreateV1,
        request: Request,
    ) -> dict[str, Any]:
        principal = request.state.principal
        if auth_enabled:
            if principal is None or not control_authority.has_role(
                principal.principal_id, "manager"
            ):
                raise FleetAPIError(403, "Manager role is required to begin resume")
        return control_authority.begin_resume(
            run_id,
            now=now(),
            ttl=timedelta(seconds=body.ttl_seconds),
        )

    @app.post(
        "/api/v1/control/resume-challenges/{challenge_id}/approvals",
        status_code=202,
    )
    async def approve_run_resume(
        challenge_id: str,
        body: ResumeApprovalV1,
        request: Request,
    ) -> dict[str, Any]:
        principal_id = authenticated_control_principal(request, body.principal_id)
        return control_authority.approve_resume(
            challenge_id,
            principal_id=principal_id,
            session_id=body.session_id,
            now=now(),
        )

    @app.get("/api/v1/safety/latest")
    async def list_latest_safety_assessments(
        run_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        ),
        agent_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        ),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return safety.list_latest(run_id=run_id, agent_id=agent_id, limit=limit)

    @app.get("/api/v1/safety/events")
    async def list_safety_events(
        run_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        ),
        agent_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        ),
        state: SafetyState | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return safety.list_events(
            run_id=run_id,
            agent_id=agent_id,
            state=state,
            limit=limit,
        )

    @app.get("/api/v1/aas/projections")
    async def list_aas_projections(
        asset_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=192,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]*$",
        ),
    ) -> list[dict[str, Any]]:
        return require_aas_projections().list_projections(asset_id=asset_id)

    @app.post("/api/v1/aas/projections", status_code=201)
    async def import_aas_projection(
        request: Request,
        asset_id: str = Query(
            min_length=1,
            max_length=192,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]*$",
        ),
        source_file_name: str = Query(min_length=1, max_length=256),
        shell_id: str | None = Query(default=None, min_length=1, max_length=2048),
        robotics_profile_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=192,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]*$",
        ),
        opcua_endpoint: str | None = Query(default=None, min_length=1, max_length=2048),
    ) -> dict[str, Any]:
        declared = int(request.headers["content-length"])
        payload = await request.body()
        if len(payload) != declared:
            raise FleetAPIError(400, "AAS body size differs from Content-Length")
        if len(payload) > MAX_AAS_IMPORT_BYTES:
            raise FleetAPIError(413, "AAS import exceeds 64 MiB")
        principal = request.state.principal
        result = await require_aas_projections().import_artifact(
            payload,
            asset_id=asset_id,
            source_file_name=source_file_name,
            shell_id=shell_id,
            robotics_profile_id=robotics_profile_id,
            opcua_endpoint=opcua_endpoint,
            imported_by=None if principal is None else principal.principal_id,
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    @app.get("/api/v1/aas/projections/{projection_id}")
    async def get_aas_projection(projection_id: str) -> dict[str, Any]:
        return require_aas_projections().get_projection(projection_id)

    @app.get("/api/v1/aas/projections/{projection_id}/artifact")
    async def download_aas_artifact(projection_id: str) -> FileResponse:
        catalog = require_aas_projections()
        projection = catalog.get_projection(projection_id)
        path = await asyncio.to_thread(
            catalog.open_artifact,
            projection_id,
            environment=False,
        )
        return FileResponse(
            path,
            media_type=projection["artifact_media_type"],
            filename=projection["source_file_name"],
            headers={
                "Cache-Control": "private, no-store",
                "ETag": f'"sha256:{projection["artifact_sha256"]}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/aas/projections/{projection_id}/environment.json")
    async def export_aas_environment(projection_id: str) -> FileResponse:
        catalog = require_aas_projections()
        projection = catalog.get_projection(projection_id)
        path = await asyncio.to_thread(
            catalog.open_artifact,
            projection_id,
            environment=True,
        )
        return FileResponse(
            path,
            media_type="application/json",
            filename="aas-environment.json",
            headers={
                "Cache-Control": "private, no-store",
                "ETag": f'"sha256:{projection["environment_sha256"]}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/commissioning/g6b/cases")
    async def list_g6b_cases() -> list[dict[str, Any]]:
        return require_g6b_evidence().list_cases()

    @app.post("/api/v1/commissioning/g6b/cases", status_code=201)
    async def create_g6b_case(
        body: G6bCaseCreateV1,
        request: Request,
    ) -> dict[str, Any]:
        principal = request.state.principal
        result = require_g6b_evidence().create_case(
            body,
            created_by=None if principal is None else principal.principal_id,
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/commissioning/g6b/artifacts", status_code=201)
    async def import_g6b_supporting_artifact(
        request: Request,
        source_file_name: str = Query(min_length=1, max_length=256),
        sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    ) -> dict[str, Any]:
        payload = await request.body()
        principal = request.state.principal
        media_type = request.headers.get(
            "content-type",
            "application/octet-stream",
        ).split(";", 1)[0]
        result = await require_g6b_evidence().import_supporting_artifact(
            payload,
            source_file_name=source_file_name,
            media_type=media_type,
            expected_sha256=sha256,
            uploaded_by=None if principal is None else principal.principal_id,
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    @app.get("/api/v1/commissioning/g6b/artifacts/{artifact_id}")
    async def download_g6b_supporting_artifact(artifact_id: str) -> FileResponse:
        catalog = require_g6b_evidence()
        artifact = catalog.get_supporting_artifact(artifact_id)
        path = await asyncio.to_thread(catalog.open_supporting_artifact, artifact_id)
        return FileResponse(
            path,
            media_type=artifact["media_type"],
            filename=artifact["source_file_name"],
            headers={
                "Cache-Control": "private, no-store",
                "ETag": f'"sha256:{artifact["sha256"]}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/commissioning/g6b/cases/{case_id}")
    async def get_g6b_case(case_id: str) -> dict[str, Any]:
        return require_g6b_evidence().get_case(case_id)

    @app.get("/api/v1/commissioning/g6b/cases/{case_id}/submissions")
    async def list_g6b_submissions(case_id: str) -> list[dict[str, Any]]:
        return require_g6b_evidence().list_submissions(case_id)

    @app.post(
        "/api/v1/commissioning/g6b/cases/{case_id}/submissions",
        status_code=201,
    )
    async def submit_g6b_evidence(
        case_id: str,
        body: G6bEvidencePackageV1,
        request: Request,
    ) -> dict[str, Any]:
        if body.case_id != case_id:
            raise G6bEvidenceInvalid("G6b body case_id differs from the route")
        principal = request.state.principal
        result = await require_g6b_evidence().submit(
            body,
            submitted_by=None if principal is None else principal.principal_id,
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    @app.get("/api/v1/commissioning/g6b/cases/{case_id}/report")
    async def get_g6b_report(
        case_id: str,
        submission_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        ),
    ) -> dict[str, Any]:
        return require_g6b_evidence().report(
            case_id,
            generated_at=now(),
            submission_id=submission_id,
        ).model_dump(mode="json")

    @app.get("/api/v1/commissioning/g6b/submissions/{submission_id}/artifact")
    async def download_g6b_submission(submission_id: str) -> FileResponse:
        catalog = require_g6b_evidence()
        submission = catalog.get_submission(submission_id)
        path = await asyncio.to_thread(catalog.open_submission, submission_id)
        return FileResponse(
            path,
            media_type="application/json",
            filename=f"{submission_id}.json",
            headers={
                "Cache-Control": "private, no-store",
                "ETag": f'"sha256:{submission["payload_sha256"]}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post(
        "/api/v1/commissioning/g6b/submissions/{submission_id}/reviews",
        status_code=201,
    )
    async def review_g6b_submission(
        submission_id: str,
        body: G6bReviewRequestV1,
        request: Request,
    ) -> dict[str, Any]:
        principal = request.state.principal
        if principal is not None and principal.principal_id != body.reviewer_id:
            raise FleetAPIError(
                403,
                "authenticated principal differs from the G6b reviewer",
            )
        result = require_g6b_evidence().review(
            submission_id,
            body,
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    @app.get("/api/v1/recording-sessions")
    async def list_recording_sessions(
        run_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        ),
        agent_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        ),
        state: str | None = Query(default=None, pattern=r"^(STAGING|COMPLETE)$"),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return recordings.list_sessions(
            run_id=run_id,
            agent_id=agent_id,
            state=state,
            limit=limit,
        )

    @app.post("/api/v1/recording-sessions", status_code=201)
    async def register_recording_session(
        body: RecordingManifest,
        request: Request,
    ) -> dict[str, Any]:
        backend = require_object_store()
        principal = request.state.principal
        require_recording_agent_scope(request, body.agent_id)
        result = recordings.register(
            body,
            object_store=backend,
            created_by=None if principal is None else principal.principal_id,
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    @app.get("/api/v1/recording-sessions/{session_id}")
    async def get_recording_session(session_id: str) -> dict[str, Any]:
        return recordings.get_session(session_id)

    @app.get("/api/v1/dataset-derivations")
    async def list_dataset_derivations(
        session_id: str | None = Query(default=None, min_length=1, max_length=128),
        run_id: str | None = Query(default=None, min_length=1, max_length=64),
        state: str | None = Query(default=None, pattern=r"^(RUNNING|COMPLETE|FAILED)$"),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return derivations.list(
            session_id=session_id,
            run_id=run_id,
            state=state,
            limit=limit,
        )

    @app.get("/api/v1/dataset-derivations/{derivation_id}")
    async def get_dataset_derivation(derivation_id: str) -> dict[str, Any]:
        try:
            return derivations.get(derivation_id)
        except DerivationNotFound as exc:
            raise FleetAPIError(404, str(exc)) from exc

    @app.get("/api/v1/dataset-derivations/{derivation_id}/download")
    async def download_dataset_derivation(derivation_id: str) -> FileResponse:
        backend = require_object_store()
        try:
            derivation = derivations.get(derivation_id)
        except DerivationNotFound as exc:
            raise FleetAPIError(404, str(exc)) from exc
        if derivation["state"] != "COMPLETE":
            raise FleetAPIError(409, "dataset derivation is not complete")
        try:
            path = await asyncio.to_thread(
                backend.open_path,
                derivation["output_object_key"],
                size_bytes=derivation["output_size_bytes"],
                sha256=derivation["output_sha256"],
            )
        except ObjectStoreError as exc:
            raise FleetAPIError(500, "derived dataset integrity check failed") from exc
        return FileResponse(
            path,
            media_type="application/x-tar",
            filename=f"{derivation_id}.tar",
            headers={
                "ETag": f'"sha256:{derivation["output_sha256"]}"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    def require_quality() -> RecordingQualityService:
        if quality is None:
            raise FleetAPIError(503, "Central recording quality analysis is not configured")
        return quality

    @app.get("/api/v1/recording-sessions/{session_id}/quality")
    async def get_recording_quality(session_id: str) -> dict[str, Any]:
        recordings.get_session(session_id)
        result = recordings.quality_report(session_id)
        if result is None:
            return {
                "available": False,
                "session_id": session_id,
                "state": "UNKNOWN",
                "streams": [],
            }
        return result

    @app.post("/api/v1/recording-sessions/{session_id}/quality/analyze")
    async def analyze_recording_quality(session_id: str) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(require_quality().analyze, session_id)
        except QualityUnavailable as exc:
            raise FleetAPIError(422, str(exc)) from exc
        app.state.fleet_revision += 1
        return result

    @app.get("/api/v1/recording-sessions/{session_id}/quality/report")
    async def download_recording_quality_report(session_id: str) -> FileResponse:
        backend = require_object_store()
        report = recordings.quality_object(session_id)
        try:
            path = await asyncio.to_thread(
                backend.open_path,
                report["object_key"],
                size_bytes=report["report_size_bytes"],
                sha256=report["report_sha256"],
            )
        except ObjectStoreError as exc:
            raise FleetAPIError(500, "quality report integrity check failed") from exc
        return FileResponse(
            path,
            media_type="application/json",
            filename=f"{session_id}-quality.json",
            headers={
                "ETag": f'"sha256:{report["report_sha256"]}"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    def require_playback() -> RecordingPlayback:
        if playback is None:
            raise FleetAPIError(503, "Central recording playback is not configured")
        return playback

    @app.get("/api/v1/recording-sessions/{session_id}/playback")
    async def describe_recording_playback(session_id: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(require_playback().describe, session_id)
        except PlaybackNotFound as exc:
            raise FleetAPIError(404, str(exc)) from exc
        except PlaybackUnavailable as exc:
            raise FleetAPIError(409, str(exc)) from exc
        except PlaybackInvalid as exc:
            raise FleetAPIError(422, str(exc)) from exc

    @app.get("/api/v1/recording-sessions/{session_id}/playback/frame")
    async def get_recording_playback_frame(
        session_id: str,
        topic_key: str = Query(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        ),
        offset_ms: int = Query(ge=0, le=28_800_000),
    ) -> Response:
        try:
            frame = await asyncio.to_thread(
                require_playback().frame,
                session_id,
                topic_key,
                offset_ms,
            )
        except PlaybackNotFound as exc:
            raise FleetAPIError(404, str(exc)) from exc
        except PlaybackUnavailable as exc:
            raise FleetAPIError(409, str(exc)) from exc
        except PlaybackInvalid as exc:
            raise FleetAPIError(422, str(exc)) from exc
        except PlaybackBusy as exc:
            raise FleetAPIError(429, str(exc)) from exc
        return Response(
            content=frame.data,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "X-SANG-Log-Time-Ns": str(frame.log_time_ns),
                "X-SANG-Source-Time-Ns": str(frame.source_time_ns),
                "X-SANG-Frame-Offset-Ms": str(frame.frame_offset_ms),
                "X-SANG-Source-Offset-Ms": str(frame.source_offset_ms),
                "X-SANG-Skew-Ms": str(frame.skew_ms),
            },
        )

    @app.put(
        "/api/v1/recording-sessions/{session_id}/artifacts/{artifact_path:path}"
    )
    async def upload_recording_artifact(
        session_id: str,
        artifact_path: str,
        request: Request,
    ) -> dict[str, Any]:
        backend = require_object_store()
        require_recording_agent_scope(
            request,
            recordings.get_session(session_id)["agent_id"],
        )
        expected = recordings.artifact_expectation(session_id, artifact_path)
        raw_length = request.headers.get("content-length")
        if raw_length is None:
            raise FleetAPIError(411, "artifact upload requires Content-Length")
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise FleetAPIError(400, "invalid Content-Length") from exc
        if content_length != expected["expected_size_bytes"]:
            raise RecordingInvalid("artifact Content-Length differs from the manifest")
        await backend.put_stream(
            expected["object_key"],
            request.stream(),
            size_bytes=expected["expected_size_bytes"],
            sha256=expected["expected_sha256"],
        )
        result = recordings.mark_uploaded(
            session_id,
            artifact_path,
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    @app.post("/api/v1/recording-sessions/{session_id}/finalize")
    async def finalize_recording_session(
        session_id: str,
        body: RecordingFinalize,
        request: Request,
    ) -> dict[str, Any]:
        require_recording_agent_scope(
            request,
            recordings.get_session(session_id)["agent_id"],
        )
        result = await recordings.finalize_async(
            session_id,
            expected_manifest_sha256=body.expected_manifest_sha256,
            object_store=require_object_store(),
            occurred_at=now(),
        )
        app.state.fleet_revision += 1
        return result

    @app.get(
        "/api/v1/recording-sessions/{session_id}/artifacts/{artifact_path:path}"
    )
    async def download_recording_artifact(
        session_id: str,
        artifact_path: str,
    ) -> FileResponse:
        backend = require_object_store()
        artifact = recordings.artifact(session_id, artifact_path)
        path = await asyncio.to_thread(
            backend.open_path,
            artifact["object_key"],
            size_bytes=artifact["expected_size_bytes"],
            sha256=artifact["expected_sha256"],
        )
        return FileResponse(
            path,
            media_type=artifact["media_type"],
            filename=path.name,
            headers={
                "Cache-Control": "private, no-store",
                "ETag": f'"sha256:{artifact["expected_sha256"]}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    def stream_camera_asset(asset_id: str) -> StreamingResponse:
        upstream = camera_upstream_url(asset_id)
        if not preview_slot.acquire(blocking=False):
            raise FleetAPIError(429, "another camera preview is already active")
        request = UrlRequest(
            upstream,
            headers={
                "Accept": "multipart/x-mixed-replace",
                "User-Agent": "sang-central-preview/1",
            },
        )
        try:
            response = preview_opener.open(
                request,
                timeout=5.0,
            )
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            preview_slot.release()
            raise FleetAPIError(502, f"camera preview upstream failed: {exc}") from exc
        content_type = response.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/x-mixed-replace"):
            response.close()
            preview_slot.release()
            raise FleetAPIError(502, "camera preview upstream is not an MJPEG stream")

        release_lock = threading.Lock()
        released = False

        def release_preview() -> None:
            nonlocal released
            with release_lock:
                if released:
                    return
                released = True
            response.close()
            preview_slot.release()

        def chunks():
            try:
                while True:
                    block = response.read(64 * 1024)
                    if not block:
                        return
                    yield block
            finally:
                release_preview()

        return StreamingResponse(
            chunks(),
            headers={
                "Content-Type": content_type,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Accel-Buffering": "no",
            },
            background=BackgroundTask(release_preview),
        )

    @app.get("/api/v1/cameras/{asset_id}/preview.mjpg")
    def camera_preview(asset_id: str) -> StreamingResponse:
        return stream_camera_asset(asset_id)

    @app.get(
        "/api/v1/task-runs/{run_id}/cameras/{asset_id}/preview.mjpg"
    )
    def task_run_camera_preview(
        run_id: str,
        asset_id: str,
    ) -> StreamingResponse:
        task_run = task_run_or_404(run_id)
        if not any(
            item["asset_id"] == asset_id
            and item["asset"]["asset_type"] == "camera"
            for item in task_run["assignments"]
        ):
            raise ConfigurationNotFound(
                f"camera asset {asset_id} is not assigned to task run {run_id}"
            )
        return stream_camera_asset(asset_id)

    @app.websocket("/api/v1/fleet")
    async def fleet_websocket(websocket: WebSocket) -> None:
        if not await authorize_websocket(websocket):
            return
        await websocket.accept()
        last_revision = -1
        next_periodic = 0.0
        try:
            while True:
                current_loop_time = time.monotonic()
                revision = app.state.fleet_revision
                if revision != last_revision or current_loop_time >= next_periodic:
                    payload = fleet_payload()
                    await websocket.send_json(payload)
                    last_revision = app.state.fleet_revision
                    next_periodic = current_loop_time + websocket_interval_s
                await asyncio.sleep(min(0.1, websocket_interval_s))
        except (RuntimeError, WebSocketDisconnect):
            pass

    @app.websocket("/api/v1/task-runs/{run_id}/stream")
    async def task_run_fleet_websocket(websocket: WebSocket, run_id: str) -> None:
        if not await authorize_websocket(websocket):
            return
        if store.get_task_run(run_id) is None:
            await websocket.close(code=4404, reason="unknown task run")
            return
        await websocket.accept()
        last_revision = -1
        next_periodic = 0.0
        try:
            while True:
                current_loop_time = time.monotonic()
                revision = app.state.fleet_revision
                if revision != last_revision or current_loop_time >= next_periodic:
                    await websocket.send_json(task_run_fleet_payload(run_id))
                    last_revision = app.state.fleet_revision
                    next_periodic = current_loop_time + websocket_interval_s
                await asyncio.sleep(min(0.1, websocket_interval_s))
        except (RuntimeError, WebSocketDisconnect):
            pass

    if operator_ui_dir is not None:
        operator_path = Path(operator_ui_dir)
        if not operator_path.is_dir():
            raise ValueError(f"Operator UI directory does not exist: {operator_path}")
        app.mount(
            "/operator",
            StaticFiles(directory=str(operator_path), html=True),
            name="operator-ui",
        )

    if ui_dir is not None:
        path = Path(ui_dir)
        if not path.is_dir():
            raise ValueError(f"Admin UI directory does not exist: {path}")
        app.mount("/", StaticFiles(directory=str(path), html=True), name="admin-ui")

    return app
