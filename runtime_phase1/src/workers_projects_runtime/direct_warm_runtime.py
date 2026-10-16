"""Direct import-only preparation over the retained native pool lifecycle."""
import hashlib
import importlib.util
import importlib.metadata
from functools import lru_cache
import json
from pathlib import Path
import sys

from .bootstrap import bootstrap_bundle_for
from .direct_native_warm import environment_digest, idle_environment, start_direct_server
from .grok_native_warm import GrokNativeWarmMixin
from .native_warm_pool import WarmScope


@lru_cache(maxsize=1)
def _installed_identity_files():
    paths = []
    for name in ('glass-hive-runtime-phase1', 'pydantic-ai-slim', 'openai', 'anthropic', 'fastmcp', 'websockets'):
        distribution = importlib.metadata.distribution(name)
        paths.extend(Path(distribution.locate_file(item)) for item in distribution.files or []
                     if str(item).endswith(('.dist-info/RECORD', '.dist-info/METADATA')))
    return tuple(paths)


class DirectNativeWarmMixin(GrokNativeWarmMixin):
    def _native_warm_supported(self, worker):
        return worker.get('profile') == self.runtime_name and worker.get('execution_mode') == 'host'

    def _warm_context(self, worker):
        if not self._native_warm_supported(worker):
            raise ValueError('Direct preparation requires its exact host profile')
        environment = idle_environment()
        lane = 'conversation' if bootstrap_bundle_for(worker).get('run_mode') == 'conversation' else 'mission'
        spec = importlib.util.find_spec('pydantic_ai')
        if spec is None or not spec.origin:
            raise ValueError('Direct SDK is not installed')
        paths = [Path(sys.executable).resolve(), Path(spec.origin),
                 *[Path(__file__).with_name(name + '.py') for name in ('direct_runner', 'direct_native_warm',
                     'direct_runtime', 'direct_warm_runtime', 'native_warm_pool', 'grok_native_warm',
                     'grok_runtime', 'profile_runtime')], *_installed_identity_files()]
        revision = []
        for path in paths:
            info = path.stat()
            revision.append([str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])
        # Accounts remain unbound while idle. Their exact fresh projection enters
        # only after the normal account/run authorization and single-use lease.
        scope = WarmScope(*self._warm_owner(worker), self.runtime_name,
            str(worker.get('model') or self.resolve_model(self.runtime_name)), 'import-only-unbound',
            hashlib.sha256(json.dumps(revision, separators=(',', ':')).encode()).hexdigest(),
            environment_digest(environment), lane=lane)
        return scope, environment, sys.executable

    def _start_native_warm_server(self, worker, scope, environment, binary):
        return start_direct_server(scope=scope, static_environment=environment,
            cwd=self.runtime_root, state_directory=self._warm_directory(), stop_runtime=self)

    def _prepare_native_warm_command(self, worker, command, *, run_root):
        prepared = super()._prepare_native_warm_command(worker, command, run_root=run_root)
        if prepared == command:
            return command
        path = prepared[-1]
        return [sys.executable, str(Path(__file__).with_name('direct_native_warm.py')), '--attach', path,
                str(worker['worker_id']), str(worker['_active_run_id']),
                str(worker['_run_attempt_id']), self.runtime_name]
