import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from workers_projects_runtime.store import Store
from workers_projects_runtime.workspace_files import WorkspaceFiles, FileAdmissionError
from workers_projects_runtime.personal_library import PersonalLibrary, LibraryError
from workers_projects_runtime.bootstrap_contributions import subtract_contribution


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setenv('WPR_BOOTSTRAP_SOURCE_ROOTS', str(tmp_path))
    monkeypatch.setenv('WPR_HOST_NATIVE_WEB_ACCESS', 'inherit')
    store = Store(tmp_path / 'state.db')
    project = store.create_project('owner', 'Example', 'Test saved material', 'claude-code')
    worker = store.create_worker(project['project_id'], 'owner', 'Researcher', 'assistant',
        'claude-code', 'local', 'claude', 'exact', workspace_root=str(tmp_path / 'workers'))
    root = tmp_path/'worker'; root.mkdir(parents=True, exist_ok=True)
    with store._connect() as conn:
        conn.execute('UPDATE workers SET workspace_dir=? WHERE worker_id=?',(str(root),worker['worker_id']))
    worker['workspace_dir']=str(root)
    files = WorkspaceFiles(store)
    service = SimpleNamespace(store=store, files=files, control_plane_store=None)
    yield PersonalLibrary(service), worker, root
    store.close()


def note(lib, text='Exact reference', key='save'):
    return lib.create_item('local', 'owner', {'name':'Notes','kind':'note','text':text}, key)


def ref(item):
    return {'item_id':item['item_id'],'version_id':item['version_id']}


def active_items(lib,worker):
    return [item for item in lib.list_worker_items('local','owner',worker['worker_id'])['items'] if item['active']]


def test_note_idempotence_owner_version_pin_and_context(library):
    lib, worker, root = library
    saved = note(lib)
    assert note(lib)['version_id'] == saved['version_id']
    with pytest.raises(LibraryError): note(lib, 'Different')
    with pytest.raises(LibraryError): lib.get_item('local','other',saved['item_id'])
    upload = saved['uploads'][0]
    with pytest.raises(FileAdmissionError, match='retained'):
        lib.files.move_upload('local','owner',upload['upload_id'],relative_path='changed.txt',revision=upload['revision'])
    with pytest.raises(FileAdmissionError): lib.files.cancel('local','owner',upload['upload_id'])
    context = lib.knowledge_context('local','owner',[ref(saved)])
    assert context['sources'][0]['loaded'] is True
    assert context['sources'][0]['text'] == 'Exact reference'
    assert lib.knowledge_context('local','owner',[ref(saved)],max_chars=1)['sources'][0]['loaded'] is False
    changed = lib.revise_item('local','owner',saved['item_id'],{'text':'Second'},saved['version_id'])
    assert changed['version'] == 2
    assert lib.get_item('local','owner',saved['item_id'],saved['version_id'])['version'] == 1
    with pytest.raises(LibraryError): lib.revise_item('local','owner',saved['item_id'],{'name':'Stale'},saved['version_id'])


def test_applied_versions_show_the_exact_revision_after_personal_item_changes(library):
    lib, worker, _ = library
    saved = note(lib)
    first = lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'pin-original')
    changed = lib.revise_item('local','owner',saved['item_id'],{'text':'Revised company method'},saved['version_id'])
    assert changed['version'] == 2
    listed = lib.list_worker_items('local','owner',worker['worker_id'])
    replayed = lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'pin-original')
    for result in (first, listed, replayed):
        assert result['items'][0]['version'] == 1
        assert result['items'][0]['version_id'] == saved['version_id']
    second = lib.apply_to_worker('local','owner',worker['worker_id'],[ref(changed)],'pin-revision')
    assert {(item['version_id'],item['version']) for item in second['items']} == {
        (saved['version_id'],1),(changed['version_id'],2)}
    with pytest.raises(FileAdmissionError):
        lib.list_worker_items('local','other',worker['worker_id'])


def test_unconfirmed_cleanup_stays_visible_when_its_version_is_pruned(library):
    lib, worker, _ = library
    saved = note(lib)
    applied = lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'cleanup-source')
    application_id = applied['items'][0]['application_id']
    with lib.store._connect() as conn:
        conn.execute("UPDATE personal_library_applications SET removed_at=1,undo_expires_at=1,cleanup_state='unconfirmed' WHERE application_id=?",(application_id,))
        conn.execute("DELETE FROM personal_library_versions WHERE version_id=?",(saved['version_id'],))
    row = lib.list_worker_items('local','owner',worker['worker_id'])['items'][0]
    assert row['application_id'] == application_id
    assert row['state'] == 'cleanup_unconfirmed'
    assert row['version'] is None
    assert row['version_id'] == saved['version_id']


def test_bundle_retains_deleted_child_and_new_direct_selection_rejects(library):
    lib, _, _ = library
    child = note(lib)
    bundle = lib.create_item('local','owner',{'name':'Pack','kind':'bundle','refs':[ref(child)]},'bundle')
    with pytest.raises(LibraryError): lib.delete_item('local','owner',child['item_id'])
    lib.delete_item('local','owner',child['item_id'],True)
    with pytest.raises(LibraryError): lib.resolve_refs('local','owner',[ref(child)])
    assert lib.resolve_refs('local','owner',[ref(bundle)])['upload_ids'] == [child['uploads'][0]['upload_id']]
    with pytest.raises(LibraryError): lib.create_item('local','owner',{'name':'Nested','kind':'bundle','refs':[ref(bundle)]},'nested')


