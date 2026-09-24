"""Measured packaged-container capacity with one memory charge per cgroup."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
from threading import Lock

from .docker_sandbox import _docker_size_bytes
from .workspace_box import WorkspaceBoxUnavailable


logger = logging.getLogger(__name__)


_SAFE_PROBE_ERRORS = {
    "Controller identity or network is unavailable": "controller_identity_unavailable",
    "Controller identity or recovery is unavailable": "controller_identity_unavailable",
    "Contained account inventory is unavailable": "account_inventory_unavailable",
    "Shared substrate probe failed": "shared_substrate_probe_failed",
    "Shared substrate identity is unavailable": "shared_substrate_identity_unavailable",
    "Shared substrate proof is unavailable": "shared_substrate_proof_unavailable",
    "Shared native image identity changed": "native_image_identity_changed",
    "Shared controller identity is unavailable": "controller_identity_unavailable",
    "Shared controller storage identity changed": "controller_storage_identity_changed",
    "Shared native bridge identity is unavailable": "native_bridge_identity_unavailable",
    "Shared native bridge identity changed": "native_bridge_identity_changed",
    "Shared filesystem ACL tools are unavailable": "filesystem_acl_tools_unavailable",
    "Shared filesystem ACL inspection is unavailable": "filesystem_acl_inspection_unavailable",
    "Shared filesystem ACL application is unavailable": "filesystem_acl_application_unavailable",
    "Native workspace guard identity is unavailable": "native_guard_identity_unavailable",
    "Native workspace guard identity changed": "native_guard_identity_changed",
    "Native workspace guard failed": "native_guard_failed",
    "Native workspace guard is unavailable": "native_guard_unavailable",
    "Native workspace guard cleanup is unconfirmed": "native_guard_cleanup_unconfirmed",
    "Native process inventory is unavailable": "native_process_inventory_unavailable",
    "Native process inventory is malformed": "native_process_inventory_malformed",
    "Running container has no measured processes": "native_process_inventory_empty",
    "Container memory authority is unavailable": "container_memory_authority_unavailable",
    "Native bridge identity changed": "native_bridge_identity_changed",
    "Controller left its native bridge": "controller_left_native_bridge",
    "Native network contains unverified containers": "native_network_membership_unverified",
    "Unregistered native member is still alive": "native_member_unregistered",
    "Account reservation is invalid": "account_reservation_invalid",
    "Account capacity identity changed": "account_capacity_identity_changed",
    "Account recovery is pending": "account_recovery_pending",
    "Container memory observation is malformed": "container_memory_observation_malformed",
    "Container memory observation is unavailable": "container_memory_observation_unavailable",
    "Container disappeared during measurement": "container_disappeared_during_measurement",
    "Duplicate container reservation": "duplicate_container_reservation",
}


def _probe_error_code(exc: BaseException) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "docker_command_timeout"
    text = str(exc).strip()
    if text in _SAFE_PROBE_ERRORS:
        return _SAFE_PROBE_ERRORS[text]
    if isinstance(exc, WorkspaceBoxUnavailable):
        return "workspace_box_unavailable"
    return "resource_probe_failed"


def unavailable(error_code: str = "resource_probe_unavailable"):
    return {'accounting_version': 'workspace-v1', 'child_processes': 0, 'threads': 0,
            'available_memory_bytes': 0, 'available_disk_bytes': 0,
            'running_worker_containers': 0, 'running_worker_ids': [],
            'worker_process_counts': {}, 'process_probe_ok': False,
            'memory_probe_ok': False, 'disk_probe_ok': False,
            'probe_error_code': str(error_code or 'resource_probe_unavailable')}


# Admission and readiness accept a substrate proof younger than 30 seconds.
# The service readiness loop runs about every ten seconds and renews the proof
# once it is 15 seconds old, so a steady loop never lets a valid proof lapse.
PROOF_MAX_AGE_SECONDS = 30.0
PROOF_RENEW_AGE_SECONDS = 15.0

PROSPECTIVE_WORKER_KEY = '__prospective_worker__'
"""Reservation key the service admission uses for a worker not yet persisted."""


def pending_memory(usage, pending_ids, reservations, prospective, lookup):
    """Charge pending boxes once and reject member promises above their box cap."""
    limit = int(usage['workspace_memory_bytes'])
    if limit <= 0:
        raise ValueError('Invalid workspace memory bound')
    resident = set(usage['accounted_workspace_ids'])
    groups = {}
    pending = set()
    prospective_key = str((prospective or {}).get('worker_id') or '') or PROSPECTIVE_WORKER_KEY
    for worker_id, reserved in reservations.items():
        is_prospective = bool(prospective) and worker_id == prospective_key
        worker = prospective if is_prospective else lookup(worker_id)
        workspace_id = str((worker or {}).get('workspace_id') or '')
        if is_prospective and not workspace_id and worker_id == PROSPECTIVE_WORKER_KEY:
            # A new separate-workspace member will own a box of its own.
            workspace_id = PROSPECTIVE_WORKER_KEY
        if not workspace_id or int(reserved) <= 0:
            raise ValueError('Worker reservation has no persisted workspace')
        groups[workspace_id] = groups.get(workspace_id, 0) + int(reserved)
        if worker_id in pending_ids and workspace_id not in resident:
            pending.add(workspace_id)
    return len(pending) * limit, any(amount > limit for amount in groups.values())


def process_counts(text: str) -> dict[int, tuple[int, int]]:
    lines = [line.split() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != ['UID', 'PID', 'LWP']:
        raise WorkspaceBoxUnavailable('Native process inventory is unavailable')
    processes, threads = {}, {}
    for row in lines[1:]:
        if len(row) != 3 or not all(item.isdecimal() for item in row):
            raise WorkspaceBoxUnavailable('Native process inventory is malformed')
        uid, pid, tid = map(int, row)
        processes.setdefault(uid, set()).add(pid)
        threads.setdefault(uid, set()).add(tid)
    if not processes:
        raise WorkspaceBoxUnavailable('Running container has no measured processes')
    return {uid: (len(pids), len(threads[uid])) for uid, pids in processes.items()}


class WorkspaceResources:
    def __init__(self, shared):
        self.shared = shared
        self.cached = None
        self._substrate_proof: tuple[float, dict[str, str]] | None = None
        self._probe_lock = Lock()
        self.last_refresh_error_code = ""

    def invalidate_capacity_snapshot(self):
        """Re-measure after a confirmed box change, retaining substrate proof."""
        with self._probe_lock:
            self.cached = None

    def usage(self, runtime, *, cached_only=False):
        # Admission must never wait behind the controlled Docker/ACL probe.
        # If it is running, fail closed and let the retry scheduler revisit
        # this work after the readiness loop publishes a complete snapshot.
        if not self._probe_lock.acquire(blocking=not cached_only):
            return unavailable("resource_probe_in_progress")
        try:
            controller = str(os.environ.get("XPERFECT_CONTROLLER_ID") or "").strip()
            network = str(os.environ.get("XPERFECT_SHARED_NETWORK") or "").strip()
            proof_current = self._proof_matches_configuration(
                controller=controller, network=network
            )
            if (
                proof_current
                and self.cached
                and self.cached[0] + (30 if cached_only else 2) > time.monotonic()
            ):
                return self.cached[1]
            if not proof_current:
                # A read cannot establish the controlled ACL/image proof. Keep
                # its last specific failure instead of masking it with the
                # generic missing-proof error from _measure.
                return unavailable(
                    self.last_refresh_error_code or "shared_substrate_proof_unavailable"
                )
            if cached_only:
                return unavailable(self.last_refresh_error_code or "resource_probe_snapshot_unavailable")
            try:
                result = self._measure(runtime)
                self.last_refresh_error_code = ""
            except Exception as exc:
                self._record_failure(_probe_error_code(exc), "Workspace resource probe unavailable")
                result = unavailable(self.last_refresh_error_code)
            self.cached = (time.monotonic(), result)
            return result
        finally:
            self._probe_lock.release()

    def refresh(self, runtime):
        """Refresh the controlled substrate proof and capacity snapshot.

        Active capability checks are owned by the service startup/preflight
        loop. Admission and read-only readiness consume the resulting proof;
        they never start a probe container or mutate ACLs themselves.
        """

        with self._probe_lock:
            try:
                controller = str(os.environ.get("XPERFECT_CONTROLLER_ID") or "").strip()
                network = str(os.environ.get("XPERFECT_SHARED_NETWORK") or "").strip()
                if not self._proof_matches_configuration(
                    controller=controller, network=network,
                    max_age=PROOF_RENEW_AGE_SECONDS,
                ):
                    self._refresh_substrate_proof(runtime)
                result = self._measure(runtime)
            except Exception as exc:
                self._substrate_proof = None
                self._record_failure(_probe_error_code(exc), "Workspace resource refresh failed closed")
                result = unavailable(self.last_refresh_error_code)
            else:
                self.last_refresh_error_code = ""
            self.cached = (time.monotonic(), result)
            return result

    def _record_failure(self, code: str, message: str) -> None:
        # Only allowlisted codes reach the log. The readiness loop repeats
        # every few seconds, so log a failure when its code changes.
        if code != self.last_refresh_error_code:
            logger.warning("%s: %s", message, code, extra={"error_code": code})
        self.last_refresh_error_code = code

    def _proof_matches_configuration(self, *, controller: str, network: str,
                                     max_age: float = PROOF_MAX_AGE_SECONDS) -> bool:
        proof = self._substrate_proof
        if not proof or proof[0] + max_age <= time.monotonic():
            return False
        expected = {
            "image": str(os.environ.get("XPERFECT_SHARED_IMAGE") or "").strip(),
            "volume_name": str(os.environ.get("XPERFECT_SHARED_VOLUME_NAME") or "").strip(),
            "volume_root": str(os.environ.get("XPERFECT_SHARED_VOLUME_ROOT") or "").strip(),
            "control_root": str(os.environ.get("XPERFECT_CONTROL_ROOT") or "").strip(),
            "controller": controller,
            "network": network,
        }
        return all(proof[1].get(key) == value for key, value in expected.items())

    def _refresh_substrate_proof(self, runtime) -> None:
        controller = str(os.environ.get("XPERFECT_CONTROLLER_ID") or "").strip()
        network = str(os.environ.get("XPERFECT_SHARED_NETWORK") or "").strip()
        if not re.fullmatch(r"[a-f0-9]{64}", controller) or not network:
            raise WorkspaceBoxUnavailable("Controller identity or network is unavailable")
        docker = runtime.codex.sandbox._docker

        def raw_call(args):
            # Callers inspect the exit status themselves: guard cleanup
            # confirms removal from a non-zero ``inspect``. The sandbox
            # wrapper raises on non-zero exits unless check is disabled.
            return docker(args, capture_output=True, timeout_sec=10, check=False)

        def call(args):
            result = raw_call(args)
            if getattr(result, "returncode", 0) != 0:
                raise WorkspaceBoxUnavailable("Shared substrate probe failed")
            return result.stdout

        self._validate_shared_substrate(
            call, controller=controller, network=network, raw_call=raw_call
        )
        network_records = json.loads(call(["network", "inspect", network]))
        if not isinstance(network_records, list) or len(network_records) != 1:
            raise WorkspaceBoxUnavailable("Shared native bridge identity is unavailable")
        network_record = network_records[0]
        if (network_record.get("Driver") != "bridge"
                or controller not in set((network_record.get("Containers") or {}))):
            raise WorkspaceBoxUnavailable("Shared native bridge identity changed")
        self._substrate_proof = (
            time.monotonic(),
            {
                "image": str(os.environ.get("XPERFECT_SHARED_IMAGE") or "").strip(),
                "volume_name": str(os.environ.get("XPERFECT_SHARED_VOLUME_NAME") or "").strip(),
                "volume_root": str(os.environ.get("XPERFECT_SHARED_VOLUME_ROOT") or "").strip(),
                "control_root": str(os.environ.get("XPERFECT_CONTROL_ROOT") or "").strip(),
                "controller": controller,
                "network": network,
            },
        )

    @staticmethod
    def _validate_shared_substrate(
        call, *, controller: str, network: str, raw_call=None
    ) -> None:
        """Prove the empty-workspace substrate before reporting capacity.

        A resource snapshot with no admitted members cannot inspect a member box.
        The controller therefore owns the first live image/volume identity check;
        the native guard and ACL probes exercise the same capabilities that a
        later member admission will require, without creating a workspace.
        """
        image = str(os.environ.get("XPERFECT_SHARED_IMAGE") or "").strip()
        volume_name = str(os.environ.get("XPERFECT_SHARED_VOLUME_NAME") or "").strip()
        volume_root = Path(str(os.environ.get("XPERFECT_SHARED_VOLUME_ROOT") or "").strip())
        control_root = Path(str(os.environ.get("XPERFECT_CONTROL_ROOT") or "").strip())
        if (not re.fullmatch(r"sha256:[a-f0-9]{64}", image)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", volume_name)
                or not volume_root.is_absolute() or not control_root.is_absolute()):
            raise WorkspaceBoxUnavailable("Shared substrate identity is unavailable")
        if call(["image", "inspect", "--format", "{{.Id}}", image]).strip() != image:
            raise WorkspaceBoxUnavailable("Shared native image identity changed")
        records = json.loads(call(["inspect", controller]))
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise WorkspaceBoxUnavailable("Shared controller identity is unavailable")
        record = records[0]
        mounts = {str(item.get("Destination")): item for item in (record.get("Mounts") or [])}
        data_mount = mounts.get(str(volume_root))
        control_mount = mounts.get(str(control_root))
        if (record.get("Id") != controller or record.get("State", {}).get("Running") is not True
                or not isinstance(data_mount, dict)
                or data_mount.get("Type") != "volume"
                or data_mount.get("Name") != volume_name
                or data_mount.get("RW") is False
                or not isinstance(control_mount, dict)
                or control_mount.get("Type") != "volume"
                or control_mount.get("Name") == volume_name):
            raise WorkspaceBoxUnavailable("Shared controller storage identity changed")

        # WorkspaceBox applies these ACLs to service-owned publication/member
        # paths.  Probe a disposable file in the configured volume so an image
        # with missing ACL support cannot report ready while empty.
        if shutil.which("getfacl") is None or shutil.which("setfacl") is None:
            raise WorkspaceBoxUnavailable("Shared filesystem ACL tools are unavailable")
        probe = volume_root / (".xperfect-readiness-" + secrets.token_hex(16))
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            acl = subprocess.run(["getfacl", "-cEpn", str(probe)], capture_output=True,
                                 text=True, timeout=5)
            if acl.returncode:
                raise WorkspaceBoxUnavailable("Shared filesystem ACL inspection is unavailable")
            applied = subprocess.run(
                ["setfacl", "--set",
                 "user::rw-,user:20001:rw-,group::---,mask::rw-,other::---", str(probe)],
                capture_output=True, text=True, timeout=5)
            if applied.returncode:
                raise WorkspaceBoxUnavailable("Shared filesystem ACL application is unavailable")
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            probe.unlink(missing_ok=True)

        # This is the exact immutable entrypoint used by WorkspaceBox.guarded_command.
        program = (
            "import workers_projects_runtime.account_native_entry; "
            "import ctypes; ctypes.CDLL('libseccomp.so.2'); "
            "print('xperfect-native-guard-ready')"
        )
        guarded = WorkspaceResources._native_guard_probe(
            call, image, program, raw_call=raw_call
        )
        if guarded.strip() != "xperfect-native-guard-ready":
            raise WorkspaceBoxUnavailable("Native workspace guard is unavailable")

    @staticmethod
    def _native_guard_probe(call, image: str, program: str, *, raw_call=None) -> str:
        """Run one attested guard probe with explicit container ownership.

        ``--rm`` is intentionally avoided. A named container is created,
        started, waited, inspected, and removed in a finally block so a CLI
        timeout cannot leave an untracked daemon task behind.
        """

        name = "xperfect-readiness-" + secrets.token_hex(16)
        create_args = [
            "create", "--name", name, "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "65534:65534", "--ipc", "none", "--no-healthcheck",
            "--memory", str(128 * 1024**2), "--pids-limit", "16",
            "--entrypoint", "/usr/bin/python3", image, "-I", "-m",
            "workers_projects_runtime.storage_quota_guard", "--", "/usr/bin/python3",
            "-I", "-c", program,
        ]
        container_id = ""
        create_attempted = False
        cleanup_error: WorkspaceBoxUnavailable | None = None
        try:
            create_attempted = True
            container_id = str(call(create_args) or "").strip()
            if not re.fullmatch(r"[a-f0-9]{64}", container_id):
                raise WorkspaceBoxUnavailable("Native workspace guard identity is unavailable")
            record = json.loads(call(["inspect", name]))
            if not isinstance(record, list) or len(record) != 1:
                raise WorkspaceBoxUnavailable("Native workspace guard identity is unavailable")
            initial = record[0]
            if initial.get("Id") != container_id or initial.get("State", {}).get("Running") is not False:
                raise WorkspaceBoxUnavailable("Native workspace guard identity changed")
            call(["start", name])
            exit_code = str(call(["wait", name]) or "").strip()
            if exit_code != "0":
                raise WorkspaceBoxUnavailable("Native workspace guard failed")
            final = json.loads(call(["inspect", name]))
            if not isinstance(final, list) or len(final) != 1:
                raise WorkspaceBoxUnavailable("Native workspace guard identity is unavailable")
            state = final[0].get("State") or {}
            exit_value = state.get("ExitCode")
            if (final[0].get("Id") != container_id or state.get("Running") is not False
                    or not isinstance(exit_value, int) or exit_value != 0
                    or state.get("OOMKilled") is True):
                raise WorkspaceBoxUnavailable("Native workspace guard identity changed")
            return call(["logs", name])
        finally:
            cleanup_id = container_id
            if not cleanup_id and raw_call is not None:
                try:
                    listed = raw_call(
                        ["ps", "-aq", "--no-trunc", "--filter", f"name=^{name}$"]
                    )
                except Exception:
                    cleanup_error = WorkspaceBoxUnavailable(
                        "Native workspace guard cleanup is unconfirmed"
                    )
                else:
                    if listed.returncode != 0:
                        cleanup_error = WorkspaceBoxUnavailable(
                            "Native workspace guard cleanup is unconfirmed"
                        )
                    else:
                        candidates = [
                            line.strip()
                            for line in str(listed.stdout or "").splitlines()
                            if line.strip()
                        ]
                        if len(candidates) > 1 or any(
                            not re.fullmatch(r"[a-f0-9]{64}", candidate)
                            for candidate in candidates
                        ):
                            cleanup_error = WorkspaceBoxUnavailable(
                                "Native workspace guard cleanup is unconfirmed"
                            )
                        elif candidates:
                            cleanup_id = candidates[0]
                        elif create_attempted:
                            cleanup_error = WorkspaceBoxUnavailable(
                                "Native workspace guard cleanup is unconfirmed"
                            )
            if cleanup_id:
                if raw_call is None:
                    call(["rm", "-f", cleanup_id])
                else:
                    try:
                        removed = raw_call(["rm", "-f", cleanup_id])
                    except Exception:
                        cleanup_error = WorkspaceBoxUnavailable(
                            "Native workspace guard cleanup is unconfirmed"
                        )
                    else:
                        if removed.returncode != 0:
                            cleanup_error = WorkspaceBoxUnavailable(
                                "Native workspace guard cleanup is unconfirmed"
                            )
                        else:
                            try:
                                residual = raw_call(["inspect", cleanup_id])
                            except Exception:
                                cleanup_error = WorkspaceBoxUnavailable(
                                    "Native workspace guard cleanup is unconfirmed"
                                )
                            else:
                                if residual.returncode == 0:
                                    cleanup_error = WorkspaceBoxUnavailable(
                                        "Native workspace guard cleanup is unconfirmed"
                                    )
                                elif "no such object" not in str(
                                    residual.stderr or ""
                                ).lower() and "not found" not in str(
                                    residual.stderr or ""
                                ).lower():
                                    cleanup_error = WorkspaceBoxUnavailable(
                                        "Native workspace guard cleanup is unconfirmed"
                                    )
            if cleanup_error is not None:
                raise cleanup_error

    def _measure(self, runtime):
        shared = self.shared
        controller = os.environ.get('XPERFECT_CONTROLLER_ID', '')
        network = os.environ.get('XPERFECT_SHARED_NETWORK', '')
        if not re.fullmatch(r'[a-f0-9]{64}', controller) or not network or shared.recovery_issues:
            raise WorkspaceBoxUnavailable('Controller identity or recovery is unavailable')
        launcher = shared.binder.homes.native_launcher if shared.binder else None
        if launcher is None or not callable(getattr(launcher, 'inventory', None)):
            raise WorkspaceBoxUnavailable('Contained account inventory is unavailable')
        docker = runtime.codex.sandbox._docker
        def call(args):
            return docker(args, capture_output=True, timeout_sec=10).stdout
        info = json.loads(call(['info', '--format', '{{json .}}']))
        if str(info.get('CgroupVersion')) != '2' or int(info.get('MemTotal') or 0) <= 0:
            raise WorkspaceBoxUnavailable('Container memory authority is unavailable')
        if not self._proof_matches_configuration(controller=controller, network=network):
            raise WorkspaceBoxUnavailable('Shared substrate proof is unavailable')
        network_record = json.loads(call(['network', 'inspect', network]))[0]
        if network_record.get('Driver') != 'bridge':
            raise WorkspaceBoxUnavailable('Native bridge identity changed')
        attached = set((network_record.get('Containers') or {}))
        if controller not in attached:
            raise WorkspaceBoxUnavailable('Controller left its native bridge')
        with shared.store._connect() as conn:
            members = [dict(row) for row in conn.execute('''
              SELECT w.*,i.member_uid FROM workers w JOIN execution_workspace_identities i
              ON w.worker_id=i.worker_id AND w.workspace_id=i.workspace_id
              AND w.tenant_id=i.tenant_id AND w.owner_id=i.owner_id
              WHERE w.execution_mode='docker'
            ''')]
        boxes, workspace_ids = {}, set()
        for worker in members:
            template = runtime._runtime_for_profile(worker['profile'], 'docker')
            member = shared.for_worker(worker, template, identity={'member_uid': worker['member_uid']})
            box = member.sandbox.box
            if box.name not in boxes:
                record = box._inspect()
                boxes[box.name] = [box, record, {}]
            boxes[box.name][2][worker['member_uid']] = worker['worker_id']
        limits, counts, background_processes, background_threads = {}, {}, 0, 0
        verified = {controller}
        for box, record, identities in boxes.values():
            if record is None or not record['State'].get('Running'):
                continue
            identity = record['Id']
            verified.add(identity)
            limits[identity] = box.memory_bytes
            workspace_ids.add(box.binding.workspace_id)
            measured = process_counts(call(['top', identity, '-eLo', 'uid,pid,lwp']))
            for uid, (processes, threads) in measured.items():
                worker_id = identities.get(uid)
                if worker_id is None:
                    # The idle supervisor is charged once. An unregistered
                    # native UID is an orphan and blocks new admissions.
                    if uid != 65534:
                        raise WorkspaceBoxUnavailable('Unregistered native member is still alive')
                    background_processes += processes
                    background_threads += threads
                else:
                    counts[worker_id] = {'child_processes': processes, 'threads': threads}
        pending_account_memory = 0
        for account in launcher.inventory():
            if account.memory_bytes <= 0 or account.pids_limit <= 0:
                raise WorkspaceBoxUnavailable('Account reservation is invalid')
            if not account.container_id:
                pending_account_memory += account.memory_bytes
                background_processes += account.pids_limit
                background_threads += account.pids_limit
                continue
            # Inventory owns generation/lease identity; capacity independently
            # verifies the live cgroup bounds and counts every contained task.
            record = json.loads(call(['inspect', account.container_id]))[0]
            if (record.get('Id') != account.container_id
                    or record['HostConfig'].get('Memory') != account.memory_bytes
                    or record['HostConfig'].get('PidsLimit') != account.pids_limit):
                raise WorkspaceBoxUnavailable('Account capacity identity changed')
            if not record['State'].get('Running'):
                if account.pending_recovery:
                    raise WorkspaceBoxUnavailable('Account recovery is pending')
                continue
            verified.add(account.container_id)
            if account.container_id in limits:
                raise WorkspaceBoxUnavailable('Duplicate container reservation')
            limits[account.container_id] = account.memory_bytes
            measured = process_counts(call(['top', account.container_id, '-eLo', 'uid,pid,lwp']))
            background_processes += sum(value[0] for value in measured.values())
            background_threads += sum(value[1] for value in measured.values())
        if attached != verified:
            raise WorkspaceBoxUnavailable('Native network contains unverified containers')
        memory_used = pending_account_memory + sum(limits.values())
        seen = set()
        for line in call(['stats', '--no-stream', '--format', '{{json .}}']).splitlines():
            row = json.loads(line)
            raw = str(row.get('ID') or row.get('Container') or '')
            if not re.fullmatch(r'[a-f0-9]{12,64}', raw):
                raise WorkspaceBoxUnavailable('Container memory observation is malformed')
            known = [identity for identity in limits if identity.startswith(raw)]
            if len(known) > 1 or raw in seen:
                raise WorkspaceBoxUnavailable('Container memory observation is ambiguous')
            seen.add(raw)
            used = _docker_size_bytes(row.get('MemUsage'))
            if used is None:
                raise WorkspaceBoxUnavailable('Container memory observation is unavailable')
            if not known:
                memory_used += used
        if any(not any(identity.startswith(raw) for raw in seen) for identity in limits):
            raise WorkspaceBoxUnavailable('Container disappeared during measurement')
        return {'accounting_version': 'workspace-v1',
                'child_processes': background_processes + sum(value['child_processes'] for value in counts.values()),
                'threads': background_threads + sum(value['threads'] for value in counts.values()),
                'unattributed_child_processes': background_processes, 'unattributed_threads': background_threads,
                'available_memory_bytes': max(0, int(info['MemTotal']) - memory_used),
                'available_disk_bytes': shutil.disk_usage(Path(os.environ['XPERFECT_SHARED_VOLUME_ROOT'])).free,
                'running_worker_containers': len(workspace_ids), 'running_container_ids': sorted(limits),
                'running_worker_ids': sorted(counts), 'worker_process_counts': counts,
                'accounted_workspace_ids': sorted(workspace_ids),
                'workspace_memory_bytes': int(os.environ['XPERFECT_SHARED_MEMORY_BYTES']),
                'process_probe_ok': True, 'memory_probe_ok': True, 'disk_probe_ok': True}
