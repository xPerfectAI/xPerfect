"""Owner routes for Experts, their Chat and Ask-me-first decisions."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import HTTPException, Request
from pydantic import Field, ValidationError

from .coordinator import CoordinatorConflict, CoordinatorScopeError, StrictModel
from .channels import DeliveryResult, InboundMessage
from .experts import ExpertError, ExpertSpec


class ExpertCreateInput(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    expert: ExpertSpec
    personal_library_refs: list[dict[str, str]] = Field(default_factory=list, max_length=128)


class ExpertUpdateInput(StrictModel):
    expected_catalog_revision: int
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    expert: ExpertSpec | None = None
    personal_library_refs: list[dict[str, str]] | None = Field(default=None, max_length=128)


class ReviewDecisionInput(StrictModel):
    decision: str = Field(pattern="^(approve|reject)$")
    expected_digest: str = Field(min_length=64, max_length=64)
    note: str = Field(default="", max_length=2000)


def install_expert_routes(app, experts, principal: Callable[[Request], tuple[Any, str, str]],
                          auth_context: Callable[[Request], Any]) -> None:
    def invoke(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ExpertError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc
        except CoordinatorScopeError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (CoordinatorConflict, ValidationError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    def owner(request: Request) -> tuple[str, str]:
        ctx, tenant, user = principal(request)
        if ctx.auth_mode == "signed_link" or str(ctx.role or "").lower() == "viewer":
            raise HTTPException(403, "Sign in as the owner to manage Experts")
        return tenant, user

    @app.post("/v1/experts", status_code=201)
    def create_expert(payload: ExpertCreateInput, request: Request):
        tenant, user = owner(request)
        return invoke(experts.create_expert, tenant, user, name=payload.name, description=payload.description,
                      spec=payload.expert, personal_library_refs=payload.personal_library_refs)

    @app.get("/v1/experts/{expert_id}")
    def read_expert(expert_id: str, request: Request):
        tenant, user = owner(request)
        return experts.describe(invoke(experts.resolve, tenant, user, expert_id))

    @app.patch("/v1/experts/{expert_id}")
    def update_expert(expert_id: str, payload: ExpertUpdateInput, request: Request):
        tenant, user = owner(request)
        return invoke(experts.update_expert, tenant, user, expert_id,
                      expected_catalog_revision=payload.expected_catalog_revision, name=payload.name,
                      description=payload.description, spec=payload.expert,
                      personal_library_refs=payload.personal_library_refs)

    @app.post("/v1/experts/{expert_id}/chat")
    def owner_chat(expert_id: str, request: Request):
        """The owner's own Chat with the Expert; it uses the ordinary conversation routes."""
        tenant, user = owner(request)
        speaker = invoke(experts.queen, tenant, user, expert_id, channel="chat", thread_key="owner",
                         sender={"name": "Owner"})
        return {"conversation_id": speaker["conversation_id"], "lineage_id": speaker["lineage_id"]}

    @app.get("/v1/experts/{expert_id}/jobs")
    def expert_jobs(expert_id: str, request: Request):
        tenant, user = owner(request)
        template = invoke(experts.resolve, tenant, user, expert_id)
        with experts.store._connect() as conn:
            rows = conn.execute("SELECT * FROM expert_jobs WHERE tenant_id=? AND owner_id=? AND lineage_id=? "
                                "ORDER BY created_at DESC LIMIT 100", (tenant, user, template["lineage_id"])).fetchall()
        return {"items": [experts._job_view(dict(row)) for row in rows]}

    @app.get("/v1/experts-talk-speeds")
    def talk_speeds(request: Request):
        tenant, user = owner(request)
        return {"items": experts.talk_speeds(tenant, user)}

    @app.get("/v1/expert-notices")
    def list_notices(request: Request):
        tenant, user = owner(request)
        return {"items": experts.notices(tenant, user)}

    @app.get("/v1/expert-reviews")
    def list_reviews(request: Request, state: str = "pending"):
        tenant, user = owner(request)
        if state not in {"pending", "approved", "rejected", "withdrawn", "all"}:
            raise HTTPException(422, "Unknown review state")
        return {"items": experts.reviews(tenant, user, state)}

    @app.post("/v1/expert-reviews/{review_id}/decision")
    def decide(review_id: str, payload: ReviewDecisionInput, request: Request):
        ctx = auth_context(request)
        # Only a signed-in owner session decides, on this computer too. A runtime
        # token, a Worker's run authority or the Expert itself cannot approve.
        if ctx.auth_mode != "signed_internal_assertion" or "human:confirm" not in ctx.scopes:
            raise HTTPException(403, "Approve or reject from your signed-in xPerfect page")
        tenant, user = owner(request)
        return invoke(experts.decide, tenant, user, review_id, decision=payload.decision,
                      expected_digest=payload.expected_digest, note=payload.note)


