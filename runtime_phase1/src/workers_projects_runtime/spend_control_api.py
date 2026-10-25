"""Owner-scoped HTTP views of the existing admission controls."""
from __future__ import annotations

from typing import Any, Literal
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from .spend_admission import SpendPolicyRevisionConflict


class SpendPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    policy: dict[str, Any]


class AdmissionHoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scope_type: Literal["owner", "project"] = "owner"
    scope_id: str = Field(default="", max_length=512)
    reason_code: Literal["operator_pause", "transport_integrity"] = "operator_pause"


class AdmissionResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_generation: int = Field(ge=1)


def install_spend_control_routes(app, store, service, principal, project_scope):
    def owner(request):
        _, tenant_id, owner_id = principal(request)
        return tenant_id, owner_id

    def scope(request, scope_type, scope_id):
        if scope_type == "project":
            _, tenant_id, owner_id = project_scope("project", scope_id, request)
            return tenant_id, owner_id
        if scope_type != "owner" or scope_id:
            raise HTTPException(status_code=422, detail={"code": "invalid_scope"})
        return owner(request)

    def invoke(callback, **kwargs):
        try:
            return callback(**kwargs)
        except SpendPolicyRevisionConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "spend_control_changed"}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_spend_control"}) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail={"code": "spend_control_changed"}) from exc

    @app.get("/v1/spend-policy")
    def get_owner_spend_policy(request: Request):
        tenant_id, owner_id = owner(request)
        return store.get_spend_policy(tenant_id=tenant_id, owner_id=owner_id)

    @app.put("/v1/spend-policy")
    def update_owner_spend_policy(payload: SpendPolicyUpdate, request: Request):
        tenant_id, owner_id = owner(request)
        return invoke(store.put_spend_policy, tenant_id=tenant_id, owner_id=owner_id, **payload.model_dump())

    @app.get("/v1/projects/{project_id}/spend-policy")
    def get_project_spend_policy(project_id: str, request: Request):
        tenant_id, owner_id = scope(request, "project", project_id)
        return store.get_spend_policy(tenant_id=tenant_id, owner_id=owner_id, scope_type="project", scope_id=project_id)

    @app.put("/v1/projects/{project_id}/spend-policy")
    def update_project_spend_policy(project_id: str, payload: SpendPolicyUpdate, request: Request):
        tenant_id, owner_id = scope(request, "project", project_id)
        return invoke(store.put_spend_policy, tenant_id=tenant_id, owner_id=owner_id,
            scope_type="project", scope_id=project_id, **payload.model_dump())

    @app.get("/v1/admission-holds")
    def list_holds(request: Request):
        tenant_id, owner_id = owner(request)
        return {"items": store.list_admission_holds(tenant_id=tenant_id, owner_id=owner_id)}

    @app.post("/v1/admission-holds", status_code=201)
    def place_hold(payload: AdmissionHoldRequest, request: Request):
        tenant_id, owner_id = scope(request, payload.scope_type, payload.scope_id)
        return invoke(store.place_admission_hold, tenant_id=tenant_id, owner_id=owner_id, **payload.model_dump())

    @app.post("/v1/admission-holds/{hold_id}/resume")
    def resume_hold(hold_id: str, payload: AdmissionResumeRequest, request: Request):
        tenant_id, owner_id = owner(request)
        return invoke(service.resume_admission_hold, hold_id=hold_id, tenant_id=tenant_id,
            owner_id=owner_id, expected_generation=payload.expected_generation)