def test_retained_template_context_survives_personal_delete(library):
    lib, _, _ = library
    saved = note(lib)
    resolved = lib.retain_refs('local','owner',[ref(saved)],'template','template-1')
    lib.delete_item('local','owner',saved['item_id'],True)
    assert lib.resolve_retained_refs('local','owner',[ref(saved)],'template','template-1')['upload_ids']
    with pytest.raises(LibraryError): lib.resolve_retained_refs('local','owner',[ref(saved)],'template','wrong')
    assert lib.knowledge_context('local','owner',resolved=resolved)['sources'][0]['text'] == 'Exact reference'
    with lib.store._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        lib.retain_refs('local','owner',[],'coordinator_turn','turn',connection=conn,resolved=resolved)
    lib.release_refs('local','owner','template','template-1')
    assert lib.resolve_retained_refs('local','owner',[ref(saved)],'coordinator_turn','turn')['upload_ids']


def test_generated_capture_is_exact_idempotent_and_source_checked(library):
    lib, worker, root = library
    (root/'result.txt').write_text('Generated exact bytes')
    entry = next(i for i in lib.files.list_files(worker['worker_id'],'local','owner')['items'] if i['name']=='result.txt')
    saved = lib.capture_file('local','owner',worker['worker_id'],entry['file_id'],entry['revision'],idempotency_key='capture')
    assert lib.knowledge_context('local','owner',[ref(saved)])['sources'][0]['text'] == 'Generated exact bytes'
    (root/'result.txt').unlink()
    assert lib.capture_file('local','owner',worker['worker_id'],entry['file_id'],entry['revision'],idempotency_key='capture')['item_id'] == saved['item_id']
    (root/'result.txt').write_text('Changed')
    with pytest.raises(FileAdmissionError):
        lib.capture_file('local','owner',worker['worker_id'],entry['file_id'],entry['revision'],idempotency_key='other')


def test_apply_knowledge_and_idle_remove_uses_trash_and_preserves_edits(library):
    lib, worker, root = library
    saved = note(lib)
    result = lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'apply')
    app = result['items'][0]
    assert app['state'] == 'in_files'
    assert 'prior' not in app['receipt']
    path = root/app['receipt']['manifest'][0]['path']
    assert path.read_text() == 'Exact reference'
    with lib.store._connect() as conn:
        bootstrap=json.loads(conn.execute('SELECT bootstrap_bundle_json FROM workers WHERE worker_id=?',(worker['worker_id'],)).fetchone()[0])
    assert bootstrap['files'][0]['context_delivery']=='lookup'
    assert len(lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'retry')['items'])==1
    result = lib.remove_from_worker('local','owner',worker['worker_id'],app['application_id'])
    assert not path.exists() and result['trash']
    assert not active_items(lib,worker)
    # A separate item has its own input identity and its edits are never destroyed.
    other=note(lib,'Other','save-other')
    app=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(other)],'apply-other')['items'][0]
    path=root/app['receipt']['manifest'][0]['path'];path.write_text('User edit')
    removed=lib.remove_from_worker('local','owner',worker['worker_id'],app['application_id'])
    assert path.read_text()=='User edit' and removed['retained']


def test_skill_full_text_validation_review_and_supported_start_receipt(library):
    lib, worker, root=library
    payload={'name':'Reporting','kind':'skill','profiles':['claude-code'],'files':[
        {'path':'.claude/skills/report/SKILL.md','content':'---\nname: report\ndescription: Report work\n---\nUse references/example.txt'},
        {'path':'.claude/skills/report/references/example.txt','content':'Exact supporting text'}]}
    item=lib.create_item('local','owner',payload,'skill')
    needs=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(item)],'prepare')
    assert needs['pending'] and needs['items'][0]['state']=='needs_review'
    result=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(item)],'confirm',True)
    assert result['items'][0]['state']=='ready_for_next_start'
    assert result['items'][0]['receipt']['loaded'] is False
    assert len(result['items'][0]['receipt']['paths'])==2
    assert 'prior' not in result['items'][0]['receipt']
    bad={**payload,'files':[{'path':'.claude/settings.local.json','content':'{}'}]}
    with pytest.raises(LibraryError):lib.create_item('local','owner',bad,'unsafe')


@pytest.mark.parametrize('policy,reason', [('full',None),('read_only','access policy'),('workspace','access policy'),('restricted','native tools'),('web_disabled','native web access')])
def test_skill_apply_uses_actual_native_launch_policy(library,monkeypatch,policy,reason):
    lib,worker,_=library
    bundle={}
    if policy in {'read_only','workspace'}:bundle['access_mode']=policy
    if policy=='restricted':bundle['provider_capabilities']={'native_tools':False}
    if policy=='web_disabled':monkeypatch.setenv('WPR_HOST_NATIVE_WEB_ACCESS','disabled')
    with lib.store._connect() as conn:
        conn.execute("UPDATE workers SET bootstrap_bundle_json=?,execution_mode='host' WHERE worker_id=?",(json.dumps(bundle),worker['worker_id']))
    saved=lib.create_item('local','owner',{'name':'Reporting','kind':'skill','files':[{'path':'.claude/skills/report/SKILL.md','content':'Exact skill text.'}]},'policy-skill')
    result=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'apply-policy',True)
    app=result['items'][0]
    assert app['state']==('ready_for_next_start' if reason is None else 'not_available')
    assert app['receipt']['loaded'] is False
    with lib.store._connect() as conn:
        applied=json.loads(conn.execute('SELECT bootstrap_bundle_json FROM workers WHERE worker_id=?',(worker['worker_id'],)).fetchone()[0])
    if reason:
        assert reason in app['receipt']['reason']
        assert not applied.get('files')
        assert result['pending'][0]['state']=='not_available'
    else:
        assert applied['files'][0]['content']=='Exact skill text.'
        assert not result['pending']


