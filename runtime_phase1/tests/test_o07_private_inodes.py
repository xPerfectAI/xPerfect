"""Private native config must not mutate a shared inode, including target races."""

import json
import os
import stat
from pathlib import Path

import pytest
from workers_projects_runtime import bootstrap
from workers_projects_runtime.native_context_projection import (
    materialize_native_context,
)


@pytest.mark.parametrize(
    "name",
    [
        ".grok/AGENTS.md",
        ".codex/config.toml",
        ".glasshive/native/settings.json",
        ".glasshive/native/managed-mcp.json",
    ],
)
@pytest.mark.parametrize("link", ["hard", "symbolic"])
def test_native_home_link_rejected_without_common_mutation(tmp_path, name, link):
    home = tmp_path / "home"
    target = home / name
    target.parent.mkdir(parents=True)
    common = tmp_path / "common"
    common.write_text('model = "exact"\n')
    before = (common.read_bytes(), common.stat().st_mode, common.stat().st_ino)
    if link == "hard":
        os.link(common, target)
    else:
        target.symlink_to(common)
    with pytest.raises(PermissionError):
        materialize_native_context(
            {"home_dir": str(home), "native_home": str(home)},
            {
                "codex_config_append": '[mcp_servers.fixture]\nurl="https://example.test/mcp"'
            },
        )
    assert (common.read_bytes(), common.stat().st_mode, common.stat().st_ino) == before


@pytest.mark.parametrize("link", ["hard", "symbolic"])
def test_atomic_target_substitution_preserves_common_inode(tmp_path, monkeypatch, link):
    home = tmp_path / "home"
    home.mkdir()
    common = tmp_path / "common"
    common.write_bytes(b"common")
    original = bootstrap.os.replace

    def replace(source, target, *, src_dir_fd, dst_dir_fd):
        if link == "hard":
            os.link(common, target, dst_dir_fd=dst_dir_fd)
        else:
            os.symlink(common, target, dir_fd=dst_dir_fd)
        return original(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(bootstrap.os, "replace", replace)
    bootstrap._write_private_native_file(home, Path("config"), b"private")
    assert common.read_bytes() == b"common"
    assert (home / "config").read_bytes() == b"private"
    assert (home / "config").stat().st_ino != common.stat().st_ino
    assert stat.S_IMODE((home / "config").stat().st_mode) == 0o600
    assert not list(home.glob(".native-config-*"))


def test_project_target_substitution_after_preflight_cannot_modify_common(
    tmp_path, monkeypatch
):
    private = tmp_path / "private"
    private.mkdir()
    common = tmp_path / "common"
    common.write_bytes(b"common")
    original = bootstrap.os.replace

    def replace(source, target, *, src_dir_fd, dst_dir_fd):
        if target == "AGENTS.md":
            # Case-insensitive filesystems already have the lowercase mirror.
            try:
                os.unlink(target, dir_fd=dst_dir_fd)
            except FileNotFoundError:
                pass
            os.link(common, target, dst_dir_fd=dst_dir_fd)
        return original(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(bootstrap.os, "replace", replace)
    bootstrap._write_claude_project_files(private, {}, private=True)
    assert common.read_bytes() == b"common"
    assert (private / "AGENTS.md").stat().st_ino != common.stat().st_ino
    assert stat.S_IMODE((private / "AGENTS.md").stat().st_mode) == 0o644


def test_normal_codex_merge_and_private_replacement_preserved(tmp_path):
    home = tmp_path / "home"
    config = home / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        'model = "exact"\n[mcp_servers.ambient]\nurl="https://ambient.test"\n'
    )
    before = config.stat().st_ino
    bundle = {"codex_config_append": '[mcp_servers.owner]\nurl="https://owner.test"\n'}
    placement = {"home_dir": str(home), "native_home": str(home)}
    materialize_native_context(placement, bundle)
    first = config.read_text()
    materialize_native_context(placement, bundle)
    assert config.read_text() == first
    assert (
        'model = "exact"' in first
        and "mcp_servers.ambient" in first
        and "mcp_servers.owner" in first
    )
    assert config.stat().st_ino != before
    assert json.loads((home / ".glasshive/native/managed-mcp.json").read_text()) == [
        "owner"
    ]
