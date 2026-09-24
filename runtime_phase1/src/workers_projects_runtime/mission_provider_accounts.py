from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Condition, Event, Lock, Thread
from typing import Callable, Iterator

from .auth import multi_user_security_enabled
from .control_plane import ControlPlaneConflict, ControlPlaneError, ControlPlaneStore, PROFILE_ACCOUNT_PROVIDERS, ProviderProjectionPending
from .openclaw_runtime import HostCapacityError, RuntimeErrorBase
from .provider_accounts import ProviderAccountHomeManager
from .account_native_launch import GuardedAccountLauncher


logger = logging.getLogger(__name__)


class ProviderAccountBusyError(HostCapacityError):
    """Another live run holds this account; native launch has not started."""

    def __init__(self) -> None:
        super().__init__(
            "The selected AI account is already in use. This work will wait for the account to be free.",
            capacity_class="provider_account",
        )


def other_live_provider_lease(
    store: ControlPlaneStore, *, account_id: str, tenant_id: str,
    owner_id: str, run_id: str,
) -> dict | None:
    """A live foreign lease is queue pressure; expired cleanup stays recovery."""
    lease = store.active_provider_account_lease(account_id)
    if (lease and str(lease.get("tenant_id") or "") == tenant_id
            and str(lease.get("owner_id") or "") == owner_id
            and str(lease.get("run_id") or "") != run_id
            and float(lease.get("expires_at") or 0) > time.time()):
        return lease
    return None

_POLICY_ALIASES = {
    "legacy": "legacy",
    "personal_optional": "personal_preferred",
    "personal_preferred": "personal_preferred",
    "personal_required": "personal_required",
}
_PROFILE_PROVIDERS = PROFILE_ACCOUNT_PROVIDERS


def provider_account_capabilities() -> dict[str, list[str]]:
    """Advertise the same profile/account compatibility used by native binding."""
    return {profile: sorted(providers) for profile, providers in _PROFILE_PROVIDERS.items()}
_EXPECTED_HOME_KEYS = {
    "grok-build": frozenset({"GROK_AUTH_PATH"}),
    "codex-cli": frozenset({"CODEX_HOME"}),
    "claude-code": frozenset(
        {"CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR"}
    ),
}
_CONTAINER_ACCOUNT_MOUNT = "/workspace/.provider-account"
_DEFAULT_CONTAINER_WORKSPACE_HOME = "/workspace/.wpr-home"
_CODEX_CONFLICTING_ENV = {
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_REVERSE_PROXY",
    "PORTKEY_API_KEY",
    "PORTKEY_BASE_URL",
    "PORTKEY_PROVIDER",
    "PORTKEY_VIRTUAL_KEY",
    "PORTKEY_CONFIG",
}
_CLAUDE_CONFLICTING_ENV = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_CONFIG_DIR",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "CLAUDE_CODE_USE_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
}
_PROVIDER_CLEANUP_ATTEMPTS = 2
_PROVIDER_CLEANUP_RETRY_DELAY_SECONDS = 0.2


