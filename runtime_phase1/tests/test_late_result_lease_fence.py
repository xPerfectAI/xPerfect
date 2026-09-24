from workers_projects_runtime.service import WorkersProjectsService


class _FakeStore:
    def __init__(self, lease):
        self._lease = lease

    def get_host_run_lease(self, lease_id):
        return self._lease if self._lease and self._lease["lease_id"] == lease_id else None


def _service_with(lease):
    service = WorkersProjectsService.__new__(WorkersProjectsService)
    service.store = _FakeStore(lease)
    return service


def test_result_bound_to_released_lease_is_fenced():
    service = _service_with({"lease_id": "lease-1", "status": "released"})
    assert (
        service._recovered_run_lease_is_active({"run_id": "run-1"}, {"expected_lease_id": "lease-1"})
        is False
    )


def test_result_bound_to_active_lease_is_applied():
    service = _service_with({"lease_id": "lease-1", "status": "active"})
    assert (
        service._recovered_run_lease_is_active({"run_id": "run-1"}, {"expected_lease_id": "lease-1"})
        is True
    )


def test_result_without_a_bound_lease_is_not_fenced():
    service = _service_with(None)
    assert service._recovered_run_lease_is_active({"run_id": "run-1"}, {"expected_lease_id": ""}) is True
    assert service._recovered_run_lease_is_active({"run_id": "run-1"}, {}) is True
