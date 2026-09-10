"""Central same-origin proxy for the loopback Unity runtime supervisor."""

from __future__ import annotations

import json
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_STATUS_BYTES = 256 * 1024


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "Unity runtime redirects are disabled", headers, fp)


class UnityRuntimeProxy:
    def __init__(self, base_url: str) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in LOOPBACK_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Unity runtime URL must be an unauthenticated loopback HTTP URL")
        self.base_url = base_url.rstrip("/") + "/"
        self._opener = build_opener(_RejectRedirects())
        self._preview_slot = threading.BoundedSemaphore(value=2)

    def request_json(self, method: str, path: str) -> dict[str, Any]:
        request = Request(
            urljoin(self.base_url, path.lstrip("/")),
            data=b"" if method == "POST" else None,
            method=method,
            headers={"Accept": "application/json", "User-Agent": "sang-central/1"},
        )
        try:
            with self._opener.open(request, timeout=5.0) as response:
                content_type = response.headers.get("Content-Type", "")
                if not content_type.lower().startswith("application/json"):
                    raise HTTPException(
                        status_code=502,
                        detail="Unity runtime returned a non-JSON response",
                    )
                payload = response.read(MAX_STATUS_BYTES + 1)
        except HTTPException:
            raise
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Unity runtime supervisor is unavailable: {exc}",
            ) from exc
        if len(payload) > MAX_STATUS_BYTES:
            raise HTTPException(status_code=502, detail="Unity runtime response is too large")
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail="Unity runtime returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise HTTPException(status_code=502, detail="Unity runtime JSON must be an object")
        return value

    def preview_response(self) -> StreamingResponse:
        if not self._preview_slot.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="Unity preview client limit reached")
        request = Request(
            urljoin(self.base_url, "preview.mjpg"),
            method="GET",
            headers={
                "Accept": "multipart/x-mixed-replace",
                "User-Agent": "sang-central/1",
            },
        )
        try:
            response = self._opener.open(request, timeout=5.0)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            self._preview_slot.release()
            raise HTTPException(
                status_code=502,
                detail=f"Unity preview is unavailable: {exc}",
            ) from exc
        content_type = response.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/x-mixed-replace"):
            response.close()
            self._preview_slot.release()
            raise HTTPException(status_code=502, detail="Unity preview is not MJPEG")

        released = False
        release_lock = threading.Lock()

        def release() -> None:
            nonlocal released
            with release_lock:
                if released:
                    return
                released = True
            response.close()
            self._preview_slot.release()

        def chunks():
            try:
                while True:
                    block = response.read(64 * 1024)
                    if not block:
                        return
                    yield block
            finally:
                release()

        return StreamingResponse(
            chunks(),
            headers={
                "Content-Type": content_type,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Accel-Buffering": "no",
            },
            background=BackgroundTask(release),
        )


def unity_runtime_router(base_url: str | None) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/workflow-authoring/runtime",
        tags=["workflow-authoring"],
    )
    proxy = None if base_url is None else UnityRuntimeProxy(base_url)

    @router.get("")
    def status() -> dict[str, Any]:
        if proxy is None:
            return {
                "schema_version": 1,
                "configured": False,
                "running": False,
                "ready": False,
                "preview_available": False,
                "physical_output_connected": False,
            }
        return proxy.request_json("GET", "status")

    @router.post("/start")
    def start() -> dict[str, Any]:
        if proxy is None:
            raise HTTPException(status_code=503, detail="Unity runtime is not configured")
        return proxy.request_json("POST", "start")

    @router.post("/stop")
    def stop() -> dict[str, Any]:
        if proxy is None:
            raise HTTPException(status_code=503, detail="Unity runtime is not configured")
        return proxy.request_json("POST", "stop")

    @router.get("/preview.mjpg")
    def preview() -> StreamingResponse:
        if proxy is None:
            raise HTTPException(status_code=503, detail="Unity runtime is not configured")
        return proxy.preview_response()

    return router
