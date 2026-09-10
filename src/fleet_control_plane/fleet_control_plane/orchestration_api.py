"""FastAPI routes for the immutable Central orchestration registry and Runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query

from fleet_control_plane.control_signing import SignatureInvalid, VerificationKeyRing
from fleet_control_plane.orchestration_models import (
    CalibrationV1,
    CapabilityProfileV1,
    CoordinatedGroupV1,
    ControlEnvelopeV1,
    DeviceManifestV1,
    RuntimeBindingV1,
    UnityBuildAttestationV1,
    WorkflowDefinitionV2,
    ZoneV1,
)
from fleet_control_plane.orchestration_runtime_models import (
    GenericRegistryObjectV1,
    OrchestrationRunCreateV1,
    OrchestrationRunTransitionV1,
    ProducerBindingCreateV1,
    RegistryKind,
    SafetyPolicyV2,
)
from fleet_control_plane.orchestration_store import (
    OrchestrationConflict,
    OrchestrationInvalid,
    OrchestrationNotFound,
    OrchestrationStore,
)


def _translate(error: ValueError) -> HTTPException:
    if isinstance(error, OrchestrationNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, OrchestrationConflict):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=422, detail=str(error))


def orchestration_router(
    store: OrchestrationStore,
    *,
    now: Callable[[], datetime],
    unity_build_keyring: VerificationKeyRing | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/orchestration", tags=["orchestration"])

    def register(model) -> dict[str, Any]:
        try:
            return store.register_model(model, occurred_at=now())
        except (OrchestrationConflict, OrchestrationInvalid, OrchestrationNotFound) as exc:
            raise _translate(exc) from exc

    @router.post("/capability-profiles", status_code=201)
    def register_capability_profile(body: CapabilityProfileV1) -> dict[str, Any]:
        return register(body)

    @router.post("/device-manifests", status_code=201)
    def register_device_manifest(body: DeviceManifestV1) -> dict[str, Any]:
        return register(body)

    @router.post("/workflows", status_code=201)
    def register_workflow(body: WorkflowDefinitionV2) -> dict[str, Any]:
        return register(body)

    @router.post("/safety-policies", status_code=201)
    def register_safety_policy(body: SafetyPolicyV2) -> dict[str, Any]:
        return register(body)

    @router.post("/runtime-bindings", status_code=201)
    def register_runtime_binding(body: RuntimeBindingV1) -> dict[str, Any]:
        return register(body)

    @router.post("/coordinated-groups", status_code=201)
    def register_coordinated_group(body: CoordinatedGroupV1) -> dict[str, Any]:
        return register(body)

    @router.post("/zones", status_code=201)
    def register_zone(body: ZoneV1) -> dict[str, Any]:
        return register(body)

    @router.post("/calibrations", status_code=201)
    def register_calibration(body: CalibrationV1) -> dict[str, Any]:
        return register(body)

    @router.post("/control-envelopes", status_code=201)
    def register_control_envelope(body: ControlEnvelopeV1) -> dict[str, Any]:
        return register(body)

    @router.post("/unity-build-attestations", status_code=201)
    def register_unity_build(body: UnityBuildAttestationV1) -> dict[str, Any]:
        if unity_build_keyring is None:
            raise HTTPException(
                status_code=503,
                detail="trusted Unity build verification keys are not configured",
            )
        try:
            unity_build_keyring.verify_payload(
                key_id=body.signer_key_id,
                signature_text=body.signature,
                payload=body,
                at=now(),
            )
        except SignatureInvalid as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return register(body)

    @router.post("/registry/{kind}", status_code=201)
    def register_generic(
        kind: RegistryKind,
        body: GenericRegistryObjectV1,
    ) -> dict[str, Any]:
        try:
            return store.register_generic(kind, body, occurred_at=now())
        except (OrchestrationConflict, OrchestrationInvalid, OrchestrationNotFound) as exc:
            raise _translate(exc) from exc

    @router.get("/registry")
    def list_registry(kind: RegistryKind | None = Query(default=None)) -> list[dict[str, Any]]:
        return store.list_registry(kind)

    @router.post("/runs", status_code=201)
    def create_run(body: OrchestrationRunCreateV1) -> dict[str, Any]:
        try:
            return store.create_run(body, occurred_at=now())
        except (OrchestrationConflict, OrchestrationInvalid, OrchestrationNotFound) as exc:
            raise _translate(exc) from exc

    @router.get("/runs")
    def list_runs() -> list[dict[str, Any]]:
        return store.list_runs()

    @router.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        result = store.get_run(run_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"unknown orchestration Run: {run_id}")
        return result

    @router.post("/runs/{run_id}/transition")
    def transition_run(
        run_id: str,
        body: OrchestrationRunTransitionV1,
    ) -> dict[str, Any]:
        try:
            return store.transition_run(run_id, body, occurred_at=now())
        except (OrchestrationConflict, OrchestrationInvalid, OrchestrationNotFound) as exc:
            raise _translate(exc) from exc

    @router.post("/runs/{run_id}/producers", status_code=201)
    def bind_producer(run_id: str, body: ProducerBindingCreateV1) -> dict[str, Any]:
        try:
            return store.bind_producer(run_id, body, occurred_at=now())
        except (OrchestrationConflict, OrchestrationInvalid, OrchestrationNotFound) as exc:
            raise _translate(exc) from exc

    @router.get("/runs/{run_id}/events")
    def list_events(run_id: str) -> list[dict[str, Any]]:
        if store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown orchestration Run: {run_id}")
        return store.list_events(run_id)

    return router
