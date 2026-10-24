"""Non-authoritative Library labels and delivery hints in native context carriers."""
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _source_reference(value):
    if value is not None:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "native"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Native Library source must be a credential-free reference")
    return value


class _Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Ref(_Metadata):
    item_id: str = Field(min_length=1, max_length=128)
    version_id: str = Field(min_length=1, max_length=128)


class _Item(_Ref):
    name: str = Field(max_length=1024)
    kind: Literal["knowledge", "note", "skill", "native", "bundle", "approved"]
    version: int | None = Field(default=None, ge=1)
    source_uri: str | None = Field(default=None, max_length=2048)
    state: str | None = Field(default=None, max_length=128)
    status: str | None = Field(default=None, max_length=256)
    _reference = field_validator("source_uri")(_source_reference)


class _Pending(_Metadata):
    item_id: str | None = Field(default=None, max_length=128)
    version_id: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=1024)
    kind: str | None = Field(default=None, max_length=64)
    code: str | None = Field(default=None, max_length=128)
    status: str | None = Field(default=None, max_length=256)
    reason: str | None = Field(default=None, max_length=1024)
    profile: str | None = Field(default=None, max_length=128)
    source_uri: str | None = Field(default=None, max_length=2048)
    state: str | None = Field(default=None, max_length=128)
    _reference = field_validator("source_uri")(_source_reference)


class _Source(_Metadata):
    item_id: str | None = Field(default=None, max_length=128)
    version_id: str | None = Field(default=None, max_length=128)
    name: str = Field(max_length=1024)
    upload_id: str = Field(min_length=1, max_length=128)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    loaded: bool | None = None
    reason: str | None = Field(default=None, max_length=128)
    status: str | None = Field(default=None, max_length=256)
    delivery: Literal["native_file"] | None = None


class _ReferenceMessage(_Metadata):
    id: str = Field(min_length=1, max_length=160)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _Selection(_Metadata):
    refs: list[_Ref] = Field(default_factory=list, max_length=128)
    items: list[_Item] = Field(default_factory=list, max_length=1024)
    pending: list[_Pending] = Field(default_factory=list, max_length=1024)
    upload_ids: list[str] = Field(default_factory=list, max_length=1024)
    upload_revisions: list[str] = Field(default_factory=list, max_length=1024)
    context: list[_Source] | None = Field(default=None, max_length=1024)
    reference_message: _ReferenceMessage | None = None


def canonical_library_selection(value: object) -> dict:
    """Labels never carry source bytes, native configuration or capability authority."""
    selected = _Selection.model_validate(value)
    if any(not value or len(value) > 128 for value in selected.upload_ids):
        raise ValueError("Library upload identities must be bounded non-empty strings")
    if selected.upload_revisions and (len(selected.upload_revisions) != len(selected.upload_ids)
            or any(not value or len(value) > 128 for value in selected.upload_revisions)):
        raise ValueError("Library file versions must match their upload identities")
    return selected.model_dump(exclude_none=True)


def canonical_file_context_metadata(entry: dict) -> dict:
    """Preserve existing Files provenance, not a second admission authority."""
    result = {}
    for key in ("managed_upload_id", "managed_projection_id"):
        if key not in entry:
            continue
        value = entry[key]
        if key == "managed_projection_id" and value is None:
            continue
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError("Managed file identity is invalid")
        result[key] = value
    if "sha256" in entry:
        value = entry["sha256"]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Managed file content hash is invalid")
        result["sha256"] = value
    if "allow_empty" in entry:
        if not isinstance(entry["allow_empty"], bool):
            raise ValueError("Managed file empty-content option is invalid")
        result["allow_empty"] = entry["allow_empty"]
    if "context_delivery" in entry:
        if entry["context_delivery"] != "lookup":
            raise ValueError("File context delivery is invalid")
        result["context_delivery"] = "lookup"
    return result
