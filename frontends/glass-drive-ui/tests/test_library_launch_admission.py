"""Library review is distinct from safe-copy capability reapproval."""
import pytest
from fastapi.testclient import TestClient

from glass_drive_ui.server import create_app
from test_server import FakeRuntimeClient, clear_glasshive_ui_env  # noqa: F401


class LibraryRuntime(FakeRuntimeClient):
    def __init__(self, kind, state):
        super().__init__()
        self.library_kind, self.library_state = kind, state

    def file_request(self, method, path, payload=None):
        self.file_upload_requests.append((method, path, payload))
        if path.endswith('/personal-library'):
            item = {'application_id': 'app-library', 'item_id': 'item-library',
                    'version_id': 'version-1', 'name': 'Selected material',
                    'kind': self.library_kind, 'state': self.library_state}
            return {'items': [item], 'pending': [{key: value for key, value in item.items() if key != 'kind'}]}
        return {'items': [], 'state': 'available'}


def launch(runtime):
    return TestClient(create_app(runtime_client=runtime)).post('/api/launch', json={
        'description': 'Use the selected source with the native worker.',
        'workspace_option': 'new:codex-cli', 'launch_surface': 'terminal',
        'library_refs': [{'item_id': 'item-library', 'version_id': 'version-1'}],
        'idempotency_key': 'synthetic-library-launch',
    })


def test_source_only_native_reference_allows_worker_to_do_its_native_setup():
    runtime = LibraryRuntime('native', 'needs_setup')
    response = launch(runtime)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['status'] == 'dispatched'
    assert body['library_action_required'] is False
    assert body['library_setup_pending'][0]['state'] == 'needs_setup'
    assert body['capabilities_requiring_reapproval'] == 0
    assert body['reapproval_items'] == []
    assert len(runtime.assign_requests) == 1


@pytest.mark.parametrize('kind,state', [('skill', 'needs_review'), ('approved', 'needs_review'), ('skill', 'not_available'), ('unknown', 'needs_setup')])
def test_pending_item_stays_unapplied_while_original_job_runs_and_links_exact_worker(kind, state):
    runtime = LibraryRuntime(kind, state)
    response = launch(runtime)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['status'] == 'dispatched'
    assert body['library_action_required'] is True
    assert body['library_requirements'][0]['state'] == state
    assert body['capabilities_requiring_reapproval'] == 0
    assert body['reapproval_items'] == []
    assert body['library_watch_url'].startswith('/watch/' + body['worker_id'] + '?')
    assert 'surface=terminal' in body['library_watch_url']
    assert 'library=added' in body['library_watch_url']
    assert 'gh_token' not in body['library_watch_url']
    assert len(runtime.assign_requests) == 1
    assert 'Use the selected source with the native worker.' in str(runtime.assign_requests[0])
    assert runtime.schedule_requests == []
    library_calls = [row for row in runtime.file_upload_requests if row[1].endswith('/personal-library')]
    assert len(library_calls) == 1
    assert library_calls[0][2]['confirm_native'] is False
