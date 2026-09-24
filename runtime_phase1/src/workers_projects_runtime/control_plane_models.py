from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr


class CreateProviderAccountRequest(BaseModel):
    provider: Literal["codex", "claude", "openai", "anthropic", "grok", "xai", "custom"]
    label: str = Field(min_length=1, max_length=160)
    auth_method: Literal["subscription", "api_key", "enterprise_route"]
    platform_support: str = Field(min_length=1, max_length=80)
    secret_locator: str = Field(default="native-home://auto", min_length=1, max_length=512)
    make_default: bool = False


class ProviderApiKeyRequest(BaseModel):
    value: SecretStr = Field(min_length=1, max_length=8192)


class ProviderSetupInputRequest(BaseModel):
    value: str


class CreatePendingChangeRequest(BaseModel):
    change_type: Literal[
        "workspace_grant",
        "connection_write",
        "library_enable",
        "library_upgrade",
        "workspace_provider_account",
        "workspace_duplication_reapproval_waiver",
    ]
    target_id: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)


class ConfirmPendingChangeRequest(BaseModel):
    confirmation_token: str = Field(min_length=16, max_length=512)


class WorkspaceCatalogResponse(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


class UpdateWorkspaceRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    favorite: bool | None = None
    tags: list[str] | None = None
    workspace_kind: Literal["named", "ephemeral", "legacy"] | None = None


class DuplicateWorkspaceRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=160)


class SaveWorkspaceTemplateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    lineage_id: str | None = Field(default=None, min_length=1, max_length=80)


class InstantiateWorkspaceTemplateRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=160)


class PublishLibraryManifestRequest(BaseModel):
    manifest: dict[str, Any]


class UpdateLibraryStatusRequest(BaseModel):
    status: Literal["available", "disabled", "removed"]
    reason: str = Field(default="", max_length=1000)


class CreateLibraryProposalRequest(BaseModel):
    manifest: dict[str, Any]


class ReviewLibraryProposalRequest(BaseModel):
    action: Literal["publish", "reject"]
    reason: str = Field(default="", max_length=1000)
