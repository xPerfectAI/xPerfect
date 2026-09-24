from __future__ import annotations

import os
import secrets
from pathlib import Path


OPENAI_PLUGIN_MARKETPLACE_ORIGIN = "https://github.com/openai/plugins.git"
OPENAI_PLUGIN_MARKETPLACE_COMMIT = "11c74d6ba24d3a6d48f54a194cd00ef3beea18f9"
OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH = "/opt/glasshive-openai-plugins"


def _write_revision(path: Path) -> None:
    payload = f"{OPENAI_PLUGIN_MARKETPLACE_COMMIT}\n".encode("ascii")
    partial = path.with_name(f".{path.name}.{secrets.token_hex(8)}.partial")
    try:
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("Could not write the Codex plugin revision")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(partial, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        partial.unlink(missing_ok=True)


def provision_codex_official_marketplace(home_dir: Path) -> None:
    """Seed an empty Codex home without taking ownership from native Codex state."""

    codex_home = Path(home_dir) / ".codex"
    catalog_parent = codex_home / ".tmp"
    for directory in (codex_home, catalog_parent):
        if os.path.lexists(directory):
            if directory.is_symlink() or not directory.is_dir():
                return
            continue
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        directory.chmod(0o700)

    catalog = catalog_parent / "plugins"
    revision = catalog_parent / "plugins.sha"
    expected_target = Path(OPENAI_PLUGIN_MARKETPLACE_IMAGE_PATH)
    if os.path.lexists(catalog):
        # Codex owns this cache after first use. Preserve any native upgrade or
        # user-selected marketplace state; only finish our own interrupted seed.
        if (
            catalog.is_symlink()
            and catalog.readlink() == expected_target
            and not os.path.lexists(revision)
        ):
            _write_revision(revision)
        return
    if os.path.lexists(revision):
        return

    try:
        catalog.symlink_to(expected_target, target_is_directory=True)
        _write_revision(revision)
    except FileExistsError:
        # A native Codex process won the race. Its state is authoritative.
        return
