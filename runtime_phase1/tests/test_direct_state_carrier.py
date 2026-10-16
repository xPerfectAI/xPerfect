"""Accepted typed Core DATA ranges cross the native carrier without text discovery."""
import hashlib
import json
from copy import deepcopy
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from workers_projects_runtime import conversation_provider as owner
from workers_projects_runtime import direct_runner


def fixture():
    data='<viventium_untrusted_active_work_data encoding="json-v1">\n' + json.dumps({'goal':'é 漢字 "quotes"','state':'fresh'},ensure_ascii=False) + '\n</viventium_untrusted_active_work_data>'
    prefix='Current time: synthetic. é\n'
    context=prefix+data+'\nCurrent unique goal.'
    descriptor={'kind':'active_work','start_utf8':len(prefix.encode()),'length_utf8':len(data.encode()),'sha256':hashlib.sha256(data.encode()).hexdigest()}
    metadata={'owner_id':'synthetic-owner','main_context_protocol':'main_context_v1','main_context_owner':'core',
        'main_context_snapshot_sha256':'a'*64,'main_context_epoch':'e'*64,'stable_authority_sha256':'b'*64,
        'continuity_domain_id':'c'*64,'continuity_agent_id':'synthetic-agent','logical_turn_id':'synthetic-turn',
        'turn_context':context,'main_context_state_blocks':[descriptor]}
    return metadata,data


@pytest.mark.parametrize('profile',['direct-xai','direct-openai','direct-anthropic'])
def test_typed_native_range_to_encoded_json_carrier_and_bound_sdk_origin(monkeypatch,profile):
    metadata,data=fixture()
    monkeypatch.setattr(owner,'require_native_installed_owner',lambda _:None)
    payload=owner.ChatCompletionRequest(model='synthetic',messages=[{'role':'user','content':'Unique current goal'}],metadata=metadata)
    native=owner._hydrate_metadata(payload,Request({'type':'http','headers':[]}),
        owner.ProviderAuthContext(tenant_id='synthetic-tenant',principal_id='synthetic-owner',trust_identity_headers=True))
    accepted=owner._direct_core_state_projection(native,type('Model',(),{'harness_profile':profile})())
    assert accepted['blocks']==[{'kind':'active_work','text':data,'sha256':hashlib.sha256(data.encode()).hexdigest()}]
    carrier=json.dumps({'instruction':native.metadata.turn_context,'context':[{'role':'user','content':'Unique current goal'}],
        'context_manifest':{'source':'synthetic'}},ensure_ascii=False)
    bound=direct_runner._core_state_input({'instruction':carrier,'core_state':accepted})
    assert bound['blocks']==metadata['main_context_state_blocks']
    assert data not in json.dumps(bound)  # Descriptor does not duplicate content into journal metadata.


@pytest.mark.parametrize('change',[{'start_utf8':1},{'length_utf8':1},{'sha256':'f'*64},{'kind':'other'},{'start_utf8':True}])
def test_bad_range_fails_at_typed_native_owner_before_execution(monkeypatch,change):
    metadata,_=fixture();metadata['main_context_state_blocks'][0].update(change)
    monkeypatch.setattr(owner,'require_native_installed_owner',lambda _:None)
    with pytest.raises((HTTPException,ValueError)):
        payload=owner.ChatCompletionRequest(model='synthetic',messages=[{'role':'user','content':'Synthetic current goal'}],metadata=metadata)
        owner._hydrate_metadata(payload,Request({'type':'http','headers':[]}),
            owner.ProviderAuthContext(tenant_id='synthetic-tenant',principal_id='synthetic-owner',trust_identity_headers=True))


def test_untrusted_source_and_partial_binding_are_not_accepted(monkeypatch):
    metadata,_=fixture();monkeypatch.setattr(owner,'require_native_installed_owner',lambda _:None)
    payload=owner.ChatCompletionRequest(model='synthetic',messages=[{'role':'user','content':'Synthetic current goal'}],metadata=metadata)
    with pytest.raises(HTTPException) as denied:
        owner._hydrate_metadata(payload,Request({'type':'http','headers':[]}),
            owner.ProviderAuthContext(tenant_id='synthetic-tenant',principal_id='synthetic-owner',trust_identity_headers=False))
    assert denied.value.status_code==403
    metadata['main_context_owner']='provider_legacy'
    with pytest.raises(ValueError):owner._core_state_block_data(owner.CompletionMetadata.model_validate(metadata))


def test_no_descriptor_keeps_native_and_direct_legacy_paths_unchanged():
    metadata,_=fixture();metadata.pop('main_context_state_blocks')
    payload=owner.ChatCompletionRequest(model='synthetic',messages=[{'role':'user','content':'Synthetic current goal'}],metadata=metadata)
    assert owner._direct_core_state_projection(payload,type('Model',(),{'harness_profile':'direct-xai'})()) is None
    assert owner._direct_core_state_projection(payload,type('Model',(),{'harness_profile':'codex-cli'})()) is None
