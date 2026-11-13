"""Owner-declared immutable inputs for Docker workspaces.

Only copies already inside one service-owned workspace are eligible. The
containing runtime remains private; read bits let a different container UID
read its bind-mounted copies without granting access to the host tree.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat


POLICY_LABEL = "com.glasshive.read-only-workspace-paths"


def declared_paths(bundle: dict) -> tuple[str, ...]:
    values = bundle.get("read_only_paths", [])
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError("read_only_paths must be a list of at most 32 workspace-relative paths")
    paths = []
    for value in values:
        if (not isinstance(value, str) or not value or len(value) > 512
                or any(character in value for character in "\0\r\n,:\\")):
            raise ValueError("A read-only workspace path is invalid")
        path = PurePosixPath(value)
        if path.is_absolute() or len(path.parts) != 1 or "." in path.parts or ".." in path.parts or str(path) != value:
            raise ValueError("Read-only inputs must be top-level workspace directories")
        if any(path == previous or path in previous.parents or previous in path.parents for previous in paths):
            raise ValueError("Read-only workspace paths must not overlap")
        paths.append(path)
    return tuple(sorted(str(path) for path in paths))


def policy_fingerprint(paths: tuple[str, ...]) -> str:
    return hashlib.sha256(json.dumps(paths, separators=(",", ":")).encode()).hexdigest() if paths else ""


def prepare_paths(runtime_root: Path, workspace: Path, paths: tuple[str, ...]) -> None:
    if not paths:
        return
    declared_paths({"read_only_paths": list(paths)})
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("Read-only workspace inputs require no-follow POSIX file operations")
    try:
        parts = workspace.relative_to(runtime_root).parts
    except ValueError as exc:
        raise RuntimeError("Read-only copies must belong to a managed worker workspace") from exc
    if len(parts) != 4 or parts[0] != "workers" or parts[2:] != ("state", "workspace"):
        raise RuntimeError("Read-only copies must belong to a managed worker workspace")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    owner = os.geteuid()

    def require_owned(info):
        if info.st_uid != owner:
            raise RuntimeError("Read-only workspace copy has an unexpected owner")

    def make_readable(parent, name, *, directory=False):
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        require_owned(before)
        if directory and not stat.S_ISDIR(before.st_mode):
            raise RuntimeError("Read-only workspace path traverses a non-directory")
        if not (stat.S_ISDIR(before.st_mode) or (stat.S_ISREG(before.st_mode) and before.st_nlink == 1)):
            raise RuntimeError("Read-only copies require directories and regular files without links")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if stat.S_ISDIR(before.st_mode):
            flags |= os.O_DIRECTORY
        descriptor = os.open(name, flags, dir_fd=parent)
        try:
            opened = os.fstat(descriptor)
            require_owned(opened)
            if ((opened.st_dev, opened.st_ino, opened.st_mode, opened.st_nlink)
                    != (before.st_dev, before.st_ino, before.st_mode, before.st_nlink)):
                raise RuntimeError("Read-only workspace copy changed during verification")
            os.fchmod(descriptor, stat.S_IMODE(opened.st_mode) | (0o055 if stat.S_ISDIR(opened.st_mode) else 0o044))
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    with ExitStack() as handles:
        root = os.open(runtime_root, directory_flags)
        handles.callback(os.close, root)
        metadata = os.fstat(root)
        require_owned(metadata)
        if metadata.st_mode & 0o027:
            raise RuntimeError("Read-only workspace inputs require a private runtime root")
        workspace_fd = root
        for part in parts:
            workspace_fd = os.open(part, directory_flags, dir_fd=workspace_fd)
            handles.callback(os.close, workspace_fd)
            require_owned(os.fstat(workspace_fd))
        for relative in paths:
            parent = workspace_fd
            components = PurePosixPath(relative).parts
            for part in components[:-1]:
                parent = os.open(part, directory_flags, dir_fd=parent)
                handles.callback(os.close, parent)
                require_owned(os.fstat(parent))
            target = make_readable(parent, components[-1], directory=True)
            handles.callback(os.close, target)
            if not stat.S_ISDIR(os.fstat(target).st_mode):
                continue

            def fail_walk(error):
                raise error

            for _, directories, files, current in os.fwalk(".", dir_fd=target, follow_symlinks=False, onerror=fail_walk):
                require_owned(os.fstat(current))
                for name in directories + files:
                    child = make_readable(current, name)
                    os.close(child)
                info = os.fstat(current)
                os.fchmod(current, stat.S_IMODE(info.st_mode) | 0o055)