@pytest.mark.parametrize('policy', ['read_only','workspace','restricted','web_disabled'])
def test_reviewed_skill_policy_change_updates_listing_and_replay_without_uninstall(library,monkeypatch,policy):
    lib,worker,_=library
    with lib.store._connect() as conn:
        conn.execute("UPDATE workers SET execution_mode='host' WHERE worker_id=?",(worker['worker_id'],))
    saved=lib.create_item('local','owner',{'name':'Reporting','kind':'skill','files':[{'path':'.claude/skills/report/SKILL.md','content':'Exact reviewed skill.'}]},'reviewed-policy-skill')
    first=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'approved-policy',True)['items'][0]
    assert first['state']=='ready_for_next_start'
    with lib.store._connect() as conn:
        before=json.loads(conn.execute('SELECT bootstrap_bundle_json FROM workers WHERE worker_id=?',(worker['worker_id'],)).fetchone()[0])
        changed=dict(before)
        if policy in {'read_only','workspace'}:changed['access_mode']=policy
        if policy=='restricted':changed['provider_capabilities']={'native_tools':False}
        conn.execute('UPDATE workers SET bootstrap_bundle_json=? WHERE worker_id=?',(json.dumps(changed),worker['worker_id']))
    if policy=='web_disabled':monkeypatch.setenv('WPR_HOST_NATIVE_WEB_ACCESS','disabled')
    for result in [lib.list_worker_items('local','owner',worker['worker_id']),lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'approved-policy',True)]:
        app=result['items'][0]
        assert app['state']=='not_available'
        assert app['receipt']['loaded'] is False
        assert 'Not available' in app['receipt']['reason']
    with lib.store._connect() as conn:
        actual=json.loads(conn.execute('SELECT bootstrap_bundle_json FROM workers WHERE worker_id=?',(worker['worker_id'],)).fetchone()[0])
        row=conn.execute('SELECT state,receipt_json FROM personal_library_applications WHERE application_id=?',(first['application_id'],)).fetchone()
        assert row['state']=='ready_for_next_start'
        assert json.loads(row['receipt_json'])['applied']['files']==before['files']
        assert actual['files']==before['files']
        assert actual['library_selection']['items'][0]['state']=='not_available'
        assert actual['library_selection']['pending'][0]['status']=='not_available'
        conn.execute('UPDATE workers SET bootstrap_bundle_json=? WHERE worker_id=?',(json.dumps({**actual,'access_mode':'full','provider_capabilities':{}}),worker['worker_id']))
    monkeypatch.setenv('WPR_HOST_NATIVE_WEB_ACCESS','inherit')
    restored=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'approved-policy',True)
    assert restored['items'][0]['state']=='ready_for_next_start'
    assert restored['items'][0]['application_id']==first['application_id']
    assert not restored['pending']
    with lib.store._connect() as conn:
        actual=json.loads(conn.execute('SELECT bootstrap_bundle_json FROM workers WHERE worker_id=?',(worker['worker_id'],)).fetchone()[0])
        assert actual['files']==before['files']
        assert actual['library_selection']['items'][0]['state']=='ready_for_next_start'
        assert not actual['library_selection']['pending']


def test_policy_blocked_skill_requires_new_explicit_review_after_recovery(library,monkeypatch):
    lib,worker,root=library
    with lib.store._connect() as conn:
        conn.execute("UPDATE workers SET execution_mode='host' WHERE worker_id=?",(worker['worker_id'],))
    saved=lib.create_item('local','owner',{'name':'Reporting','kind':'skill','files':[{'path':'.claude/skills/report/SKILL.md','content':'Exact skill.'}]},'blocked-policy-skill')
    monkeypatch.setenv('WPR_HOST_NATIVE_WEB_ACCESS','disabled')
    blocked=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'blocked',True)
    assert blocked['items'][0]['state']=='not_available'
    assert 'native web access is disabled' in blocked['items'][0]['receipt']['reason']
    monkeypatch.setenv('WPR_HOST_NATIVE_WEB_ACCESS','inherit')
    for result in [lib.list_worker_items('local','owner',worker['worker_id']),lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'blocked',True)]:
        assert result['items'][0]['state']=='needs_review'
        assert result['items'][0]['receipt']['reason']=='Review this exact worker and skill files before adding'
        assert result['items'][0]['receipt']['loaded'] is False
    with lib.store._connect() as conn:
        actual=json.loads(conn.execute('SELECT bootstrap_bundle_json FROM workers WHERE worker_id=?',(worker['worker_id'],)).fetchone()[0])
        assert not actual.get('files')
        assert actual['library_selection']['items'][0]['state']=='needs_review'
        assert actual['library_selection']['pending'][0]['status']=='needs_review'
    assert not (root/'.claude/skills/report/SKILL.md').exists()
    recovered=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'review-after-recovery',True)
    assert recovered['items'][0]['state']=='ready_for_next_start'
    with lib.store._connect() as conn:
        actual=json.loads(conn.execute('SELECT bootstrap_bundle_json FROM workers WHERE worker_id=?',(worker['worker_id'],)).fetchone()[0])
        assert actual['files'][0]['content']=='Exact skill.'


