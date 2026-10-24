"""Useful display descriptions do not rewrite accepted Library material."""
import json

import pytest

from test_personal_library import library, ref
from workers_projects_runtime.personal_library import LibraryError, _skill_description


def skill_payload(header='description: Check assay data against the supplied method.'):
    return {'name': 'Assay quality', 'kind': 'skill', 'files': [
        {'path': '.claude/skills/assay/SKILL.md', 'content': f'---\nname: assay\n{header}\n---\nPRIVATE BODY MUST NOT ENTER CATALOG'}]}


@pytest.mark.parametrize('header,expected', [
    ('description: Check assay data. Explain exceptions.', 'Check assay data.'),
    ('description: "Check assay data: control quality."', 'Check assay data: control quality.'),
    ("description: 'Check the company''s method.'", "Check the company's method."),
    ('description: >-\n  Check assay data against\n  the supplied method.\nlicense: Apache-2.0', 'Check assay data against the supplied method.'),
    ('description: |\n  Check supplied assay data.\n  Explain exceptions.', 'Check supplied assay data.'),
])
def test_skill_description_reads_only_declared_bounded_header(header, expected):
    assert _skill_description(skill_payload(header)['files']) == expected


def test_missing_or_unclosed_header_never_promotes_instruction_body():
    for content in ['description: Body text is not frontmatter', '---\nname: assay\n---\ndescription: body only', '---\ndescription: incomplete header']:
        assert _skill_description([{'path': '.claude/skills/assay/SKILL.md', 'content': content}]) == ''
    assert len(_skill_description(skill_payload('description: '+ 'long phrase ' * 100)['files'])) <= 280


def test_optional_description_create_revise_validation_and_version_identity(library):
    lib, _, _ = library
    saved=lib.create_item('local','owner',{**skill_payload(), 'description':'Explain assay uncertainty and review gaps.'},'described')
    assert lib.list_items('local','owner')['items'][0]['payload']['description']=='Explain assay uncertainty and review gaps.'
    changed=lib.revise_item('local','owner',saved['item_id'],{'description':'Trace sample quality and missing controls.'},saved['version_id'])
    assert changed['version_id'] != saved['version_id']
    assert lib.get_item('local','owner',saved['item_id'],saved['version_id'])['payload']['description']=='Explain assay uncertainty and review gaps.'
    for description in [None, 8, 'x'*301]:
        with pytest.raises(LibraryError, match='description'):
            lib.revise_item('local','owner',saved['item_id'],{'description':description},changed['version_id'])
    assert lib.get_item('local','owner',saved['item_id'])['version_id']==changed['version_id']


def test_legacy_cards_project_descriptions_once_without_changing_exact_payload_or_refs(library, monkeypatch):
    from workers_projects_runtime import workspace_file_drafts
    lib,worker,_=library
    note=lib.create_item('local','owner',{'name':'Study methods','kind':'note','text':'Review the supplied study plan and data. PRIVATE REMAINDER'},'note-description')
    skill=lib.create_item('local','owner',skill_payload(),'skill-description')
    lib.apply_to_worker('local','owner',worker['worker_id'],[ref(note),ref(skill)],'description-apply',True)
    before=lib.store.get_worker(worker['worker_id'])['bootstrap_bundle_json']
    with lib.store._connect() as conn:
        payloads={row['version_id']:row['payload_json'] for row in conn.execute('SELECT version_id,payload_json FROM personal_library_versions')}
        # Existing libraries predate the display metadata field.
        for row in conn.execute('SELECT version_id,metadata_json FROM personal_library_versions').fetchall():
            metadata=json.loads(row['metadata_json']);metadata.pop('description',None)
            conn.execute('UPDATE personal_library_versions SET metadata_json=? WHERE version_id=?',(json.dumps(metadata),row['version_id']))
    listed={item['kind']:item for item in lib.list_items('local','owner')['items']}
    assert listed['note']['payload']['description']=='Review the supplied study plan and data.'
    assert listed['skill']['payload']['description']=='Check assay data against the supplied method.'
    assert 'PRIVATE' not in json.dumps(list(listed.values()))
    with lib.store._connect() as conn:
        assert {row['version_id']:row['payload_json'] for row in conn.execute('SELECT version_id,payload_json FROM personal_library_versions')}==payloads
    assert lib.store.get_worker(worker['worker_id'])['bootstrap_bundle_json']==before
    assert ref(listed['note'])==ref(note) and ref(listed['skill'])==ref(skill)
    monkeypatch.setattr(workspace_file_drafts,'DraftDownload',lambda *a,**k:pytest.fail('A cached description must not reread the note'))
    assert len(lib.list_items('local','owner')['items'])==2
    assert not lib.list_items('local','another-owner')['items']


def test_new_skill_catalog_and_revised_skill_use_headers_without_exposing_body(library):
    lib,_,_=library
    saved=lib.create_item('local','owner',skill_payload(),'description-new')
    first=lib.list_items('local','owner')['items'][0]
    assert first['payload']['description']=='Check assay data against the supplied method.'
    assert 'files' not in first['payload']
    lib.revise_item('local','owner',saved['item_id'],{'files':skill_payload('description: Reconcile specimen identities before analysis.')['files']},saved['version_id'])
    assert lib.list_items('local','owner')['items'][0]['payload']['description']=='Reconcile specimen identities before analysis.'


def test_note_revisions_refresh_derived_description_and_missing_file_is_not_cached_as_empty(library, monkeypatch):
    from workers_projects_runtime import workspace_file_drafts
    lib,_,_=library
    saved=lib.create_item('local','owner',{'name':'Plan','kind':'note','text':'Use the original method. More text.'},'description-note')
    assert lib.list_items('local','owner')['items'][0]['payload']['description']=='Use the original method.'
    changed=lib.revise_item('local','owner',saved['item_id'],{'text':'Use the approved replacement method. More text.'},saved['version_id'])
    actual=workspace_file_drafts.DraftDownload
    def fail(*args,**kwargs): raise LibraryError('Temporary unavailable source',503)
    monkeypatch.setattr(workspace_file_drafts,'DraftDownload',fail)
    assert 'description' not in lib.list_items('local','owner')['items'][0]['payload']
    monkeypatch.setattr(workspace_file_drafts,'DraftDownload',actual)
    assert lib.list_items('local','owner')['items'][0]['payload']['description']=='Use the approved replacement method.'
    assert lib.get_item('local','owner',saved['item_id'],saved['version_id'])['payload']['text']=='Use the original method. More text.'
