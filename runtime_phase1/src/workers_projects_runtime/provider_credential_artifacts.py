"""Fixed native credential artifacts for a fenced, owner-scoped projection.

Paths are relative to the managed account root and private member home respectively.
These declarations grant no access by themselves; the runtime owns the account lease,
member ACL, starting revision, exact process-stop proof and atomic refresh transaction.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CredentialArtifact:
    account_path: str
    member_path: str
    refresh: bool
    environment_key: str = ""
    max_bytes: int = 1_048_576


def credential_artifacts(provider: str, auth_method: str) -> tuple[CredentialArtifact, ...]:
    if auth_method not in {"subscription", "api_key"}:
        raise ValueError("Only native subscription/API-key accounts have projected artifacts")
    if provider in {"codex", "openai"}:
        # API-key setup uses codex login --with-api-key and file credential storage.
        return (CredentialArtifact("codex/auth.json", ".codex/auth.json", True),)
    if provider in {"claude", "anthropic"}:
        if auth_method == "subscription":
            # Linux native secure storage. Never copy .claude.json or settings/MCP/plugins.
            return (CredentialArtifact("claude/.credentials.json", ".claude/.credentials.json", True),)
        return (CredentialArtifact("api-key.json", ".xperfect/credentials/claude-api-key.json", False, "ANTHROPIC_API_KEY", 16_384),)
    if provider in {"grok", "xai"}:
        if auth_method == "subscription":
            return (CredentialArtifact("grok/auth.json", ".grok/auth.json", True),)
        return (CredentialArtifact("api-key.json", ".xperfect/credentials/grok-api-key.json", False, "XAI_API_KEY", 16_384),)
    raise ValueError("Provider has no reviewed native credential artifact contract")
