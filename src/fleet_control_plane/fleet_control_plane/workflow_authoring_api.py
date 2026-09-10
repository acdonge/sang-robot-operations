"""Manager API for Workflow drafts, immutable publications, and simulation queueing."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from fastapi import APIRouter, HTTPException, Request

from fleet_control_plane.action_library import (
    ActionDraftCreateV1,
    ActionDraftReplaceV1,
    ActionLibraryConflict,
    ActionLibraryInvalid,
    ActionLibraryNotFound,
    ActionPublishV1,
    ActionSimulationValidateV1,
)
from fleet_control_plane.orchestration_models import WorkflowDefinitionV2
from fleet_control_plane.workflow_authoring import (
    CommissioningCandidateCreateV1,
    SimulationValidationProfileCreateV1,
    WorkflowAuthoringConflict,
    WorkflowAuthoringInvalid,
    WorkflowAuthoringNotFound,
    WorkflowAuthoringStore,
    WorkflowAuthoringUnavailable,
    WorkflowDraftCloneV1,
    WorkflowDraftCreateV1,
    WorkflowDraftReplaceV1,
    WorkflowPublishV1,
    WorkflowSimulationControlV1,
    WorkflowSimulationQueueV1,
)


def _principal_id(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    return "local-auth-disabled" if principal is None else principal.principal_id


def _translate(error: ValueError) -> HTTPException:
    if isinstance(error, (ActionLibraryNotFound, WorkflowAuthoringNotFound)):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, (ActionLibraryConflict, WorkflowAuthoringConflict)):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, WorkflowAuthoringUnavailable):
        return HTTPException(status_code=503, detail=str(error))
    return HTTPException(status_code=422, detail=str(error))


def workflow_authoring_router(
    store: WorkflowAuthoringStore,
    *,
    now: Callable[[], datetime],
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/workflow-authoring",
        tags=["workflow-authoring"],
    )

    @router.get("/drafts")
    def list_drafts() -> list[dict]:
        return store.list_drafts()

    @router.get("/skill-definitions")
    def list_skill_definitions() -> list[dict]:
        return store.list_skill_definitions()

    @router.get("/action-definitions")
    def list_action_definitions() -> list[dict]:
        return store.list_action_definitions()

    @router.get("/action-library")
    def list_action_library() -> list[dict]:
        return store.action_library.list_entries()

    @router.post("/action-library/drafts", status_code=201)
    def create_action_draft(body: ActionDraftCreateV1, request: Request) -> dict:
        try:
            return store.action_library.create_draft(
                body,
                principal_id=_principal_id(request),
                occurred_at=now(),
            )
        except (ActionLibraryConflict, ActionLibraryInvalid) as exc:
            raise _translate(exc) from exc

    @router.get("/action-library/drafts/{draft_id}")
    def get_action_draft(draft_id: str) -> dict:
        try:
            return store.action_library.get_draft(draft_id)
        except ActionLibraryNotFound as exc:
            raise _translate(exc) from exc

    @router.put("/action-library/drafts/{draft_id}")
    def replace_action_draft(
        draft_id: str,
        body: ActionDraftReplaceV1,
        request: Request,
    ) -> dict:
        try:
            return store.action_library.replace_draft(
                draft_id,
                body,
                principal_id=_principal_id(request),
                occurred_at=now(),
            )
        except (
            ActionLibraryConflict,
            ActionLibraryInvalid,
            ActionLibraryNotFound,
        ) as exc:
            raise _translate(exc) from exc

    @router.post("/action-library/drafts/{draft_id}/source-validation")
    def validate_action_source(draft_id: str) -> dict:
        try:
            return store.action_library.validate_source(draft_id, occurred_at=now())
        except (
            ActionLibraryConflict,
            ActionLibraryInvalid,
            ActionLibraryNotFound,
        ) as exc:
            raise _translate(exc) from exc

    @router.post("/action-library/drafts/{draft_id}/simulation-validation")
    def validate_action_simulation(
        draft_id: str,
        body: ActionSimulationValidateV1,
    ) -> dict:
        try:
            return store.action_library.validate_simulation(
                draft_id,
                body,
                occurred_at=now(),
            )
        except (
            ActionLibraryConflict,
            ActionLibraryInvalid,
            ActionLibraryNotFound,
        ) as exc:
            raise _translate(exc) from exc

    @router.post("/action-library/drafts/{draft_id}/publish", status_code=201)
    def publish_action(
        draft_id: str,
        body: ActionPublishV1,
        request: Request,
    ) -> dict:
        try:
            return store.action_library.publish(
                draft_id,
                body,
                principal_id=_principal_id(request),
                occurred_at=now(),
            )
        except (
            ActionLibraryConflict,
            ActionLibraryInvalid,
            ActionLibraryNotFound,
        ) as exc:
            raise _translate(exc) from exc

    @router.post("/drafts", status_code=201)
    def create_draft(body: WorkflowDraftCreateV1, request: Request) -> dict:
        try:
            return store.create_draft(
                body,
                principal_id=_principal_id(request),
                occurred_at=now(),
            )
        except (WorkflowAuthoringConflict, WorkflowAuthoringInvalid) as exc:
            raise _translate(exc) from exc

    @router.post("/drafts/clone", status_code=201)
    def clone_draft(body: WorkflowDraftCloneV1, request: Request) -> dict:
        try:
            return store.clone_draft(
                body,
                principal_id=_principal_id(request),
                occurred_at=now(),
            )
        except (
            WorkflowAuthoringConflict,
            WorkflowAuthoringInvalid,
            WorkflowAuthoringNotFound,
        ) as exc:
            raise _translate(exc) from exc

    @router.get("/drafts/{draft_id}")
    def get_draft(draft_id: str) -> dict:
        try:
            return store.get_draft(draft_id)
        except WorkflowAuthoringNotFound as exc:
            raise _translate(exc) from exc

    @router.put("/drafts/{draft_id}")
    def update_draft(
        draft_id: str,
        body: WorkflowDraftReplaceV1,
        request: Request,
    ) -> dict:
        try:
            return store.update_draft(
                draft_id,
                body,
                principal_id=_principal_id(request),
                occurred_at=now(),
            )
        except (
            WorkflowAuthoringConflict,
            WorkflowAuthoringInvalid,
            WorkflowAuthoringNotFound,
        ) as exc:
            raise _translate(exc) from exc

    @router.post("/drafts/{draft_id}/validate")
    def validate_draft(draft_id: str) -> dict:
        try:
            return store.validate_draft(draft_id)
        except WorkflowAuthoringNotFound as exc:
            raise _translate(exc) from exc

    @router.post("/drafts/{draft_id}/publish", status_code=201)
    def publish_draft(
        draft_id: str,
        body: WorkflowPublishV1,
        request: Request,
    ) -> dict:
        try:
            return store.publish(
                draft_id,
                body,
                principal_id=_principal_id(request),
                occurred_at=now(),
            )
        except (
            WorkflowAuthoringConflict,
            WorkflowAuthoringInvalid,
            WorkflowAuthoringNotFound,
        ) as exc:
            raise _translate(exc) from exc

    @router.get("/publications")
    def list_publications() -> list[dict]:
        results = []
        for publication in store.list_publications():
            workflow = WorkflowDefinitionV2.model_validate(publication["workflow"])
            compatibility = []
            for adapter_id, (profile, digest) in store.adapter_profiles.items():
                issues = store.compatibility_issues(workflow, profile)
                compatibility.append(
                    {
                        "adapter_id": adapter_id,
                        "profile_sha256": digest,
                        "runnable": not issues,
                        "issues": issues,
                    }
                )
            results.append(
                {**publication, "simulation_compatibility": compatibility}
            )
        return results

    @router.get("/simulation-adapters")
    def list_simulation_adapters() -> list[dict]:
        return store.list_adapter_status(occurred_at=now())

    @router.get("/simulation-profiles")
    def list_simulation_profiles() -> list[dict]:
        return store.list_validation_profiles()

    @router.post("/simulation-profiles", status_code=201)
    def create_simulation_profile(
        body: SimulationValidationProfileCreateV1,
        request: Request,
    ) -> dict:
        try:
            return store.create_validation_profile(
                body,
                principal_id=_principal_id(request),
                occurred_at=now(),
            )
        except (
            WorkflowAuthoringConflict,
            WorkflowAuthoringInvalid,
            WorkflowAuthoringNotFound,
        ) as exc:
            raise _translate(exc) from exc

    @router.get("/commissioning-candidates")
    def list_commissioning_candidates() -> list[dict]:
        return store.list_commissioning_candidates()

    @router.post("/commissioning-candidates", status_code=201)
    def create_commissioning_candidate(
        body: CommissioningCandidateCreateV1,
        request: Request,
    ) -> dict:
        try:
            return store.create_commissioning_candidate(
                body,
                principal_id=_principal_id(request),
                occurred_at=now(),
            )
        except (
            WorkflowAuthoringConflict,
            WorkflowAuthoringInvalid,
            WorkflowAuthoringNotFound,
        ) as exc:
            raise _translate(exc) from exc

    @router.post("/simulation-runs", status_code=202)
    def queue_simulation(
        body: WorkflowSimulationQueueV1,
        request: Request,
    ) -> dict:
        try:
            return store.queue_simulation(
                body,
                principal_id=_principal_id(request),
                occurred_at=now(),
            )
        except (
            WorkflowAuthoringConflict,
            WorkflowAuthoringInvalid,
            WorkflowAuthoringNotFound,
            WorkflowAuthoringUnavailable,
        ) as exc:
            raise _translate(exc) from exc

    @router.get("/simulation-runs")
    def list_simulation_runs() -> list[dict]:
        return store.list_simulation_runs(occurred_at=now())

    @router.get("/simulation-runs/{run_id}")
    def get_simulation_run(run_id: str) -> dict:
        try:
            return store.get_simulation_run(run_id, occurred_at=now())
        except WorkflowAuthoringNotFound as exc:
            raise _translate(exc) from exc

    @router.post("/simulation-runs/{run_id}/controls", status_code=202)
    def control_simulation_run(
        run_id: str,
        body: WorkflowSimulationControlV1,
        request: Request,
    ) -> dict:
        try:
            return store.request_simulation_control(
                run_id,
                body,
                principal_id=_principal_id(request),
                occurred_at=now(),
            )
        except (
            WorkflowAuthoringConflict,
            WorkflowAuthoringInvalid,
            WorkflowAuthoringNotFound,
            WorkflowAuthoringUnavailable,
        ) as exc:
            raise _translate(exc) from exc

    return router