@pytest.mark.parametrize('policy,reason',[('web_disabled','native web access is disabled'),('read_only','access policy'),('restricted','native tools are disabled')])
def test_unapproved_skill_keeps_current_denial_reason(library,monkeypatch,policy,reason):
    lib,worker,_=library
    with lib.store._connect() as conn:
        conn.execute("UPDATE workers SET execution_mode='host' WHERE worker_id=?",(worker['worker_id'],))
    saved=lib.create_item('local','owner',{'name':'Reporting','kind':'skill','files':[{'path':'.claude/skills/report/SKILL.md','content':'Exact skill.'}]},'unapproved-policy-skill')
    monkeypatch.setenv('WPR_HOST_NATIVE_WEB_ACCESS','disabled')
    lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'blocked',True)
    if policy!='web_disabled':
        monkeypatch.setenv('WPR_HOST_NATIVE_WEB_ACCESS','inherit')
        with lib.store._connect() as conn:
            bundle={'access_mode':'read_only'} if policy=='read_only' else {'provider_capabilities':{'native_tools':False}}
            conn.execute('UPDATE workers SET bootstrap_bundle_json=? WHERE worker_id=?',(json.dumps(bundle),worker['worker_id']))
    current=lib.list_worker_items('local','owner',worker['worker_id'])['items'][0]
    assert current['state']=='not_available'
    assert reason in current['receipt']['reason']
    assert current['receipt']['loaded'] is False
    with lib.store._connect() as conn:
        actual=json.loads(conn.execute('SELECT bootstrap_bundle_json FROM workers WHERE worker_id=?',(worker['worker_id'],)).fetchone()[0])
        assert not actual.get('files')


@pytest.mark.parametrize('unsupported',['profile','paths','clean_room','launch_authority'])
def test_policy_recovery_does_not_offer_review_for_unsupported_skill(library,monkeypatch,unsupported):
    lib,worker,_=library
    bundle={'execution_policy':'parallel-clean-room-v1'} if unsupported=='clean_room' else {'launch_authority':{}} if unsupported=='launch_authority' else {}
    if unsupported=='clean_room':
        from workers_projects_runtime.bootstrap import PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        bundle['execution_policy']=PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
    with lib.store._connect() as conn:
        conn.execute("UPDATE workers SET bootstrap_bundle_json=?,execution_mode='host' WHERE worker_id=?",(json.dumps(bundle),worker['worker_id']))
    saved=lib.create_item('local','owner',{'name':'Reporting','kind':'skill','profiles':['codex-cli'] if unsupported=='profile' else ['claude-code'],'files':[{'path':'references/SKILL.md' if unsupported=='paths' else '.claude/skills/report/SKILL.md','content':'Exact skill.'}]},'unsupported-policy-skill')
    monkeypatch.setenv('WPR_HOST_NATIVE_WEB_ACCESS','disabled')
    lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'blocked',True)
    monkeypatch.setenv('WPR_HOST_NATIVE_WEB_ACCESS','inherit')
    current=lib.list_worker_items('local','owner',worker['worker_id'])['items'][0]
    assert current['state']=='not_available'
    assert current['receipt']['reason']=='Not available for this worker type'
    assert current['receipt']['loaded'] is False


def test_host_web_policy_does_not_change_docker_project_skill_delivery(library,monkeypatch):
    lib,worker,_=library
    assert worker['execution_mode']=='docker'
    monkeypatch.setenv('WPR_HOST_NATIVE_WEB_ACCESS','disabled')
    saved=lib.create_item('local','owner',{'name':'Reporting','kind':'skill','files':[{'path':'.claude/skills/report/SKILL.md','content':'Exact Docker project skill.'}]},'docker-skill')
    result=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'docker-apply',True)
    assert result['items'][0]['state']=='ready_for_next_start'


def test_contribution_removal_keeps_independent_changes():
    before={'files':[{'path':'a.txt','content':'base'}], 'other':'original'}
    applied={'files':[{'path':'a.txt','content':'managed'},{'path':'b.txt','content':'B'}], 'other':'original'}
    current={**applied,'files':[*applied['files'],{'path':'user.txt','content':'user'}], 'other':'user change'}
    result, kept=subtract_contribution(current,before,applied)
    assert result=={'files':[{'path':'a.txt','content':'base'},{'path':'user.txt','content':'user'}], 'other':'user change'}
    assert not kept


def test_undo_restores_exact_files_binding_and_retained_deleted_item(library):
    lib, worker, root = library
    saved = note(lib)
    app = lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'apply')['items'][0]
    path = root/app['receipt']['manifest'][0]['path']
    removed = lib.remove_from_worker('local','owner',worker['worker_id'],app['application_id'])
    assert removed['undo_available'] and removed['undo_expires_at']
    lib.delete_item('local','owner',saved['item_id'],True)
    result = lib.restore_to_worker('local','owner',worker['worker_id'],app['application_id'])
    assert result['state']=='restored' and path.read_text()=='Exact reference'
    assert result['items'][0]['version_id']==saved['version_id']
    assert lib.restore_to_worker('local','owner',worker['worker_id'],app['application_id'])['state']=='already_restored'
    # Retrying an old add request cannot silently undo the user's later Remove.
    next_app=result['items'][0]
    lib.remove_from_worker('local','owner',worker['worker_id'],next_app['application_id'])
    replay=lib.apply_retained_to_worker('local','owner',worker['worker_id'],[ref(saved)],'apply',
        'removal_undo',next_app['application_id'])
    assert replay['replayed'] and not replay['items']
    assert not active_items(lib,worker)