def _usable_provider_value(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value or value == "user_provided" or (value.startswith("${") and value.endswith("}")):
        return ""
    return value


def deployment_provider_readiness(profile: str) -> tuple[str, str]:
    """Return the effective deployment-managed route state without exposing credentials."""

    if not multi_user_security_enabled():
        return "deployment_managed", ""
    normalized = str(profile or "").strip().lower()
    if normalized in {"codex-cli", "openclaw-codex"}:
        base_url = (
            _usable_provider_value("WPR_CODEX_CLI_BASE_URL")
            or _usable_provider_value("OPENAI_BASE_URL")
            or _usable_provider_value("OPENAI_API_BASE")
            or _usable_provider_value("OPENAI_REVERSE_PROXY")
            or _usable_provider_value("PORTKEY_BASE_URL")
        )
        key_name = (
            _usable_provider_value("WPR_CODEX_CLI_ENV_KEY")
        )
        if not key_name:
            key_name = (
                "PORTKEY_API_KEY"
                if _usable_provider_value("PORTKEY_BASE_URL")
                and not any(
                    _usable_provider_value(name)
                    for name in (
                        "WPR_CODEX_CLI_BASE_URL",
                        "OPENAI_BASE_URL",
                        "OPENAI_API_BASE",
                        "OPENAI_REVERSE_PROXY",
                    )
                )
                else "OPENAI_API_KEY"
            )
        if key_name not in {"OPENAI_API_KEY", "PORTKEY_API_KEY"}:
            return "action_required", "deployment_provider_unavailable"
        credential = _usable_provider_value(key_name)
        disabled = any(
            _usable_provider_value(name).lower() in {"1", "true", "yes", "on", "enabled"}
            for name in ("WPR_CODEX_CLI_DISABLE_CUSTOM_PROVIDER",)
        )
        # The native OpenAI route needs only OPENAI_API_KEY; custom-compatible and
        # Portkey routes require the selected endpoint plus its selected credential.
        ready = bool(credential) and not disabled and (
            bool(base_url) or key_name == "OPENAI_API_KEY"
        )
    elif normalized in {"openclaw", "openclaw-general"}:
        base_url = (
            _usable_provider_value("WPR_OPENCLAW_BASE_URL")
            or _usable_provider_value("OPENAI_BASE_URL")
            or _usable_provider_value("OPENAI_API_BASE")
            or _usable_provider_value("OPENAI_REVERSE_PROXY")
            or _usable_provider_value("PORTKEY_BASE_URL")
        )
        key_name = _usable_provider_value("WPR_OPENCLAW_ENV_KEY")
        if not key_name:
            key_name = (
                "PORTKEY_API_KEY"
                if _usable_provider_value("PORTKEY_BASE_URL")
                and not any(
                    _usable_provider_value(name)
                    for name in (
                        "WPR_OPENCLAW_BASE_URL",
                        "OPENAI_BASE_URL",
                        "OPENAI_API_BASE",
                        "OPENAI_REVERSE_PROXY",
                    )
                )
                else "OPENAI_API_KEY"
            )
        if key_name not in {"OPENAI_API_KEY", "PORTKEY_API_KEY"}:
            return "action_required", "deployment_provider_unavailable"
        credential = _usable_provider_value(key_name)
        disabled = _usable_provider_value("WPR_OPENCLAW_DISABLE_CUSTOM_PROVIDER").lower() in {
            "1", "true", "yes", "on", "enabled"
        }
        ready = bool(credential) and not disabled and (
            bool(base_url) or key_name == "OPENAI_API_KEY"
        )
    elif normalized == "grok-build":
        ready = bool(_usable_provider_value("XAI_API_KEY"))
    elif normalized in {"claude-code", "openclaw-claude"}:
        use_bedrock = _usable_provider_value("CLAUDE_CODE_USE_BEDROCK").lower() in {
            "1", "true", "yes", "on", "enabled"
        }
        use_api_key = _usable_provider_value("WPR_CLAUDE_CODE_USE_API_KEY").lower() in {
            "1", "true", "yes", "on", "enabled"
        }
        if use_bedrock:
            ready = bool(_usable_provider_value("AWS_REGION")) and bool(
                _usable_provider_value("AWS_BEARER_TOKEN_BEDROCK")
                or (
                    _usable_provider_value("AWS_ACCESS_KEY_ID")
                    and _usable_provider_value("AWS_SECRET_ACCESS_KEY")
                )
            )
        elif use_api_key:
            ready = bool(_usable_provider_value("ANTHROPIC_API_KEY"))
        else:
            ready = bool(_usable_provider_value("CLAUDE_CODE_OAUTH_TOKEN"))
    else:
        ready = False
    return (
        ("deployment_managed", "")
        if ready
        else ("action_required", "deployment_provider_unavailable")
    )


@dataclass(frozen=True)
class MissionProviderAccountSelection:
    policy: str
    account_id: str

    @property
    def requires_binding(self) -> bool:
        return bool(self.account_id)


def _bootstrap_bundle(worker: dict) -> dict[str, object]:
    raw = worker.get("bootstrap_bundle_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    candidate = worker.get("bootstrap_bundle")
    return candidate if isinstance(candidate, dict) else {}


def mission_provider_account_selection(
    worker: dict,
) -> MissionProviderAccountSelection | None:
    """Read the additive provider account selection contract.

    An absent selection is the historical compatibility path. An explicitly selected account
    defaults to fail-closed ``personal_required`` behavior so a typo cannot silently spend a
    process-global account.
    """

    bundle = _bootstrap_bundle(worker)
    if "provider_account" not in bundle:
        return None
    raw = bundle.get("provider_account")
    if not isinstance(raw, dict):
        raise RuntimeErrorBase("Mission provider account selection must be a structured object")
    account_id = str(raw.get("account_id") or "").strip()
    requested_policy = str(
        raw.get("policy") or ("personal_required" if account_id else "legacy")
    ).strip().lower()
    policy = _POLICY_ALIASES.get(requested_policy, "")
    if not policy:
        raise RuntimeErrorBase(
            "Mission provider account policy must be legacy, personal_preferred, or personal_required"
        )
    if policy == "legacy":
        if account_id:
            raise RuntimeErrorBase(
                "Legacy provider account policy cannot include a personal account selection"
            )
        return None
    if not account_id:
        if policy == "personal_required":
            raise RuntimeErrorBase("This mission requires a provider account selection")
        return None
    return MissionProviderAccountSelection(policy=policy, account_id=account_id)


def native_current_account_allowed(worker: dict) -> bool:
    """A named required account never imports the current OS user's credentials."""
    if worker.get("_glasshive_provider_account_bound") or worker.get("_glasshive_inference_broker_bound"):
        return False
    bundle = _bootstrap_bundle(worker)
    # Preparation can run before mission leasing or while a conversation is being
    # converted to a mission. An explicit account still excludes ambient authority.
    selection = mission_provider_account_selection({**worker, "bootstrap_bundle_json": {**bundle, "run_mode": "mission"}})
    return selection is None or (
        selection.policy == "personal_preferred"
        and bool(worker.get("_glasshive_provider_account_preferred_fallback"))
    )


def apply_bound_provider_account_environment(
    worker: dict,
    env: dict[str, str],
    *,
    runtime_name: str,
) -> dict[str, str]:
    """Apply only a trusted binding projected by ``MissionProviderAccountBinder``.

    Direct sub-runtime use with an explicit personal selection fails closed; callers cannot place
    an arbitrary home in the public bootstrap bundle and bypass owner/account validation.
    """

    from .current_native_account import MARKER, project_current_environment
    if MARKER in worker:
        project_current_environment(worker, env, runtime_name)
        return env
    selection = mission_provider_account_selection(worker)
    if selection is None:
        bundle = _bootstrap_bundle(worker)
        explicit = mission_provider_account_selection({**worker, "bootstrap_bundle_json": {**bundle, "run_mode": "mission"}})
        if explicit is not None:
            raise RuntimeErrorBase("An explicitly selected account requires the validated account execution route")
        return env
    if worker.get("_glasshive_inference_broker_bound"):
        if runtime_name != "codex-cli":
            raise RuntimeErrorBase(
                "The OpenAI inference broker can be projected only into Codex workers"
            )
        return env
    if selection.policy == "personal_preferred" and worker.get(
        "_glasshive_provider_account_preferred_fallback"
    ):
        return env
    if not worker.get("_glasshive_provider_account_bound"):
        raise RuntimeErrorBase(
            "This explicitly selected account was not validated by xPerfect"
        )
    expected_keys = _EXPECTED_HOME_KEYS.get(runtime_name)
    if runtime_name == "claude-code" and str(worker.get("execution_mode") or "host") == "docker":
        expected_keys = frozenset({"CLAUDE_CONFIG_DIR"}) if worker.get("_glasshive_provider_account_projected") else frozenset({"CLAUDE_SECURESTORAGE_CONFIG_DIR"})
    if expected_keys is None:
        raise RuntimeErrorBase(
            "Personal provider accounts are supported only for registered native mission workers"
        )
    raw_environment = worker.get("_glasshive_provider_account_env")
    if not isinstance(raw_environment, dict):
        raise RuntimeErrorBase("Mission provider account binding is missing its private provider home")
    keys = {str(key) for key in raw_environment}
    if keys != expected_keys:
        raise RuntimeErrorBase("Mission provider account binding contains an invalid provider home")
    for expected_key in expected_keys:
        account_home = str(raw_environment.get(expected_key) or "").strip()
        if not account_home or not Path(account_home).is_absolute():
            raise RuntimeErrorBase("Mission provider account binding contains an invalid provider home")

    conflicting = (
        _CODEX_CONFLICTING_ENV
        if runtime_name == "codex-cli"
        else {"XAI_API_KEY", "GROK_AUTH_PATH"} if runtime_name == "grok-build"
        else _CLAUDE_CONFLICTING_ENV
    )
    for key in conflicting:
        env.pop(key, None)
    env.pop("GLASSHIVE_NATIVE_API_KEY_FILE", None)
    env.pop("GLASSHIVE_NATIVE_API_KEY_PROVIDER", None)
    env.update({str(key): str(value) for key, value in raw_environment.items()})
    from .native_api_keys import project_key
    project_key(worker, env, runtime_name)
    return env


class MissionProviderAccountBinder:
    """Owner-scoped native account home and durable mission lease projection."""

    def __init__(
        self,
        *,
        db_path: str | None,
        home_root: Path,
        owner_root_resolver: Callable[[str, str], Path] | None = None,
        native_launcher: GuardedAccountLauncher | None = None,
    ) -> None:
        self.store = ControlPlaneStore(db_path) if db_path else None
        self.home_root = Path(home_root)
        self.owner_root_resolver = owner_root_resolver
        self.native_launcher = native_launcher
        self._homes: ProviderAccountHomeManager | None = None
        self._active_binding_condition = Condition()
        self._active_bindings: dict[str, dict[str, object]] = {}
        self._allowed_ai_start_checker: Callable[[dict, dict], dict | None] | None = None

    def configure_allowed_ai_start(
        self, checker: Callable[[dict, dict], dict | None] | None
    ) -> None:
        """Install the owner runtime's final Allowed AI start check."""

        self._allowed_ai_start_checker = checker

    def native_connection_receipt(
        self,
        worker: dict,
        *,
        runtime_name: str,
        run_id: str,
        route_kind: str | None = None,
        connection_id: str | None = None,
        native_model: str | None = None,
    ) -> dict[str, object]:
        """Return a typed receipt from the active binder route, without secrets."""

        worker_id = str(worker.get("worker_id") or "").strip()
        clean_run = str(run_id or "").strip()
        with self._active_binding_condition:
            active = self._active_bindings.get(worker_id)
            if active is not None:
                if str(active.get("run_id") or "") != clean_run:
                    raise RuntimeErrorBase("The native provider route is bound to another run")
                if str(active.get("route_kind") or "") == "native" and (
                    (connection_id is not None and str(connection_id).strip() != str(active.get("account_id") or "").strip())
                    or (route_kind is not None and str(route_kind).strip() != "native")
                ):
                    raise RuntimeErrorBase("The native provider receipt conflicts with its active account binding")
                connection_id = str(
                    connection_id if connection_id is not None else active.get("account_id") or ""
                ).strip()
                route_kind = str(
                    route_kind if route_kind is not None else active.get("route_kind") or ""
                ).strip()
            else:
                route_kind = str(route_kind or "legacy").strip()
                connection_id = str(connection_id or "").strip()
        return {
            "protocol": "glasshive.native_connection_receipt.v1",
            "worker_id": worker_id,
            "run_id": clean_run,
            "profile": str(worker.get("profile") or "").strip(),
            "runtime": str(runtime_name or "").strip(),
            "route_kind": route_kind or "legacy",
            "connection_id": connection_id,
            "native_model": str(native_model or worker.get("model") or "").strip(),
        }

    def authorize_native_start(
        self, worker: dict, *, receipt: dict[str, object]
    ) -> dict | None:
        """Run the final policy/start fence for one binder-produced receipt."""

        checker = self._allowed_ai_start_checker
        bundle = _bootstrap_bundle(worker)
        foreground_selected = (
            str(bundle.get("run_mode") or "").strip().lower() == "conversation"
            and mission_provider_account_selection(worker) is not None
        )
        admission = worker.get("_allowed_ai_admission")
        if not isinstance(admission, dict):
            admission = worker.get("allowed_ai_admission")
        if foreground_selected and (not callable(checker) or not isinstance(admission, dict) or not admission):
            raise RuntimeErrorBase(
                "Selected foreground provider account is missing its native start admission"
            )
        if not callable(checker):
            return None
        if not isinstance(admission, dict):
            # Interactive setup and legacy rows without a durable run snapshot
            # do not represent a native work start. Durable queued runs receive
            # their snapshot in the service admission path.
            return None
        result = checker(worker, dict(receipt))
        worker["_glasshive_allowed_ai_connection_receipt"] = dict(receipt)
        return result

    @property
    def homes(self) -> ProviderAccountHomeManager:
        with self._active_binding_condition:
            if self._homes is None:
                self._homes = ProviderAccountHomeManager(self.home_root, owner_root_resolver=self.owner_root_resolver, native_launcher=self.native_launcher)
            return self._homes

    def recover_projection(self, record: dict, *, projection_factory: Callable) -> None:
        from .provider_projection_binding import recover_projected_account
        recover_projected_account(self, record, factory=projection_factory)

    def selected_account_record(
        self,
        worker: dict,
        selection: MissionProviderAccountSelection,
    ) -> dict | None:
        """Return the internal owner-scoped account record for route selection only.

        Secret locators are intentionally not used by the broker consumer. The method exists so
        ``ProfiledWorkerRuntime`` can distinguish a provider-native subscription home from a
        LibreChat-held API key or enterprise route before either credential path is projected.
        """

        if self.store is None:
            return None
        tenant_id = str(worker.get("tenant_id") or "local").strip() or "local"
        owner_id = str(worker.get("owner_id") or "").strip()
        if not owner_id:
            raise RuntimeErrorBase(
                "Mission provider account binding requires an authenticated owner"
            )
        return self.store.get_provider_account_record(
            account_id=selection.account_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )

    def update_selected_account_status(
        self,
        worker: dict,
        selection: MissionProviderAccountSelection,
        *,
        status: str,
        reconnect_reason: str = "",
    ) -> None:
        if self.store is None:
            return
        self.store.update_provider_account_status(
            account_id=selection.account_id,
            tenant_id=str(worker.get("tenant_id") or "local").strip() or "local",
            owner_id=str(worker.get("owner_id") or "").strip(),
            status=status,
            reconnect_reason=reconnect_reason,
        )

    def _reserve_active_route(
        self,
        worker: dict,
        *,
        runtime_name: str,
        run_id: str,
        account_id: str,
        route_kind: str,
    ) -> object:
        worker_id = str(worker.get("worker_id") or "").strip()
        if not worker_id:
            raise RuntimeErrorBase("Provider account routes require an exact worker")
        token = object()
        with self._active_binding_condition:
            if worker_id in self._active_bindings:
                raise RuntimeErrorBase(
                    "This worker already has an active provider account route"
                )
            self._active_bindings[worker_id] = {
                "token": token,
                "worker": dict(worker),
                "run_id": str(run_id),
                "runtime_name": str(runtime_name),
                "account_id": str(account_id),
                "tenant_id": str(worker.get("tenant_id") or "local").strip()
                or "local",
                "owner_id": str(worker.get("owner_id") or "").strip(),
                "route_kind": str(route_kind),
                "lease_id": "",
                "readers": 0,
                "ready": False,
                "closing": False,
            }
        return token

    def _update_active_route(
        self,
        worker_id: str,
        token: object,
        *,
        worker: dict | None = None,
        lease_id: str | None = None,
    ) -> None:
        with self._active_binding_condition:
            active = self._active_bindings.get(worker_id)
            if active is None or active.get("token") is not token:
                raise RuntimeErrorBase("The provider account route is no longer active")
            if worker is not None:
                active["worker"] = dict(worker)
            if lease_id is not None:
                active["lease_id"] = str(lease_id)

    def mark_active_route_ready(
        self,
        worker: dict,
        *,
        runtime_name: str,
        run_id: str,
    ) -> None:
        """Publish a route only after the mission owns its initial ready substrate."""

        selection = mission_provider_account_selection(worker)
        worker_id = str(worker.get("worker_id") or "").strip()
        with self._active_binding_condition:
            active = self._active_bindings.get(worker_id)
            if (
                active is None
                or bool(active.get("closing"))
                or str(active.get("run_id") or "") != str(run_id)
                or str(active.get("runtime_name") or "") != str(runtime_name)
                or str(active.get("account_id") or "")
                != str(selection.account_id if selection is not None else "")
                or str(active.get("tenant_id") or "")
                != (str(worker.get("tenant_id") or "local").strip() or "local")
                or str(active.get("owner_id") or "")
                != str(worker.get("owner_id") or "").strip()
            ):
                raise RuntimeErrorBase(
                    "The provider account route is no longer available for this worker run"
                )
            active["worker"] = dict(worker)
            active["ready"] = True
            self._active_binding_condition.notify_all()

    def _begin_close_active_route(self, worker_id: str, token: object) -> None:
        with self._active_binding_condition:
            active = self._active_bindings.get(worker_id)
            if active is None or active.get("token") is not token:
                return
            active["closing"] = True
            while int(active.get("readers") or 0) > 0:
                self._active_binding_condition.wait()

    def _finalize_close_active_route(self, worker_id: str, token: object) -> None:
        with self._active_binding_condition:
            active = self._active_bindings.get(worker_id)
            if active is None or active.get("token") is not token:
                return
            if self._active_bindings.get(worker_id) is active:
                self._active_bindings.pop(worker_id, None)
            self._active_binding_condition.notify_all()

    def _close_active_route(self, worker_id: str, token: object) -> None:
        self._begin_close_active_route(worker_id, token)
        self._finalize_close_active_route(worker_id, token)

    @contextmanager
    def bind_unbound_route(
        self,
        worker: dict,
        *,
        runtime_name: str,
        run_id: str,
        account_id: str,
    ) -> Iterator[dict]:
        """Register the exact selected-account run that uses no native home mount."""

        worker_id = str(worker.get("worker_id") or "").strip()
        token = self._reserve_active_route(
            worker,
            runtime_name=runtime_name,
            run_id=run_id,
            account_id=account_id,
            route_kind="unbound",
        )
        try:
            yield worker
        finally:
            self._close_active_route(worker_id, token)

    @contextmanager
    def project_active_route(
        self,
        worker: dict,
        *,
        runtime_name: str,
        run_id: str,
    ) -> Iterator[dict]:
        """Borrow the exact active provider route for an active worker action.

        Mission binding is deliberately ephemeral and never persisted on the worker row. Desktop
        actions arrive through a separate request while the mission is running, so they must prove
        the exact owner/account/worker/run lease before comparing the live container mount. This
        method never acquires a second lease or reconstructs a home from email or mutable UI
        metadata. The mission cleanup waits for every borrowed action to release its projection.
        """

        selection = mission_provider_account_selection(worker)
        if selection is None:
            yield worker
            return
        clean_runtime = str(runtime_name or "").strip()
        clean_run_id = str(run_id or "").strip()
        if clean_runtime not in _PROFILE_PROVIDERS or not clean_run_id:
            raise RuntimeErrorBase(
                "Desktop actions require the exact active provider account lease"
            )
        tenant_id = str(worker.get("tenant_id") or "local").strip() or "local"
        owner_id = str(worker.get("owner_id") or "").strip()
        worker_id = str(worker.get("worker_id") or "").strip()
        if not owner_id or not worker_id:
            raise RuntimeErrorBase(
                "Desktop actions require an authenticated provider account owner"
            )
        with self._active_binding_condition:
            current = self._active_bindings.get(worker_id)
            route_kind = str((current or {}).get("route_kind") or "")
            expected_token = (current or {}).get("token")
            expected_lease_id = str((current or {}).get("lease_id") or "")
        lease: dict | None = None
        if route_kind == "native":
            if self.store is None:
                raise RuntimeErrorBase(
                    "Desktop actions cannot verify the active provider account lease"
                )
            lease = self.store.active_provider_lease(
                selection.account_id,
                f"{clean_runtime}:mission",
            )
            if lease is None:
                raise RuntimeErrorBase(
                    "Desktop actions require the exact active provider account lease"
                )
            if any(
                str(lease.get(key) or "").strip() != expected
                for key, expected in (
                    ("tenant_id", tenant_id),
                    ("owner_id", owner_id),
                    ("worker_id", worker_id),
                    ("run_id", clean_run_id),
                )
            ):
                raise RuntimeErrorBase(
                    "The active provider account lease does not own this worker run"
                )
            if (
                not expected_lease_id
                or str(lease.get("lease_id") or "") != expected_lease_id
            ):
                raise RuntimeErrorBase(
                    "Desktop actions require the exact active provider account lease"
                )
        with self._active_binding_condition:
            active = self._active_bindings.get(worker_id)
            if (
                active is None
                or active.get("token") is not expected_token
                or bool(active.get("closing"))
                or not bool(active.get("ready"))
                or str(active.get("run_id") or "") != clean_run_id
                or str(active.get("runtime_name") or "") != clean_runtime
                or str(active.get("account_id") or "") != selection.account_id
                or str(active.get("tenant_id") or "") != tenant_id
                or str(active.get("owner_id") or "") != owner_id
                or str(active.get("route_kind") or "") not in {"native", "unbound"}
                or (
                    str(active.get("route_kind") or "") == "native"
                    and str(active.get("lease_id") or "") != expected_lease_id
                )
            ):
                raise RuntimeErrorBase(
                    "Desktop actions require the ready provider route for this worker run"
                )
            active["readers"] = int(active.get("readers") or 0) + 1
            bound_worker = dict(active["worker"])  # type: ignore[arg-type]
        try:
            yield bound_worker
        finally:
            with self._active_binding_condition:
                active = self._active_bindings.get(worker_id)
                if active is not None and active.get("token") is expected_token:
                    active["readers"] = max(0, int(active.get("readers") or 0) - 1)
                    self._active_binding_condition.notify_all()

    # Backward-compatible internal name for callers/tests that specifically exercise native binds.
    project_active_binding = project_active_route

    @staticmethod
    def _lease_ttl_seconds(timeout_sec: float | None) -> int:
        # Leases are heartbeated for the full mission, so the expiry is a crash-detection
        # window rather than the mission duration. Keeping it short prevents an API crash
        # from locking a user's personal subscription for the rest of the day.
        default_ttl = 180
        configured = str(
            os.environ.get("GLASSHIVE_PROVIDER_ACCOUNT_LEASE_TTL_SECONDS") or ""
        ).strip()
        if configured:
            try:
                requested = int(configured)
            except ValueError:
                requested = default_ttl
        else:
            requested = default_ttl
        return max(15, min(requested, 60 * 60))

    @staticmethod
    def _preferred_fallback(worker: dict, runtime_name: str) -> dict:
        readiness, _status = deployment_provider_readiness(runtime_name)
        if readiness != "deployment_managed":
            raise RuntimeErrorBase(
                "Work AI is not set up for this workspace. Reconnect the personal account or "
                "ask an administrator to finish provider setup."
            )
        return {
            **worker,
            "_glasshive_provider_account_preferred_fallback": True,
        }

    @staticmethod
    def _lease_heartbeat_interval(ttl_seconds: int) -> float:
        configured = str(
            os.environ.get("GLASSHIVE_PROVIDER_ACCOUNT_LEASE_HEARTBEAT_SECONDS")
            or ""
        ).strip()
        if configured:
            try:
                return max(0.05, min(float(configured), max(1.0, ttl_seconds / 3)))
            except ValueError:
                pass
        return max(1.0, min(60.0, ttl_seconds / 3))

    def _start_lease_heartbeat(
        self,
        *,
        lease_id: str,
        tenant_id: str,
        owner_id: str,
        ttl_seconds: int,
        worker_id: str,
        runtime_name: str,
        account_id: str,
        on_lease_lost: Callable[[], None] | None = None,
    ) -> tuple[Event, Thread, Event]:
        stop = Event()
        lease_lost = Event()

        def heartbeat() -> None:
            interval = self._lease_heartbeat_interval(ttl_seconds)
            while not stop.wait(interval):
                try:
                    assert self.store is not None
                    self.store.heartbeat_provider_lease(
                        lease_id=lease_id,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        ttl_seconds=ttl_seconds,
                    )
                except (ControlPlaneError, OSError, sqlite3.OperationalError):
                    if stop.is_set():
                        return
                    lease_lost.set()
                    logger.exception(
                        "Failed to renew provider account mission lease",
                        extra={"worker_id": worker_id, "runtime": runtime_name},
                    )
                    try:
                        assert self.store is not None
                        self.store.update_provider_account_status(
                            account_id=account_id,
                            tenant_id=tenant_id,
                            owner_id=owner_id,
                            status="action_required",
                            reconnect_reason="Mission lease renewal failed; reconnect after the worker is safely stopped",
                        )
                    except (ControlPlaneError, OSError, sqlite3.OperationalError):
                        logger.exception(
                            "Failed to quarantine provider account after lease renewal loss",
                            extra={"worker_id": worker_id, "runtime": runtime_name},
                        )
                    if on_lease_lost is not None:
                        try:
                            on_lease_lost()
                        except Exception:
                            logger.exception(
                                "Failed to stop provider-bound worker after lease renewal loss",
                                extra={"worker_id": worker_id, "runtime": runtime_name},
                            )
                    return

        thread = Thread(
            target=heartbeat,
            name=f"glasshive-provider-lease-{worker_id[:24]}",
            daemon=True,
        )
        thread.start()
        return stop, thread, lease_lost

    @contextmanager
    def bind(
        self,
        worker: dict,
        *,
        runtime_name: str,
        run_id: str,
        timeout_sec: float | None,
        lease_purpose: str = "mission",
        allow_preferred_fallback: bool = True,
        release_binding: Callable[[dict], None] | None = None,
        abort_binding: Callable[[dict], None] | None = None,
        reconcile_binding: Callable[[Path], None] | None = None,
        projection_factory: Callable | None = None,
        attempt_id: str = "",
    ) -> Iterator[dict]:
        if lease_purpose not in {"mission", "interactive"}:
            raise RuntimeErrorBase("Provider account lease purpose is invalid")
        # Durable recovery precedes EVERY selected, preferred and unbound branch.
        selection = mission_provider_account_selection(worker)
        if self.store is not None:
            worker_id = str(worker.get("worker_id") or "").strip()
            pending_worker = self.store.pending_provider_projections(worker_id=worker_id) if worker_id else []
            pending_account = self.store.pending_provider_projections(account_id=selection.account_id) if selection else []
            if pending_worker:
                raise ProviderProjectionPending("Native credential recovery must finish before using this worker or account")
            if pending_account:
                if other_live_provider_lease(
                    self.store, account_id=selection.account_id,
                    tenant_id=str(worker.get("tenant_id") or "local"),
                    owner_id=str(worker.get("owner_id") or ""), run_id=run_id,
                ):
                    raise ProviderAccountBusyError()
                raise ProviderProjectionPending("Native credential recovery must finish before using this worker or account")
        if selection is not None and self.store is not None:
            from .native_api_keys import enabled, native_key_account
            selected_account = self.selected_account_record(worker, selection)
            if selected_account is not None and native_key_account(selected_account):
                if not enabled():
                    raise RuntimeErrorBase("Native API keys require the validated native account route")
                self.homes.require_native_launcher()
        if selection is not None and self.store is not None:
            from .current_native_account import is_current_account
            account = self.selected_account_record(worker, selection)
            if account is not None and is_current_account(account):
                from .current_native_binding import bind_current_account
                with bind_current_account(self, worker, account=account, selection=selection, runtime_name=runtime_name,
                        run_id=run_id, timeout_sec=timeout_sec, lease_purpose=lease_purpose, abort_binding=abort_binding) as bound:
                    yield bound
                return
        if projection_factory is not None:
            from .provider_projection_binding import bind_projected_account
            with bind_projected_account(self, worker, runtime_name=runtime_name, run_id=run_id, attempt_id=attempt_id, timeout_sec=timeout_sec, lease_purpose=lease_purpose, factory=projection_factory) as bound:
                yield bound
            return
        selection = mission_provider_account_selection(worker)
        if selection is None:
            yield worker
            return
        execution_mode = str(worker.get("execution_mode") or "docker").strip().lower()
        security_mode = str(
            os.environ.get("GLASSHIVE_SECURITY_MODE") or ""
        ).strip().lower()
        isolation_mode = str(
            os.environ.get("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION") or ""
        ).strip().lower()
        isolated_container = (
            execution_mode == "docker"
            and isolation_mode == "per_worker_container"
        )
        if isolated_container and (release_binding is None or reconcile_binding is None):
            raise RuntimeErrorBase(
                "The reviewed provider-account container substrate cannot prove credential mount reconciliation"
            )
        if security_mode == "multi_user" and not isolated_container:
            raise RuntimeErrorBase(
                "Personal subscription workers are disabled in multi-user deployments until "
                "GlassHive can place each account and worker behind a dedicated OS or container boundary"
            )
        preferred = (
            selection.policy == "personal_preferred"
            and allow_preferred_fallback
        )
        if self.store is None:
            if preferred:
                yield self._preferred_fallback(worker, runtime_name)
                return
            raise RuntimeErrorBase(
                "Mission provider accounts are unavailable because the control-plane store is not configured"
            )
        if runtime_name not in _PROFILE_PROVIDERS:
            if preferred:
                yield self._preferred_fallback(worker, runtime_name)
                return
            raise RuntimeErrorBase(
                "Personal provider accounts are supported only for registered native mission workers"
            )
        if execution_mode not in {"host", "docker"} or (
            execution_mode == "docker" and not isolated_container
        ):
            if preferred:
                yield self._preferred_fallback(worker, runtime_name)
                return
            raise RuntimeErrorBase(
                "Personal provider account missions require a host-native worker or the reviewed per-worker container substrate"
            )
        tenant_id = str(worker.get("tenant_id") or "local").strip() or "local"
        owner_id = str(worker.get("owner_id") or "").strip()
        worker_id = str(worker.get("worker_id") or "").strip()
        if not owner_id or not worker_id:
            raise RuntimeErrorBase(
                "Mission provider account binding requires an authenticated owner and worker"
            )
        account = self.store.get_provider_account_record(
            account_id=selection.account_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if account is None:
            if preferred:
                yield self._preferred_fallback(worker, runtime_name)
                return
            raise RuntimeErrorBase("Selected provider account is not available for this user")
        provider = str(account.get("provider") or "").strip().lower()
        if provider not in _PROFILE_PROVIDERS[runtime_name]:
            raise RuntimeErrorBase(
                "Selected provider account does not match this worker profile"
            )
        if self.store.pending_provider_projections(account_id=selection.account_id):
            raise ProviderProjectionPending("Native credential recovery must finish before using this account")
        if str(account.get("status") or "").strip().lower() != "ready":
            if preferred:
                yield self._preferred_fallback(worker, runtime_name)
                return
            raise RuntimeErrorBase(
                "Selected provider account is not ready; reconnect or verify it before running"
            )
        route_token = self._reserve_active_route(
            worker,
            runtime_name=runtime_name,
            run_id=run_id,
            account_id=selection.account_id,
            route_kind="native",
        )
        try:
            from .native_api_keys import enabled, native_key_account
            native_key = native_key_account(account)
            if native_key and not enabled():
                raise ControlPlaneError("Native API keys require the validated native account route")
            homes = self.homes
            if native_key:
                homes.require_native_launcher()
            homes.require_supported_route(
                provider=provider,
                auth_method=str(account.get("auth_method") or ""),
                execution_mode=execution_mode,
                platform_name=sys.platform,
                hosted_consumer_auth_enabled=str(
                    os.environ.get("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH") or ""
                ).strip().lower()
                in {"1", "true", "yes", "on"},
            )
            account_home = homes.account_home_path(
                tenant_id=tenant_id,
                owner_id=owner_id,
                account_id=selection.account_id,
            )
            lease_ttl_seconds = self._lease_ttl_seconds(timeout_sec)
            lease = self.store.acquire_provider_lease(
                account_id=selection.account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                lane=f"{runtime_name}:{lease_purpose}",
                worker_id=worker_id,
                run_id=run_id,
                ttl_seconds=lease_ttl_seconds,
                required_recovery_code="",
            )
        except ProviderProjectionPending:
            self._close_active_route(worker_id, route_token)
            raise
        except ControlPlaneConflict as exc:
            self._close_active_route(worker_id, route_token)
            if preferred:
                yield self._preferred_fallback(worker, runtime_name)
                return
            if other_live_provider_lease(
                self.store, account_id=selection.account_id,
                tenant_id=tenant_id, owner_id=owner_id, run_id=run_id,
            ):
                raise ProviderAccountBusyError() from exc
            raise RuntimeErrorBase("Selected provider account is already in use") from exc
        except ControlPlaneError as exc:
            self._close_active_route(worker_id, route_token)
            if preferred:
                yield self._preferred_fallback(worker, runtime_name)
                return
            raise RuntimeErrorBase(str(exc)) from exc
        except BaseException:
            self._close_active_route(worker_id, route_token)
            raise

        lease_id = str(lease.get("lease_id") or "")
        self._update_active_route(worker_id, route_token, lease_id=lease_id)

        try:
            if execution_mode == "docker":
                assert reconcile_binding is not None
                reconcile_binding(account_home)
            account_home = homes.ensure_home(
                tenant_id=tenant_id,
                owner_id=owner_id,
                account_id=selection.account_id,
                provider=provider,
            )
            if native_key:
                homes.tighten_permissions(account_home=account_home)
            environment = homes.runtime_environment(
                provider=provider,
                account_home=account_home,
            )
            if execution_mode == "docker":
                grok_auth = "GROK_AUTH_PATH" in environment
                environment = {
                    key: f"{_CONTAINER_ACCOUNT_MOUNT}/{Path(value).name}"
                    for key, value in environment.items()
                }
                if grok_auth:
                    environment = {"GROK_AUTH_PATH": f"{_CONTAINER_ACCOUNT_MOUNT}/grok/auth.json"}
                # Codex keeps auth, plugins, MCP configuration, and connector state
                # under one CODEX_HOME. Keep the selected account's live auth.json
                # on its private mount, but let the CLI use the persistent workspace
                # home; Docker projects only auth.json into it for this lease.
                if "CODEX_HOME" in environment:
                    workspace_home = str(
                        os.environ.get("WPR_SANDBOX_HOME")
                        or _DEFAULT_CONTAINER_WORKSPACE_HOME
                    ).rstrip("/")
                    environment["CODEX_HOME"] = f"{workspace_home}/.codex"
                if "CLAUDE_CONFIG_DIR" in environment:
                    secure_storage_home = environment["CLAUDE_SECURESTORAGE_CONFIG_DIR"]
                    environment = {"CLAUDE_SECURESTORAGE_CONFIG_DIR": secure_storage_home}
        except BaseException as exc:
            if execution_mode == "docker":
                try:
                    self.store.update_provider_account_status(
                        account_id=selection.account_id,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        status="action_required",
                        reconnect_reason="Provider credentials need a safe connection check",
                        recovery_code="credential_cleanup_failed",
                    )
                except (ControlPlaneError, OSError):
                    logger.exception("Failed to quarantine unsafe provider credentials")
            try:
                self.store.release_provider_lease(
                    lease_id=lease_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
            finally:
                self._close_active_route(worker_id, route_token)
            raise RuntimeErrorBase(
                "GlassHive could not safely reconcile stale provider credentials; check the connection"
            ) from exc

        bound_worker = {
            **worker,
            "_glasshive_provider_account_bound": True,
            "_glasshive_provider_account_env": environment,
        }
        if native_key:
            bound_worker["_glasshive_provider_api_key_file"] = str(account_home / "api-key.json")
        if execution_mode == "docker":
            bound_worker.update(
                {
                    "_glasshive_provider_account_mount_host": str(
                        account_home.resolve(strict=True)
                    ),
                    "_glasshive_provider_account_mount_target": _CONTAINER_ACCOUNT_MOUNT,
                }
            )
        self._update_active_route(
            worker_id,
            route_token,
            worker=bound_worker,
            lease_id=lease_id,
        )
        binding_release_lock = Lock()
        binding_released = Event()
        binding_release_errors: list[BaseException] = []

        def release_bound_credentials() -> None:
            if execution_mode != "docker" or binding_released.is_set():
                return
            with binding_release_lock:
                if binding_released.is_set():
                    return
                assert release_binding is not None
                try:
                    # Cleanup APIs use the same exception families for transient
                    # post-container races and structural failures.  One complete
                    # replay is safe and idempotent; a repeated failure still
                    # quarantines the account and fails closed.
                    for attempt in range(_PROVIDER_CLEANUP_ATTEMPTS):
                        try:
                            release_binding(bound_worker)
                            homes.tighten_permissions(account_home=account_home)
                            break
                        except Exception:
                            if attempt + 1 >= _PROVIDER_CLEANUP_ATTEMPTS:
                                raise
                            logger.warning(
                                "Retrying transient provider credential cleanup",
                                extra={"worker_id": worker_id, "runtime": runtime_name},
                            )
                            time.sleep(_PROVIDER_CLEANUP_RETRY_DELAY_SECONDS)
                except BaseException as exc:
                    binding_release_errors.append(exc)
                    logger.exception(
                        "Provider credential cleanup failed; quarantining account",
                        extra={"worker_id": worker_id, "runtime": runtime_name},
                    )
                    try:
                        assert self.store is not None
                        self.store.update_provider_account_status(
                            account_id=selection.account_id,
                            tenant_id=tenant_id,
                            owner_id=owner_id,
                            status="action_required",
                            reconnect_reason="Provider credential cleanup failed; operator cleanup is required",
                            recovery_code="credential_cleanup_failed",
                        )
                    except (ControlPlaneError, OSError):
                        logger.exception(
                            "Failed to quarantine provider account after credential unmount failure",
                            extra={"worker_id": worker_id, "runtime": runtime_name},
                        )
                    raise
                binding_released.set()

        def begin_close_active_binding() -> None:
            self._begin_close_active_route(worker_id, route_token)

        def finalize_close_active_binding() -> None:
            self._finalize_close_active_route(worker_id, route_token)

        def abort_bound_credentials() -> None:
            begin_close_active_binding()
            try:
                if execution_mode == "docker":
                    release_bound_credentials()
                elif abort_binding is not None:
                    abort_binding(bound_worker)
            finally:
                finalize_close_active_binding()

        try:
            lease_stop, lease_thread, lease_lost = self._start_lease_heartbeat(
                lease_id=lease_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                ttl_seconds=lease_ttl_seconds,
                worker_id=worker_id,
                runtime_name=runtime_name,
                account_id=selection.account_id,
                on_lease_lost=abort_bound_credentials,
            )
        except BaseException:
            begin_close_active_binding()
            try:
                release_bound_credentials()
            finally:
                try:
                    self.store.release_provider_lease(
                        lease_id=lease_id,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                    )
                finally:
                    finalize_close_active_binding()
            raise
        body_error: BaseException | None = None
        try:
            yield bound_worker
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            begin_close_active_binding()
            try:
                release_bound_credentials()
            except BaseException:
                if body_error is None:
                    body_error = RuntimeErrorBase(
                        "GlassHive could not remove the provider credential mount; the account was quarantined"
                    )
            try:
                lease_stop.set()
                lease_thread.join(timeout=2)
                try:
                    self.store.release_provider_lease(
                        lease_id=lease_id,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                    )
                except ControlPlaneError as exc:
                    if body_error is None:
                        raise RuntimeErrorBase(
                            "GlassHive could not release the provider account mission lease"
                        ) from exc
                    logger.exception(
                        "Failed to release provider account lease after mission failure",
                        extra={"worker_id": worker_id, "runtime": runtime_name},
                    )
            finally:
                finalize_close_active_binding()
            if binding_release_errors and body_error is not None:
                raise body_error
            if lease_lost.is_set() and body_error is None:
                raise RuntimeErrorBase(
                    "Provider account lease renewal failed; GlassHive stopped the bound worker and requires reconnection"
                )
