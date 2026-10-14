from pathlib import Path

import pytest

from workers_projects_runtime import bootstrap, runtime_env
from workers_projects_runtime.upload_projection import project_upload_files


def upload_alias(tmp_path, monkeypatch):
    root = tmp_path / "managed-uploads"
    owner = root / "storage-owner"
    owner.mkdir(parents=True)
    alias = tmp_path / "checkout-uploads"
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(root))
    monkeypatch.setenv("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", "synthetic-test-secret")
    return root, owner, alias


def retained_entry(alias, *, ordinal=0):
    source = alias / "storage-owner" / f"file-{ordinal}__photo.jpg"
    return {
        "scope": "workspace", "path": f"uploads/photo-{ordinal}.jpg",
        "source_path": str(source), "file_id": f"file-{ordinal}",
        "filename": "photo.jpg", "media_group_index": ordinal,
        bootstrap.BOOTSTRAP_SOURCE_TOKEN_KEY: bootstrap.sign_bootstrap_source_path(
            source, tenant_id="tenant-a", owner_id="user-a",
        ),
    }


def test_retained_signed_root_alias_materializes_same_ordered_bytes(tmp_path, monkeypatch):
    root, owner, alias = upload_alias(tmp_path, monkeypatch)
    entries = []
    for ordinal, data in enumerate((b"first-image", b"second-image", b"third-image")):
        (owner / f"file-{ordinal}__photo.jpg").write_bytes(data)
        entries.append(retained_entry(alias, ordinal=ordinal))
    original = [dict(entry) for entry in entries]
    bootstrap.apply_bootstrap(
        home_dir=tmp_path / "home", workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker={"bootstrap_profile": "none", "tenant_id": "tenant-a", "owner_id": "user-a",
                "bootstrap_bundle_json": {"files": entries}},
        copy_file=lambda source, target: target.write_bytes(source.read_bytes()),
        copy_tree=lambda source, target: None,
    )
    assert [(tmp_path / "workspace" / entry["path"]).read_bytes() for entry in entries] == [
        b"first-image", b"second-image", b"third-image",
    ]
    assert entries == original  # Retained records, order, IDs and signatures stay intact.


@pytest.mark.parametrize("rejection", ["unsigned", "owner", "tenant", "missing", "leaf_symlink", "nested_symlink", "outside"])
def test_retained_reprojection_preserves_source_guards(tmp_path, monkeypatch, rejection):
    root, owner, alias = upload_alias(tmp_path, monkeypatch)
    file = owner / "file-0__photo.jpg"
    file.write_bytes(b"same-image")
    worker = {"tenant_id": "tenant-a", "owner_id": "user-a"}
    entry = retained_entry(alias)
    if rejection == "unsigned":
        entry.pop(bootstrap.BOOTSTRAP_SOURCE_TOKEN_KEY)
    elif rejection in {"owner", "tenant"}:
        worker[f"{rejection}_id"] = "foreign"
    elif rejection == "missing":
        file.unlink()
    elif rejection == "leaf_symlink":
        outside = tmp_path / "outside" / file.name
        outside.parent.mkdir()
        outside.write_bytes(b"same-image")
        file.unlink()
        file.symlink_to(outside)
    elif rejection == "nested_symlink":
        directory = root / "different-owner"
        directory.mkdir()
        (directory / file.name).write_bytes(b"same-image")
        file.unlink()
        owner.rmdir()
        owner.symlink_to(directory, target_is_directory=True)
    elif rejection == "outside":
        outside = tmp_path / "untrusted"
        outside.mkdir()
        (outside / "file-0__photo.jpg").write_bytes(b"same-image")
        source = outside / "file-0__photo.jpg"
        entry["source_path"] = str(source)
        entry[bootstrap.BOOTSTRAP_SOURCE_TOKEN_KEY] = bootstrap.sign_bootstrap_source_path(
            source, tenant_id="tenant-a", owner_id="user-a",
        )
    with pytest.raises((PermissionError, FileNotFoundError)):
        bootstrap.resolve_authorized_bootstrap_source_path(entry, entry["source_path"], worker)


@pytest.mark.parametrize("configured", ["unset", "missing", "alias"])
def test_runtime_root_repair_selects_canonical_declared_root(tmp_path, monkeypatch, configured):
    root, owner, alias = upload_alias(tmp_path, monkeypatch)
    fallback = tmp_path / "other-uploads"
    fallback.mkdir()
    monkeypatch.setattr(runtime_env, "_local_checkout_librechat_uploads_root", lambda: alias)
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(fallback))
    if configured == "unset":
        monkeypatch.delenv("WPR_LIBRECHAT_UPLOADS_ROOT")
    else:
        monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(alias if configured == "alias" else tmp_path / "missing"))
    loaded = {}
    runtime_env._repair_local_upload_roots(loaded)
    assert loaded["WPR_LIBRECHAT_UPLOADS_ROOT"] == str(root)
    assert str(root) in runtime_env.os.environ["WPR_BOOTSTRAP_SOURCE_ROOTS"].split(runtime_env.os.pathsep)
    assert str(alias) not in runtime_env.os.environ["WPR_BOOTSTRAP_SOURCE_ROOTS"].split(runtime_env.os.pathsep)
    (owner / "file-0__photo.jpg").write_bytes(b"new-image")
    projected = project_upload_files(
        {"request_files": [{"file_id": "file-0", "filename": "photo.jpg",
                            "filepath": "/uploads/storage-owner/file-0__photo.jpg"}]},
        tenant_id="tenant-a", owner_id="user-a", storage_owner_id="storage-owner",
    )
    assert bootstrap.resolve_authorized_bootstrap_source_path(
        projected[0], projected[0]["source_path"], {"tenant_id": "tenant-a", "owner_id": "user-a"},
    ) == owner / "file-0__photo.jpg"


