"""Explicit native runtime identities shared by dispatch and discovery."""
from dataclasses import dataclass
from typing import Literal


class UnsupportedWorkerProfileError(ValueError):
    """The requested native harness or placement has no registered adapter."""


@dataclass(frozen=True)
class WorkerProfile:
    profile: str
    label: str
    runtime_attribute: str
    native_transport: Literal['cli', 'gateway', 'acp', 'api']
    model_environment: str
    primary: bool = True
    mcp_tool_access_prompt_id: str | None = None

    def runtime_for(self, execution_mode: str) -> str:
        if execution_mode not in {'host', 'docker'}:
            raise UnsupportedWorkerProfileError('Unsupported execution mode')
        return ('host_' if execution_mode == 'host' else '') + self.runtime_attribute


PROFILES = (
    WorkerProfile('direct-openai', 'Direct / OpenAI API', 'direct_openai', 'api', 'WPR_MODEL_DIRECT_OPENAI'),
    WorkerProfile('direct-anthropic', 'Direct / Anthropic API', 'direct_anthropic', 'api', 'WPR_MODEL_DIRECT_ANTHROPIC'),
    WorkerProfile('direct-xai', 'Direct / Grok API', 'direct_xai', 'api', 'WPR_MODEL_DIRECT_XAI'),
    WorkerProfile('codex-cli', 'Codex CLI', 'codex', 'cli', 'WPR_MODEL_CODEX_CLI'),
    WorkerProfile('claude-code', 'Claude Code', 'claude', 'cli', 'WPR_MODEL_CLAUDE_CODE'),
    WorkerProfile('openclaw-general', 'OpenClaw', 'openclaw', 'gateway', 'WPR_MODEL_OPENCLAW_GENERAL'),
    WorkerProfile('openclaw', 'OpenClaw', 'openclaw', 'gateway', 'WPR_MODEL_OPENCLAW_GENERAL', False),
    WorkerProfile('openclaw-codex', 'OpenClaw Codex', 'openclaw', 'gateway', 'WPR_MODEL_OPENCLAW_CODEX', False),
    WorkerProfile('openclaw-claude', 'OpenClaw Claude', 'openclaw', 'gateway', 'WPR_MODEL_OPENCLAW_CLAUDE', False),
    WorkerProfile('openclaw-desktop', 'OpenClaw Desktop', 'openclaw', 'gateway', 'WPR_MODEL_OPENCLAW_DESKTOP', False),
    WorkerProfile('grok-build', 'Grok Build', 'grok', 'acp', 'WPR_MODEL_GROK_BUILD',
                  mcp_tool_access_prompt_id='worker.grok_mcp_tool_access'),
)
_PROFILE_BY_ID = {item.profile: item for item in PROFILES}


def require_worker_profile(profile: str) -> WorkerProfile:
    try:
        return _PROFILE_BY_ID[profile]
    except KeyError:
        raise UnsupportedWorkerProfileError('Unsupported worker profile') from None