class ChannelAddInput(StrictModel):
    kind: str = Field(pattern="^(teams|slack)$")


class ChannelUpdateInput(StrictModel):
    people: list[dict[str, str]] | None = Field(default=None, max_length=500)
    state: str | None = Field(default=None, pattern="^(connected|off)$")


class ChannelSetupInput(StrictModel):
    settings: dict[str, str] = Field(default_factory=dict)


def install_channel_routes(app, channels, principal: Callable[[Request], tuple[Any, str, str]]) -> None:
    """Owner routes for an Expert's channels, and the routes a channel package uses."""

    def invoke(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ExpertError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc
        except CoordinatorScopeError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (CoordinatorConflict, ValidationError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    def owner(request: Request) -> tuple[str, str]:
        ctx, tenant, user = principal(request)
        if ctx.auth_mode == "signed_link" or str(ctx.role or "").lower() == "viewer":
            raise HTTPException(403, "Sign in as the owner to manage channels")
        return tenant, user

    def owned(binding_id: str, request: Request) -> None:
        tenant, user = owner(request)
        invoke(channels._binding, binding_id, tenant, user)

    @app.get("/v1/experts/{expert_id}/channels")
    def list_channels(expert_id: str, request: Request):
        return {"items": invoke(channels.list, *owner(request), expert_id)}

    @app.post("/v1/experts/{expert_id}/channels", status_code=201)
    def add_channel(expert_id: str, payload: ChannelAddInput, request: Request):
        return invoke(channels.add, *owner(request), expert_id, payload.kind)

    @app.patch("/v1/channels/{binding_id}")
    def update_channel(binding_id: str, payload: ChannelUpdateInput, request: Request):
        return invoke(channels.update, *owner(request), binding_id, people=payload.people, state=payload.state)

    @app.post("/v1/channels/{binding_id}/connect")
    def connect(binding_id: str, request: Request):
        return invoke(channels.setup.connect, *owner(request), binding_id)

    @app.get("/v1/channels/{binding_id}/connect")
    def connect_status(binding_id: str, request: Request):
        return invoke(channels.setup.status, *owner(request), binding_id)

    @app.post("/v1/channels/{binding_id}/setup")
    def record_setup(binding_id: str, payload: ChannelSetupInput, request: Request):
        return invoke(channels.record_setup, *owner(request), binding_id, payload.settings)

    # The channel package runs beside the core with the owner's runtime credential.
    @app.get("/v1/channels/{binding_id}/package")
    def package_config(binding_id: str, request: Request):
        owned(binding_id, request)
        return invoke(channels.package_config, binding_id)

    @app.post("/v1/channels/{binding_id}/heartbeat")
    def heartbeat(binding_id: str, request: Request):
        owned(binding_id, request)
        return invoke(channels.heartbeat, binding_id)

    @app.post("/v1/channels/{binding_id}/inbound")
    def inbound(binding_id: str, payload: InboundMessage, request: Request):
        owned(binding_id, request)
        return invoke(channels.inbound, binding_id, payload)

    @app.post("/v1/channels/{binding_id}/deliveries/claim")
    def claim(binding_id: str, request: Request, limit: int = 10):
        owned(binding_id, request)
        return {"items": invoke(channels.claim, binding_id, limit)}

    @app.post("/v1/channels/{binding_id}/deliveries/{delivery_id}")
    def delivered(binding_id: str, delivery_id: str, payload: DeliveryResult, request: Request):
        owned(binding_id, request)
        return invoke(channels.delivered, binding_id, delivery_id, payload)
