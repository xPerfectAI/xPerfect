from pathlib import Path
import os
import pytest
from workers_projects_runtime.control_plane import ControlPlaneError
from workers_projects_runtime.provider_accounts import ProviderAccountHomeManager

@pytest.fixture
def account(tmp_path, monkeypatch):
    root=tmp_path.resolve()
    binary=root/'trusted-codex';binary.write_text('synthetic executable');binary.chmod(0o755)
    monkeypatch.setattr('workers_projects_runtime.provider_accounts.provider_setup_binary',lambda _:str(binary))
    homes=ProviderAccountHomeManager(root/'accounts')
    home=homes.ensure_home(tenant_id='tenant',owner_id='owner',account_id='acct_test',provider='codex')
    helpers=home/'codex/tmp/arg0/codex-arg0Ab12cd';helpers.mkdir(parents=True,mode=0o700)
    auth=home/'codex/auth.json';auth.write_text('synthetic auth');auth.chmod(0o644)
    return homes,home,helpers,binary,auth


def test_declared_codex_executable_aliases_do_not_block_credential_sealing(account):
    homes,home,helpers,binary,auth=account
    for name in ('apply_patch','applypatch','codex-execve-wrapper','codex-linux-sandbox'):
        (helpers/name).symlink_to(binary)
    before=(binary.stat().st_mode,binary.read_bytes())
    homes.tighten_permissions(account_home=home)
    assert auth.stat().st_mode & 0o777 == 0o600
    assert (binary.stat().st_mode,binary.read_bytes()) == before
    assert all(p.is_symlink() for p in helpers.iterdir())


def test_codex_npm_launcher_accepts_only_its_native_vendor_alias(account, monkeypatch, tmp_path):
    homes, home, helpers, _, auth = account
    package = tmp_path / 'npm/@openai/codex'
    launcher = package / 'bin/codex.js'
    launcher.parent.mkdir(parents=True)
    launcher.write_text('synthetic launcher')
    native = package / 'node_modules/@openai/codex-linux-arm64/vendor/aarch64-unknown-linux-musl/bin/codex'
    native.parent.mkdir(parents=True)
    native.write_text('synthetic native executable')
    native.chmod(0o755)
    monkeypatch.setattr('workers_projects_runtime.provider_accounts.provider_setup_binary', lambda _: str(launcher))
    (helpers / 'apply_patch').symlink_to(native)
    homes.tighten_permissions(account_home=home)
    assert auth.stat().st_mode & 0o777 == 0o600
    assert (helpers / 'apply_patch').is_symlink()
    outside = tmp_path / 'rogue-codex'
    outside.write_text('synthetic rogue executable')
    outside.chmod(0o755)
    (helpers / 'apply_patch').unlink()
    (helpers / 'apply_patch').symlink_to(outside)
    with pytest.raises(ControlPlaneError, match='unsafe link'):
        homes.tighten_permissions(account_home=home)


def test_product_selected_symlinked_codex_launcher_is_the_only_accepted_alias(
    account, monkeypatch, tmp_path
):
    homes, home, helpers, binary, auth = account
    launcher = tmp_path / 'selected-codex'
    launcher.symlink_to(binary)
    monkeypatch.setattr(
        'workers_projects_runtime.provider_accounts.provider_setup_binary',
        lambda _: str(launcher),
    )
    helper = helpers / 'apply_patch'
    helper.symlink_to(launcher)
    homes.tighten_permissions(account_home=home)
    assert auth.stat().st_mode & 0o777 == 0o600
    assert helper.is_symlink()

    alternate = tmp_path / 'other-codex-alias'
    alternate.symlink_to(binary)
    helper.unlink()
    helper.symlink_to(alternate)
    with pytest.raises(ControlPlaneError, match='unsafe link'):
        homes.tighten_permissions(account_home=home)


@pytest.mark.parametrize('kind',['credential','other-directory','other-name','wrong-target','relative-target','session-name','provider-directory'])
def test_adjacent_links_remain_rejected_without_touching_target(account,kind):
    homes,home,helpers,binary,auth=account
    path=helpers/'apply_patch';target=binary
    if kind=='credential':auth.unlink();path=auth
    elif kind=='other-directory':path=home/'codex/apply_patch'
    elif kind=='other-name':path=helpers/'auth.json'
    elif kind=='wrong-target':target=home.parent/'outside';target.write_text('untouched');target.chmod(0o644)
    elif kind=='relative-target':target=Path(os.path.relpath(binary,helpers))
    elif kind=='session-name':path=helpers.parent/'unrecognized/apply_patch';path.parent.mkdir()
    elif kind=='provider-directory':path=home/'claude/tmp/arg0/codex-arg0Ab12cd/apply_patch';path.parent.mkdir(parents=True)
    path.symlink_to(target)
    before=(binary.stat().st_mode,binary.read_bytes())
    with pytest.raises(ControlPlaneError,match='unsafe link'):homes.tighten_permissions(account_home=home)
    assert (binary.stat().st_mode,binary.read_bytes())==before
    assert path.is_symlink()
