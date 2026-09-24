from workers_projects_runtime.store import _normalized_failure_fields


def test_normalized_failure_fields_keeps_structured_flag_as_int():
    fields = {
        "failure_class": "provider_quota_exhausted",
        "failure_retryable": 1,
        "failure_structured": 1,
        "failure_user_message": "quota",
        "failure_recommended_recovery": "fallback",
        "failure_diagnostic_summary": "diag",
    }
    normalized = _normalized_failure_fields(fields)
    assert normalized["failure_structured"] == 1
    assert normalized["failure_retryable"] == 1
    assert _normalized_failure_fields({"failure_structured": 0})["failure_structured"] == 0
    assert _normalized_failure_fields({"failure_structured": True})["failure_structured"] == 1
    assert "failure_structured" not in _normalized_failure_fields({"failure_class": "x"})
