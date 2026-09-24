"""Owner-scoped execution workspace and member admission routes."""
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from .control_plane import PROFILE_ACCOUNT_PROVIDERS, WORKSPACE_ACCOUNT_POLICIES
from .models import WorkerResponse
from .profile_registry import require_worker_profile


class CreateExecutionWorkspace(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['shared'] = 'shared'
    execution_mode: Literal['docker', 'host'] = 'docker'
    file_placement: Literal['common', 'member_private'] = 'common'


class AddWorkspaceMember(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='', max_length=120)
    role: str = Field(default='member', min_length=1, max_length=80)
    profile: str = ''
    effort: str | None = Field(default=None, max_length=64)
    provider_account_policy: Literal['legacy', 'personal_preferred', 'personal_required'] | None = None
    provider_account_id: str | None = Field(default=None, max_length=128)


def _effort_bundle(profile: str, effort: str | None) -> dict[str, object] | None:
    clean_effort = str(effort or '').strip().lower()
    if not clean_effort:
        return None
    if profile == 'codex-cli':
        if clean_effort not in {'none', 'minimal', 'low', 'medium', 'high', 'xhigh'}:
            raise HTTPException(status_code=400, detail='Codex effort must be none, minimal, low, medium, high, or xhigh')
        return {'env': {'WPR_CODEX_CLI_REASONING_EFFORT': clean_effort}}
    if profile == 'claude-code':
        if clean_effort not in {'default', 'low', 'medium', 'high', 'xhigh', 'max'}:
            raise HTTPException(status_code=400, detail='Claude effort must be default, low, medium, high, xhigh, or max')
        if clean_effort == 'default':
            return None
        return {'env': {'WPR_CLAUDE_CODE_EFFORT': clean_effort}}
    if profile == 'grok-build':
        if any(ord(character) < 32 for character in clean_effort):
            raise HTTPException(status_code=400, detail='Invalid native Grok effort')
        return {'env': {'WPR_GROK_REASONING_EFFORT': clean_effort}}
    raise HTTPException(status_code=400, detail='Effort is not supported for this worker profile')


def _provider_selection(service, *, tenant_id: str, owner_id: str, profile: str,
                        policy: str | None, account_id: str | None) -> dict[str, str]:
    requested_policy = str(policy or '').strip().lower()
    requested_account_id = str(account_id or '').strip()
    resolved_policy = requested_policy or ('personal_required' if requested_account_id else 'legacy')
    if resolved_policy not in WORKSPACE_ACCOUNT_POLICIES:
        raise HTTPException(status_code=400, detail='provider_account_policy must be legacy, personal_preferred, or personal_required')
    if resolved_policy == 'legacy':
        if requested_account_id:
            raise HTTPException(status_code=400, detail='Deployment account policy cannot include a personal account id')
        return {'policy': 'legacy'}
    supported = PROFILE_ACCOUNT_PROVIDERS.get(profile, set())
    if not supported:
        raise HTTPException(status_code=409, detail='Personal provider accounts are unavailable for this worker profile')
    control_plane = getattr(service, 'control_plane_store', None)
    if control_plane is None:
        raise HTTPException(status_code=409, detail='Personal provider account selection is unavailable')
    accounts = control_plane.list_provider_accounts(tenant_id=tenant_id, owner_id=owner_id)
    selected = None
    if requested_account_id:
        selected = control_plane.get_provider_account(
            account_id=requested_account_id, tenant_id=tenant_id, owner_id=owner_id)
        if selected is None:
            raise HTTPException(status_code=409, detail={
                'code': 'shared_provider_account_unavailable',
                'message': 'The selected personal account is not available for this user',
            })
        if str(selected.get('provider') or '').strip().lower() not in supported:
            raise HTTPException(status_code=409, detail={
                'code': 'shared_provider_account_mismatch',
                'message': 'The selected personal account does not match this worker profile',
            })
        if str(selected.get('status') or '').strip().lower() != 'ready':
            raise HTTPException(status_code=409, detail={
                'code': 'shared_provider_account_not_ready',
                'message': 'The selected personal account is not ready; reconnect it before adding the member',
            })
    else:
        selected = next((account for account in accounts
                         if str(account.get('provider') or '').strip().lower() in supported
                         and str(account.get('status') or '').strip().lower() == 'ready'
                         and bool(account.get('is_default'))), None)
        if selected is None and resolved_policy == 'personal_required':
            raise HTTPException(status_code=409, detail={
                'code': 'provider_account_required',
                'message': 'Select a ready personal provider account before adding the member.',
            })
    result = {'policy': resolved_policy}
    if selected is not None:
        result['account_id'] = str(selected.get('account_id') or '').strip()
    return result


def install_execution_workspace_routes(app, service, current_principal, require_project, resolve_profile):
    @app.post('/v1/projects/{project_id}/execution-workspaces', status_code=201)
    def create_execution_workspace(project_id: str, payload: CreateExecutionWorkspace, request: Request):
        _, tenant_id, owner_id = current_principal(request)
        require_project(project_id, request)
        workspace = service.store.create_execution_workspace(
            project_id=project_id, tenant_id=tenant_id, owner_id=owner_id,
            mode=payload.mode, execution_mode=payload.execution_mode, file_placement=payload.file_placement)
        return {**workspace, 'members': [], 'runtime_readiness': service.shared_workspace_readiness(workspace)}

    @app.post('/v1/workspaces/{workspace_id}/members', response_model=WorkerResponse, status_code=201)
    def add_workspace_member(workspace_id: str, payload: AddWorkspaceMember, request: Request):
        _, tenant_id, owner_id = current_principal(request)
        workspace = service.store.get_execution_workspace(workspace_id, tenant_id, owner_id)
        if workspace is None:
            raise HTTPException(404, 'Workspace is unavailable for this owner')
        project = require_project(workspace['project_id'], request)
        profile = resolve_profile(project, payload.profile)
        descriptor = require_worker_profile(profile)
        provider_selection = _provider_selection(
            service,
            tenant_id=tenant_id,
            owner_id=owner_id,
            profile=profile,
            policy=payload.provider_account_policy,
            account_id=payload.provider_account_id,
        )
        bootstrap_bundle: dict[str, object] = {
            'provider_account': provider_selection,
        }
        effort_bundle = _effort_bundle(profile, payload.effort)
        if effort_bundle:
            bootstrap_bundle.update(effort_bundle)
        worker = service.create_worker(project_id=workspace['project_id'], tenant_id=tenant_id,
                                       owner_id=owner_id, name=payload.name.strip() or descriptor.label,
                                       role=payload.role, profile=profile, backend='',
                                       execution_mode=workspace['execution_mode'], workspace_id=workspace_id,
                                       workspace_kind='named', bootstrap_bundle=bootstrap_bundle,
                                       start_synchronously=False)
        return WorkerResponse(**worker)
