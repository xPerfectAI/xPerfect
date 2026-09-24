"""Resolve persisted shared membership into a private native runtime instance."""
from __future__ import annotations

import os
from pathlib import Path
from threading import Lock

from .workspace_box import WorkspaceBox, WorkspaceBoxUnavailable, WorkspaceMemberBinding
from .workspace_sandbox import WorkspaceMemberSandbox


class SharedWorkspaceRuntimes:
    def __init__(self, store, binder=None, storage=None):
        self.store = store
        self.binder = binder
        self.storage = storage
        self._lock = Lock()
        self._members = {}
        self.recovery_issues = []
        from .workspace_resources import WorkspaceResources
        self.resources = WorkspaceResources(self)

    def recover_pending(self, profiled_runtime):
        if self.binder is None or self.binder.store is None:
            return
        from .workspace_projection import recovery_projection
        self.recovery_issues = []
        for record in self.binder.store.pending_provider_projections():
            identity = record["binding"]
            try:
                worker = self.store.get_worker(identity["worker_id"], identity["tenant_id"], identity["owner_id"])
                if worker is None:
                    raise WorkspaceBoxUnavailable("Recovery member is no longer present")
                runtime = profiled_runtime._runtime_for_worker(worker)
                box = runtime.sandbox.box
                self.binder.recover_projection(record, projection_factory=lambda *, record, assert_lease:
                    recovery_projection(box, record=record, assert_lease=assert_lease))
            except Exception:
                # The durable pending/quarantined row remains the authority: failed
                # or still-live recovery can never open admission or release a lease.
                self.recovery_issues.append(identity["worker_id"])

    def readiness(self, workspace, *, runtime=None):
        """Report the current shared substrate without creating a workspace or member.

        This is deliberately an observational probe.  It checks the authorities that
        admission will use, then asks the existing resource accountant for a live
        snapshot when a runtime is available.  Configuration strings alone never make
        a shared workspace ready.
        """
        import sys

        if str(workspace.get("mode") or "") != "shared":
            return {"available": False, "code": "shared_workspace_required"}
        if str(workspace.get("execution_mode") or "") != "docker":
            return {"available": False, "code": "shared_host_execution_unavailable"}
        if sys.platform != "linux":
            return {"available": False, "code": "shared_linux_runtime_required"}
        if self.recovery_issues:
            return {"available": False, "code": "shared_account_recovery_pending"}

        required = (
            "XPERFECT_SHARED_VOLUME_ROOT",
            "XPERFECT_SHARED_VOLUME_NAME",
            "XPERFECT_SHARED_IMAGE",
            "XPERFECT_SHARED_MEMORY_BYTES",
            "XPERFECT_SHARED_PIDS_LIMIT",
            "XPERFECT_SHARED_NETWORK",
            "XPERFECT_CONTROLLER_ID",
        )
        if not all(str(os.environ.get(key) or "").strip() for key in required):
            return {"available": False, "code": "shared_configuration_required"}
        try:
            memory_bytes = int(os.environ["XPERFECT_SHARED_MEMORY_BYTES"])
            pids_limit = int(os.environ["XPERFECT_SHARED_PIDS_LIMIT"])
        except (KeyError, TypeError, ValueError):
            return {"available": False, "code": "shared_configuration_invalid"}
        if memory_bytes <= 0 or pids_limit <= 0:
            return {"available": False, "code": "shared_configuration_invalid"}

        volume_root = Path(str(os.environ["XPERFECT_SHARED_VOLUME_ROOT"]).strip())
        controller_id = str(os.environ["XPERFECT_CONTROLLER_ID"]).strip().lower()
        if (not volume_root.is_absolute() or not volume_root.exists()
                or not volume_root.is_dir() or volume_root != volume_root.resolve()
                or len(controller_id) != 64
                or any(character not in "0123456789abcdef" for character in controller_id)):
            return {"available": False, "code": "shared_storage_authority_unavailable"}

        binder = self.binder
        homes = getattr(binder, "homes", None) if binder is not None else None
        if (binder is None or getattr(binder, "store", None) is None
                or homes is None or not callable(getattr(homes, "account_home_path", None))):
            return {"available": False, "code": "shared_account_projection_unavailable"}
        if self.storage is not None and not callable(getattr(homes, "owner_root_resolver", None)):
            return {"available": False, "code": "shared_owner_storage_unavailable"}
        launcher = getattr(homes, "native_launcher", None)
        if launcher is None or not callable(getattr(launcher, "inventory", None)):
            return {"available": False, "code": "shared_account_container_unavailable"}
        if runtime is None:
            return {"available": False, "code": "shared_resource_authority_unavailable"}

        usage = self.resources.usage(runtime, cached_only=False)
        probe_fields = ("process_probe_ok", "memory_probe_ok", "disk_probe_ok")
        if not all(usage.get(field) is True for field in probe_fields):
            result = {
                "available": False,
                "code": "shared_resource_authority_unavailable",
            }
            # The resource probe only publishes an allowlisted machine code.
            # Preserve it for operator diagnostics without exposing exception
            # text through the readiness API.
            probe_error_code = str(usage.get("probe_error_code") or "").strip()
            if probe_error_code:
                result["probe_error_code"] = probe_error_code
            return result
        available_memory = int(usage.get("available_memory_bytes") or 0)
        available_disk = int(usage.get("available_disk_bytes") or 0)
        if available_memory <= 0 or available_disk <= 0:
            return {
                "available": False,
                "code": "shared_capacity_busy",
                "available_memory_bytes": max(0, available_memory),
                "available_disk_bytes": max(0, available_disk),
            }
        return {
            "available": True,
            "code": "ready",
            "accounting_version": usage.get("accounting_version", "workspace-v1"),
            "available_memory_bytes": available_memory,
            "available_disk_bytes": available_disk,
        }

    def release_idle_box(self, worker: dict, profiled_runtime) -> bool:
        workspace_id = str(worker.get("workspace_id") or "")
        tenant_id = str(worker.get("tenant_id") or "local")
        owner_id = str(worker.get("owner_id") or "")
        if not workspace_id or not owner_id:
            return False
        member_runtime = profiled_runtime._runtime_for_worker(worker)
        box = getattr(getattr(member_runtime, "sandbox", None), "box", None)
        if box is None:
            return False
        return box.release_if_all_members_idle(
            lambda: self.store.idle_execution_workspace_member_uids(
                workspace_id, tenant_id, owner_id
            )
        )

    def for_worker(self, worker: dict, template, *, workspace=None, identity=None):
        from .execution_profile import packaged_linux
        packaged = packaged_linux()
        workspace_id = str(worker.get('workspace_id') or '')
        if not workspace_id:
            if packaged:
                raise WorkspaceBoxUnavailable('Packaged workers require a persisted execution workspace')
            return template
        workspace = workspace or self.store.get_execution_workspace(
            workspace_id, str(worker.get('tenant_id') or 'local'), str(worker.get('owner_id') or ''))
        if workspace is None:
            raise WorkspaceBoxUnavailable('Execution workspace is unavailable for this owner')
        if workspace['mode'] != 'shared' and self.storage is None and not packaged:
            return template
        if workspace['execution_mode'] != 'docker':
            raise WorkspaceBoxUnavailable('Shared host execution is not yet configured')
        required = ['XPERFECT_SHARED_VOLUME_ROOT', 'XPERFECT_SHARED_VOLUME_NAME',
                    'XPERFECT_SHARED_IMAGE', 'XPERFECT_SHARED_MEMORY_BYTES', 'XPERFECT_SHARED_PIDS_LIMIT']
        config = {key: os.environ.get(key, '').strip() for key in required}
        if packaged:
            for key in ('XPERFECT_SHARED_NETWORK', 'XPERFECT_CONTROL_ROOT'):
                config[key] = os.environ.get(key, '').strip()
        if not all(config.values()):
            raise WorkspaceBoxUnavailable('Shared workspace Linux storage and resource configuration is required')
        snapshot = self.storage.owner_snapshot(worker['tenant_id'], worker['owner_id']) if self.storage else None
        if snapshot is not None:
            for field in ('workspace_dir', 'home_dir'):
                raw = worker.get(field)
                if raw and not Path(raw).is_relative_to(snapshot.root):
                    raise WorkspaceBoxUnavailable('Existing worker storage requires explicit owner-root migration')
        identity = identity or self.store.reserve_workspace_member_identity(
            worker['worker_id'], tenant_id=worker['tenant_id'], owner_id=worker['owner_id'])
        binding = WorkspaceMemberBinding(workspace_id, worker['worker_id'], worker['tenant_id'],
                                          worker['owner_id'], identity['member_uid'])
        key = (binding, type(template), tuple(config.items()), workspace["file_placement"])
        with self._lock:
            runtime = self._members.get(key)
            if runtime is None:
                box = WorkspaceBox(volume_root=snapshot.root if snapshot else Path(config['XPERFECT_SHARED_VOLUME_ROOT']),
                                   volume_subpath=str(snapshot.root.relative_to(self.storage.root)) if snapshot else '',
                                   control_root=(self.storage.control_root / 'workspaces' if self.storage else
                                                 Path(config['XPERFECT_CONTROL_ROOT']) / 'workspaces' if packaged else None),
                                   network=config.get('XPERFECT_SHARED_NETWORK'),
                                   volume_name=config['XPERFECT_SHARED_VOLUME_NAME'],
                                   image=config['XPERFECT_SHARED_IMAGE'], binding=binding,
                                   memory_bytes=int(config['XPERFECT_SHARED_MEMORY_BYTES']),
                                   pids_limit=int(config['XPERFECT_SHARED_PIDS_LIMIT']),
                                   file_placement=workspace['file_placement'])
                runtime = type(template)(base_dir=str(template.base_dir), create_directories=False)
                runtime.sandbox = WorkspaceMemberSandbox(box)
                if self.binder is not None and self.binder.store is not None:
                    from .workspace_projection import assert_native_launch, assert_native_resume, projection_factory
                    def guard():
                        if self.storage is not None:
                            current = self.storage.owner_snapshot(binding.tenant_id, binding.owner_id)
                            if current.root != box.volume_root:
                                raise WorkspaceBoxUnavailable('Owner storage root changed before native launch')
                        assert_native_launch(box, self.binder.store)
                    runtime.sandbox.assert_native_launch = guard
                    def resume_guard(claimed_worker):
                        if self.storage is not None:
                            current = self.storage.owner_snapshot(binding.tenant_id, binding.owner_id)
                            if current.root != box.volume_root:
                                raise WorkspaceBoxUnavailable('Owner storage root changed before native resume')
                        return assert_native_resume(box, self.binder.store, self.store, claimed_worker)
                    runtime.sandbox.assert_native_resume = resume_guard
                    runtime.sandbox.projection_factory = projection_factory(box, self.binder.store)
                self._members[key] = runtime
            # Observer registration can change after a member runtime is cached.
            for name in ('_host_process_observer', '_run_start_observer',
                         '_native_event_observer', '_provider_liveness_observer'):
                setattr(runtime, name, getattr(template, name, None))
            return runtime