def test_removed_association_undo_survives_reload_without_becoming_active(library):
    lib,worker,root=library
    saved=note(lib)
    app=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'apply')['items'][0]
    path=root/app['receipt']['manifest'][0]['path']
    removed=lib.remove_from_worker('local','owner',worker['worker_id'],app['application_id'])
    reloaded=PersonalLibrary(lib.service)
    row=reloaded.list_worker_items('local','owner',worker['worker_id'])['items'][0]
    assert row['state']=='removed' and row['active'] is False and row['undo_available'] is True
    assert row['undo_expires_at']==removed['undo_expires_at'] and not path.exists()
    assert not json.loads(lib.store.get_worker(worker['worker_id'])['bootstrap_bundle_json'])['library_selection']['items']
    restored=reloaded.restore_to_worker('local','owner',worker['worker_id'],row['application_id'])
    assert len(restored['items'])==1 and restored['items'][0]['active'] is True
    assert path.read_text()=='Exact reference'


def test_removal_undo_expiry_releases_only_its_pins_and_deleted_bodies(library,monkeypatch):
    lib, worker, _ = library
    saved = note(lib)
    lib.retain_refs('local','owner',[ref(saved)],'template','retained-template')
    app = lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'apply')['items'][0]
    removed=lib.remove_from_worker('local','owner',worker['worker_id'],app['application_id'])
    lib.delete_item('local','owner',saved['item_id'],True)
    monkeypatch.setattr('workers_projects_runtime.personal_library.time.time',lambda:removed['undo_expires_at']+1)
    lib.list_worker_items('local','owner',worker['worker_id'])
    assert not lib.list_worker_items('local','owner',worker['worker_id'])['items']
    with lib.store._connect() as conn:
        assert not conn.execute("SELECT 1 FROM personal_library_holders WHERE holder_kind='removal_undo'").fetchone()
        assert conn.execute("SELECT 1 FROM personal_library_holders WHERE holder_kind='template'").fetchone()
    with pytest.raises(LibraryError,match='expired'):
        lib.restore_to_worker('local','owner',worker['worker_id'],app['application_id'])
    lib.release_refs('local','owner','template','retained-template')
    with lib.store._connect() as conn:
        assert not conn.execute('SELECT 1 FROM personal_library_versions WHERE version_id=?',(saved['version_id'],)).fetchone()


def test_name_only_revisions_keep_original_material_and_list_is_metadata_only(library):
    lib, _, _=library
    saved=note(lib)
    renamed=lib.revise_item('local','owner',saved['item_id'],{'name':'Renamed'},saved['version_id'])
    assert renamed['kind']=='note' and renamed['uploads']==saved['uploads']
    assert lib.get_item('local','owner',saved['item_id'])['payload']['text']=='Exact reference'
    content=lib.revise_item('local','owner',saved['item_id'],{'content':'Content edit'},renamed['version_id'])
    assert lib.get_item('local','owner',saved['item_id'])['payload']['text']=='Content edit'
    bundle=lib.create_item('local','owner',{'name':'Pack','kind':'bundle','refs':[ref(content)]},'bundle')
    lib.delete_item('local','owner',saved['item_id'],True)
    renamed_bundle=lib.revise_item('local','owner',bundle['item_id'],{'name':'Renamed pack'},bundle['version_id'])
    assert lib.resolve_refs('local','owner',[ref(renamed_bundle)])['upload_ids']==[content['uploads'][0]['upload_id']]
    skill=lib.create_item('local','owner',{'name':'Reporting','kind':'skill','files':[{'path':'.claude/skills/report/SKILL.md','content':'Follow this exact skill.'}]},'skill')
    renamed_skill=lib.revise_item('local','owner',skill['item_id'],{'name':'Renamed skill'},skill['version_id'])
    assert renamed_skill['payload']['files']==skill['payload']['files']
    assert all('files' not in item['payload'] for item in lib.list_items('local','owner')['items'])
    native=lib.create_item('local','owner',{'name':'Native','kind':'native','source_uri':'https://example.invalid/package'},'native')
    assert lib.revise_item('local','owner',native['item_id'],{'name':'Renamed native'},native['version_id'])['payload']['source_uri']==native['payload']['source_uri']
    with pytest.raises(LibraryError):
        lib.create_item('local','owner',{'name':'Unsafe','kind':'native','source_uri':'https://example.invalid/package?token=secret'},'unsafe')


def test_inline_package_uses_existing_owner_quota(library):
    lib, _, _=library
    lib.files.set_policy('local','owner',{'storage_limit_bytes':256})
    with pytest.raises(LibraryError,match='Storage limit'):
        lib.create_item('local','owner',{'name':'Large','kind':'skill','files':[{'path':'.claude/skills/report/SKILL.md','content':'x'*1024}]},'large')
    assert not lib.list_items('local','owner')['items']


