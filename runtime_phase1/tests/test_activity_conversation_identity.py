"""Activity links only owner-verified native Chat sessions, never display names."""
import pytest

from workers_projects_runtime.coordinator import _SCHEMA
from workers_projects_runtime.store import Store


@pytest.fixture
def activity(tmp_path):
    store = Store(str(tmp_path / 'runtime.db'))
    project = store.create_project('owner-a', 'Synthetic', 'Check activity', 'codex-cli', tenant_id='tenant-a')
    worker = store.create_worker(project['project_id'], 'owner-a', 'Chat · UTC · coordinator', 'conversation-agent',
                                 'codex-cli', 'codex-cli', 'stub', 'synthetic', tenant_id='tenant-a')
    store.add_event(project['project_id'], worker['worker_id'], None, 'run.completed', 'Complete')
    yield store, project, worker
    store.close()


def bind_native(store, project, worker):
    with store._connect() as conn:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO coordinator_conversations (conversation_id,tenant_id,owner_id,config_json,prompt_json,created_at) VALUES(?,?,?,?,?,?)",
                     ('conversation-a', 'tenant-a', 'owner-a', '{}', '{}', '2026-10-24T20:00:00Z'))
    return store.upsert_provider_session(tenant_id='tenant-a', owner_id='owner-a', conversation_id='conversation-a',
        agent_id='coordinator', model_id='synthetic', project_id=project['project_id'], worker_id=worker['worker_id'],
        workspace_dir='', access_mode='owner')


def rows(store):
    return store.list_owner_activity(tenant_id='tenant-a', owner_id='owner-a')


def test_native_identity_projects_without_changing_event_or_owner_scope(activity):
    store, project, worker = activity
    before = rows(store)[0]
    assert before['conversation_id'] is None  # Coordinator schema is optional.
    bind_native(store, project, worker)
    after = rows(store)[0]
    assert after == {**before, 'conversation_id': 'conversation-a'}
    assert store.list_owner_activity(tenant_id='other', owner_id='owner-a') == []
    assert store.list_owner_activity(tenant_id='tenant-a', owner_id='other') == []


@pytest.mark.parametrize('table,column,value', [
    ('provider_sessions', 'tenant_id', 'foreign-tenant'),
    ('provider_sessions', 'owner_id', 'foreign-owner'),
    ('provider_sessions', 'project_id', 'foreign-project'),
    ('provider_sessions', 'worker_id', 'foreign-worker'),
    ('provider_sessions', 'conversation_id', 'external-conversation'),
    ('coordinator_conversations', 'tenant_id', 'foreign-tenant'),
    ('coordinator_conversations', 'owner_id', 'foreign-owner'),
    ('workers', 'trusted_run_lane', 'mission'),
])
def test_mismatched_or_external_session_never_projects_chat_identity(activity, table, column, value):
    store, project, worker = activity
    bind_native(store, project, worker)
    if column in {'project_id', 'worker_id'}:
        foreign_project = store.create_project('other-owner', 'Other project', 'Other work', 'codex-cli', tenant_id='other-tenant')
        foreign_worker = store.create_worker(foreign_project['project_id'], 'other-owner', 'Other worker', 'main',
            'codex-cli', 'codex-cli', 'stub', 'synthetic', tenant_id='other-tenant')
        value = foreign_project['project_id'] if column == 'project_id' else foreign_worker['worker_id']
    event_ids = {row['event_id'] for row in rows(store)}
    with store._connect() as conn:
        conn.execute(f'UPDATE {table} SET {column}=?', (value,))
    result = rows(store)
    assert {row['event_id'] for row in result} == event_ids
    assert len(result) == len(event_ids)
    assert all(row['conversation_id'] is None for row in result)


def test_external_provider_session_has_no_native_chat_link(activity):
    store, project, worker = activity
    bind_native(store, project, worker)
    with store._connect() as conn:
        conn.execute('DELETE FROM coordinator_conversations')
    assert rows(store)[0]['conversation_id'] is None