def test_runtime_repair_keeps_explicit_upload_root_without_adding_unrelated_checkout(tmp_path, monkeypatch):
    root, owner, alias = upload_alias(tmp_path, monkeypatch)
    fallback = tmp_path / "other-checkout"
    fallback.mkdir()
    monkeypatch.setattr(runtime_env, "_local_checkout_librechat_uploads_root", lambda: fallback)
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(root))
    loaded = {}
    runtime_env._repair_local_upload_roots(loaded)
    assert loaded == {}
    assert runtime_env.os.environ["WPR_BOOTSTRAP_SOURCE_ROOTS"] == str(root)


def legacy_default_owner_token(source, owner_id):
    import hashlib
    import hmac
    import os
    message = "\0".join(("v1", str(source.absolute()), "", owner_id))
    return "v1:" + hmac.new(os.environ["GLASSHIVE_BOOTSTRAP_SOURCE_SECRET"].encode(),
                            message.encode(), hashlib.sha256).hexdigest()


def test_omitted_source_tenant_uses_worker_default_namespace(tmp_path, monkeypatch):
    _root, owner, _alias = upload_alias(tmp_path, monkeypatch)
    source = owner / "file-0__photo.jpg"
    source.write_bytes(b"original-upload")
    assert bootstrap.sign_bootstrap_source_path(source, owner_id="user-a") == \
        bootstrap.sign_bootstrap_source_path(source, tenant_id="local", owner_id="user-a")
    projected = project_upload_files(
        {"request_files": [{"file_id": "file-0", "filename": "photo.jpg",
                            "filepath": "/uploads/storage-owner/file-0__photo.jpg"}]},
        owner_id="user-a", storage_owner_id="storage-owner",
    )
    assert bootstrap.resolve_authorized_bootstrap_source_path(projected[0], source,
        {"tenant_id": "local", "owner_id": "user-a"}) == source


def test_legacy_default_owner_grant_reprojects_without_rewriting_record(tmp_path, monkeypatch):
    _root, owner, alias = upload_alias(tmp_path, monkeypatch)
    source = owner / "file-0__photo.jpg"
    source.write_bytes(b"original-upload")
    entry = retained_entry(alias)
    entry[bootstrap.BOOTSTRAP_SOURCE_TOKEN_KEY] = legacy_default_owner_token(
        Path(entry["source_path"]), "user-a")
    original = dict(entry)
    assert bootstrap.resolve_authorized_bootstrap_source_path(entry, entry["source_path"],
        {"tenant_id": "local", "owner_id": "user-a"}) == source
    assert entry == original


@pytest.mark.parametrize("rejection", ["nondefault_tenant", "foreign_owner", "ownerless", "forged", "changed_path"])
def test_legacy_default_source_grant_stays_owner_path_and_namespace_bound(tmp_path, monkeypatch, rejection):
    _root, owner, alias = upload_alias(tmp_path, monkeypatch)
    (owner / "file-0__photo.jpg").write_bytes(b"original-upload")
    entry = retained_entry(alias)
    token_owner = "" if rejection == "ownerless" else "user-a"
    entry[bootstrap.BOOTSTRAP_SOURCE_TOKEN_KEY] = legacy_default_owner_token(Path(entry["source_path"]), token_owner)
    worker = {"tenant_id": "local", "owner_id": "user-a"}
    if rejection == "nondefault_tenant": worker["tenant_id"] = "foreign-tenant"
    if rejection == "foreign_owner": worker["owner_id"] = "foreign-owner"
    if rejection == "ownerless": worker["owner_id"] = ""
    if rejection == "forged": entry[bootstrap.BOOTSTRAP_SOURCE_TOKEN_KEY] = "v1:" + "0" * 64
    if rejection == "changed_path":
        other = alias / "storage-owner" / "file-1__photo.jpg"
        (owner / other.name).write_bytes(b"other-upload")
        entry["source_path"] = str(other)
    with pytest.raises(PermissionError):
        bootstrap.resolve_authorized_bootstrap_source_path(entry, entry["source_path"], worker)


@pytest.mark.parametrize("binary", [False, True])
def test_legacy_default_source_context_and_native_copy_share_trust(tmp_path, monkeypatch, binary):
    from workers_projects_runtime.worker_configuration import WorkerConfiguration, WorkerConfig
    _root, owner, alias = upload_alias(tmp_path, monkeypatch)
    data = b"\x89PNG\r\noriginal-binary-upload" if binary else b"original text upload"
    (owner / "file-0__photo.jpg").write_bytes(data)
    entry = retained_entry(alias)
    entry[bootstrap.BOOTSTRAP_SOURCE_TOKEN_KEY] = legacy_default_owner_token(Path(entry["source_path"]), "user-a")
    worker = {"bootstrap_profile": "none", "profile": "codex-cli", "tenant_id": "local",
              "owner_id": "user-a", "bootstrap_bundle_json": {"files": [entry]}}
    reader = WorkerConfiguration.__new__(WorkerConfiguration)
    reader.source_resolver = None
    assert reader._effective(worker, WorkerConfig())[3]["issues"] == []
    bootstrap.apply_bootstrap(home_dir=tmp_path / "home", workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli", worker=worker,
        copy_file=lambda source, target: target.write_bytes(source.read_bytes()),
        copy_tree=lambda source, target: None)
    assert (tmp_path / "workspace" / entry["path"]).read_bytes() == data