def test_shared_native_package_removal_and_undo_keep_other_active_item(library):
    lib,worker,root=library
    path='.claude/skills/report/SKILL.md'
    payload={'name':'First','kind':'skill','files':[{'path':path,'content':'Shared instructions'}]}
    first=lib.create_item('local','owner',payload,'first')
    second=lib.create_item('local','owner',{**payload,'name':'Second'},'second')
    lib.apply_to_worker('local','owner',worker['worker_id'],[ref(first)],'first-apply',True)
    apps=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(second)],'second-apply',True)['items']
    apps={app['item_id']:app for app in apps}
    target=root/path;target.parent.mkdir(parents=True);target.write_text('Shared instructions')
    removed=lib.remove_from_worker('local','owner',worker['worker_id'],apps[first['item_id']]['application_id'])
    assert target.read_text()=='Shared instructions' and not removed['removed_native_files']
    removed_last=lib.remove_from_worker('local','owner',worker['worker_id'],apps[second['item_id']]['application_id'])
    assert removed_last['removed_native_files'] and not target.exists()
    restored=lib.restore_to_worker('local','owner',worker['worker_id'],apps[second['item_id']]['application_id'])
    assert restored['items'][0]['state']=='ready_for_next_start'


def test_remove_recovers_existing_file_journal_after_interrupted_receipt(library,monkeypatch):
    from workers_projects_runtime.workspace_file_mutations import FileMutations
    lib,worker,root=library
    saved=note(lib)
    app=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'apply')['items'][0]
    target=root/app['receipt']['manifest'][0]['path']
    original=FileMutations._mutate_locked
    def interrupted(self,*args,**kwargs):
        original(self,*args,**kwargs)
        raise RuntimeError('Synthetic crash after durable move')
    monkeypatch.setattr(FileMutations,'_mutate_locked',interrupted)
    with pytest.raises(RuntimeError):
        lib.remove_from_worker('local','owner',worker['worker_id'],app['application_id'])
    assert not target.exists()
    monkeypatch.setattr(FileMutations,'_mutate_locked',original)
    removed=lib.remove_from_worker('local','owner',worker['worker_id'],app['application_id'])
    assert len(removed['trash'])==1
    replay=lib.remove_from_worker('local','owner',worker['worker_id'],app['application_id'])
    assert replay['replayed'] and replay['trash']==removed['trash']
    lib.restore_to_worker('local','owner',worker['worker_id'],app['application_id'])
    assert target.read_text()=='Exact reference'


def test_native_cleanup_failure_is_retryable_and_preserves_user_edit(library,monkeypatch):
    from workers_projects_runtime import bootstrap
    lib,worker,root=library
    path='.claude/skills/report/SKILL.md'
    item=lib.create_item('local','owner',{'name':'Report','kind':'skill','files':[{'path':path,'content':'Instructions'}]},'skill')
    app=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(item)],'apply',True)['items'][0]
    target=root/path;target.parent.mkdir(parents=True);target.write_text('Instructions')
    original=bootstrap.remove_projected_library_files
    def failed(*args):raise OSError('Synthetic filesystem unavailable')
    monkeypatch.setattr(bootstrap,'remove_projected_library_files',failed)
    removed=lib.remove_from_worker('local','owner',worker['worker_id'],app['application_id'])
    assert removed['native_cleanup']['state']=='unconfirmed' and target.exists()
    target.write_text('User edit')
    monkeypatch.setattr(bootstrap,'remove_projected_library_files',original)
    replay=lib.remove_from_worker('local','owner',worker['worker_id'],app['application_id'])
    assert replay['native_cleanup']['retained'] and target.read_text()=='User edit'


@pytest.mark.parametrize('shared',[False,True])
def test_remove_only_preserves_other_binding_of_same_physical_copy(library,shared):
    lib,worker,root=library
    other=lib.store.create_worker(worker['project_id'],'owner','Second','assistant','claude-code','local','claude','exact',workspace_root=str(root.parent/'workers'))
    other_root=root if shared else root.parent/'other'
    other_root.mkdir(exist_ok=True)
    lib.store.update_worker(other['worker_id'],workspace_dir=str(other_root))
    if shared:
        lib.files.scope_resolver=lambda worker_id,**kwargs:{'scope_id':'shared-scope','kind':'workspace','root':root}
    saved=note(lib)
    first=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'first')['items'][0]
    second=lib.apply_to_worker('local','owner',other['worker_id'],[ref(saved)],'second')['items'][0]
    first_path=root/first['receipt']['manifest'][0]['path']
    second_path=other_root/second['receipt']['manifest'][0]['path']
    result=lib.remove_from_worker('local','owner',worker['worker_id'],first['application_id'])
    assert first_path.exists() is shared
    assert second_path.read_text()=='Exact reference'
    assert bool(result['retained']) is shared


def test_failed_note_save_rolls_back_own_upload_and_same_request_can_retry(library):
    lib,_,_=library
    lib.files.set_policy('local','owner',{'storage_limit_bytes':256})
    with pytest.raises(LibraryError,match='Storage limit'):
        note(lib,'x'*80)
    with lib.store._connect() as conn:
        assert not conn.execute("SELECT 1 FROM workspace_file_uploads WHERE state='ready'").fetchone()
    assert not lib.list_items('local','owner')['items']
    lib.files.set_policy('local','owner',{'storage_limit_bytes':8192},administrator=True)
    saved=note(lib,'x'*80)
    assert lib.get_item('local','owner',saved['item_id'])['payload']['text']=='x'*80
    assert note(lib,'x'*80)['version_id']==saved['version_id']


def test_stale_note_edit_never_uploads_and_failed_capture_cleans_only_new_bytes(library,monkeypatch):
    lib,worker,root=library
    saved=note(lib)
    revised=lib.revise_item('local','owner',saved['item_id'],{'text':'New'},saved['version_id'])
    with lib.store._connect() as conn:
        before=conn.execute('SELECT COUNT(*) FROM workspace_file_uploads').fetchone()[0]
    with pytest.raises(LibraryError):
        lib.revise_item('local','owner',saved['item_id'],{'text':'Stale'},saved['version_id'])
    with lib.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM workspace_file_uploads').fetchone()[0]==before
    (root/'result.txt').write_text('Generated')
    entry=next(item for item in lib.files.list_files(worker['worker_id'],'local','owner')['items'] if item['name']=='result.txt')
    with pytest.raises(LibraryError):
        lib.capture_file('local','owner',worker['worker_id'],entry['file_id'],entry['revision'],name='x'*121,idempotency_key='bad-capture')
    with lib.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM workspace_file_uploads WHERE state='ready'").fetchone()[0]==2
    assert lib.get_item('local','owner',revised['item_id'])['payload']['text']=='New'


@pytest.mark.parametrize('payload',[
    {'name':'Bad','kind':'skill','profiles':[{}],'files':[]},
    {'name':'Bad','kind':'native','source_uri':'https://['},
    {'name':'Bad','kind':'native','source_uri':'https:///missing-host'},
    {'name':'Bad','kind':'knowledge','upload_ids':[{}],'upload_revisions':['x']},
    {'name':'Bad','kind':'knowledge','uploads':[3]},
    {'name':{},'kind':'note','text':'No'},
])
def test_malformed_material_has_clear_validation_error(library,payload):
    lib,_,_=library
    with pytest.raises(LibraryError) as error:
        lib.create_item('local','owner',payload,'malformed')
    assert error.value.status_code==422


def test_source_only_setup_reference_is_visible_without_claiming_install(library):
    lib,worker,_=library
    item=lib.create_item('local','owner',{'name':'Native source','kind':'native','source_uri':'https://example.invalid/package'},'native')
    app=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(item)],'apply')['items'][0]
    current=lib.store.get_worker(worker['worker_id'])
    selection=json.loads(current['bootstrap_bundle_json'])['library_selection']
    assert selection['refs']==[ref(item)]
    assert selection['items'][0]['source_uri']=='https://example.invalid/package'
    assert selection['items'][0]['state']=='needs_setup'
    assert selection['pending'][0]['source_uri']=='https://example.invalid/package'
    assert not app['receipt']['loaded']
    lib.remove_from_worker('local','owner',worker['worker_id'],app['application_id'])
    assert not json.loads(lib.store.get_worker(worker['worker_id'])['bootstrap_bundle_json'])['library_selection']['items']


def test_compact_selection_and_picker_never_reparse_saved_skill_bodies(library,monkeypatch):
    from workers_projects_runtime import personal_library as module
    lib,worker,_=library
    body='Saved body sentinel.\n'+'x'*(800*1024)
    skill=lib.create_item('local','owner',{'name':'Large package','kind':'skill','files':[
        {'path':f'.claude/skills/package/reference-{index}.txt','content':body} for index in range(3)]},'large')
    lib.apply_to_worker('local','owner',worker['worker_id'],[ref(skill)],'apply',True)
    original=module.json.loads
    def metadata_only(value,*args,**kwargs):
        assert not isinstance(value,str) or 'Saved body sentinel' not in value
        return original(value,*args,**kwargs)
    monkeypatch.setattr(module.json,'loads',metadata_only)
    listed=lib.list_items('local','owner')['items'][0]
    assert listed['payload']['files_count']==3 and 'files' not in listed['payload']
    resolved=lib.resolve_refs('local','owner',[ref(skill)])
    compact=lib.compact_selection(resolved)
    assert len(json.dumps(compact))<4096 and 'payload' not in compact['items'][0]
    lib.retain_refs('local','owner',[ref(skill)],'coordinator_turn','exact-turn',resolved=compact)
    assert lib.list_worker_items('local','owner',worker['worker_id'])['items'][0]['state']=='ready_for_next_start'


def test_capture_from_closed_workspace_is_read_only_and_exact(library):
    lib,worker,root=library
    (root/'finished.txt').write_text('Saved completed output')
    entry=next(item for item in lib.files.list_files(worker['worker_id'],'local','owner')['items'] if item['name']=='finished.txt')
    lib.store.update_worker(worker['worker_id'],state='terminated')
    saved=lib.capture_file('local','owner',worker['worker_id'],entry['file_id'],entry['revision'],idempotency_key='closed-capture')
    assert lib.get_item('local','owner',saved['item_id'])['uploads']
    assert lib.store.get_worker(worker['worker_id'])['state']=='terminated'


def test_get_item_exposes_exact_holders_before_delete(library):
    lib,worker,_=library
    saved=note(lib)
    bundle=lib.create_item('local','owner',{'name':'Onboarding pack','kind':'bundle','refs':[ref(saved)]},'bundle')
    lib.retain_refs('local','owner',[ref(saved)],'template','template-id')
    lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'apply')
    holders=lib.get_item('local','owner',saved['item_id'])['holders']
    assert {item['holder_kind'] for item in holders}=={'bundle','template','application'}
    assert all(item['version_id']==saved['version_id'] for item in holders)
    assert any(item['label']=='Bundle: Onboarding pack' for item in holders)
    assert lib.get_item('local','owner',bundle['item_id'])['state']=='active'


def test_remove_parses_same_scope_bindings_once_per_application(library,monkeypatch):
    lib,worker,_=library
    notes=[note(lib,str(index),'note-'+str(index)) for index in range(4)]
    item=lib.create_item('local','owner',{'name':'Four sources','kind':'knowledge','upload_ids':[item['uploads'][0]['upload_id'] for item in notes],'upload_revisions':[item['uploads'][0]['revision'] for item in notes]},'sources')
    app=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(item)],'apply')['items'][0]
    original=lib._other_binding_paths;calls=[]
    def counted(*args):calls.append(1);return original(*args)
    monkeypatch.setattr(lib,'_other_binding_paths',counted)
    removed=lib.remove_from_worker('local','owner',worker['worker_id'],app['application_id'])
    assert len(removed['trash'])==4 and len(calls)==1


def test_cleanup_failure_is_visible_after_reload_and_retry_clears_it(library,monkeypatch):
    from workers_projects_runtime import bootstrap
    lib,worker,root=library
    path='.claude/skills/report/SKILL.md'
    item=lib.create_item('local','owner',{'name':'Report','kind':'skill','files':[{'path':path,'content':'Instructions'}]},'skill')
    app=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(item)],'apply',True)['items'][0]
    target=root/path;target.parent.mkdir(parents=True);target.write_text('Instructions')
    original=bootstrap.remove_projected_library_files
    def unavailable(*args):raise OSError('Synthetic failure')
    monkeypatch.setattr(bootstrap,'remove_projected_library_files',unavailable)
    lib.remove_from_worker('local','owner',worker['worker_id'],app['application_id'])
    reloaded=PersonalLibrary(lib.service).list_worker_items('local','owner',worker['worker_id'])['items']
    assert reloaded[0]['state']=='cleanup_unconfirmed'
    assert reloaded[0]['receipt']['native_cleanup']['state']=='unconfirmed'
    monkeypatch.setattr(bootstrap,'remove_projected_library_files',original)
    lib.remove_from_worker('local','owner',worker['worker_id'],app['application_id'])
    assert not active_items(lib,worker)


def test_approved_ref_tracks_grant_and_remove_revokes_actual_capability(library):
    from workers_projects_runtime.control_plane import ControlPlaneStore
    from library_test_support import library_manifest,register_manifest
    lib,worker,root=library
    cp=ControlPlaneStore(str(lib.store.db_path));lib.service.control_plane_store=cp
    path='.claude/skills/approved/SKILL.md'
    catalog=register_manifest(cp,library_manifest(stable_id='skill.synthetic.approved',profiles=['claude-code'],files=[{'path':path,'content':'Approved instructions'}]))
    item=lib.create_item('local','owner',{'name':'Approved reference','kind':'approved','library_id':catalog['library_id']},'approved')
    app=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(item)],'select')['items'][0]
    assert app['state']=='needs_review'
    pending=cp.create_pending_change(tenant_id='local',owner_id='owner',change_type='library_enable',target_id=worker['worker_id'],payload={'library_id':catalog['library_id']},ttl_seconds=300)
    grant=cp.confirm_pending_change(change_id=pending['change_id'],tenant_id='local',owner_id='owner',confirmation_token=pending['confirmation_token'])['applied']
    # The existing grant transaction also updates the model-visible selection.
    selection=json.loads(lib.store.get_worker(worker['worker_id'])['bootstrap_bundle_json'])['library_selection']
    assert selection['items'][0]['state']=='enabled' and not selection['pending']
    enabled=lib.list_worker_items('local','owner',worker['worker_id'])['items'][0]
    assert enabled['state']=='enabled' and enabled['receipt']['grant_id']==grant['grant_id']
    second=lib.create_item('local','owner',{'name':'Same approved capability','kind':'approved','library_id':catalog['library_id']},'second-reference')
    selected=lib.apply_to_worker('local','owner',worker['worker_id'],[ref(second)],'select-already-enabled')
    assert selected['state']=='available' and not selected['pending']
    second_app=next(item for item in selected['items'] if item['item_id']==second['item_id'])
    target=root/path;target.parent.mkdir(parents=True);target.write_text('Approved instructions')
    removed=lib.remove_from_worker('local','owner',worker['worker_id'],app['application_id'])
    assert removed['retained_capabilities'] and target.exists()
    assert cp.list_workspace_grants(tenant_id='local',owner_id='owner',worker_id=worker['worker_id'])
    lib.remove_from_worker('local','owner',worker['worker_id'],second_app['application_id'])
    assert not cp.list_workspace_grants(tenant_id='local',owner_id='owner',worker_id=worker['worker_id'])
    assert not target.exists()


def test_closed_workspace_library_read_preserves_mutation_guard(library):
    lib, worker, _ = library
    saved = note(lib)
    added = lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'apply')
    application_id = added['items'][0]['application_id']
    lib.store.update_worker(worker['worker_id'],state='terminated')
    listed = lib.list_worker_items('local','owner',worker['worker_id'])
    assert listed['items'][0]['application_id'] == application_id
    with pytest.raises(FileAdmissionError):
        lib.list_worker_items('local','other',worker['worker_id'])
    with pytest.raises(FileAdmissionError, match='closed'):
        lib.apply_to_worker('local','owner',worker['worker_id'],[ref(saved)],'closed-apply')
    with pytest.raises(FileAdmissionError, match='closed'):
        lib.remove_from_worker('local','owner',worker['worker_id'],application_id)
    with pytest.raises(FileAdmissionError, match='closed'):
        lib.restore_to_worker('local','owner',worker['worker_id'],application_id)
    assert lib.store.get_worker(worker['worker_id'])['state'] == 'terminated'
