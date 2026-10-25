"""The final native `result` control event is the provider's typed verdict for a CLI attempt."""

import json

import pytest

from workers_projects_runtime.failure_classification import classify_cli_failure


def _result_line(**overrides):
    record = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "terminal_reason": "api_error",
        "api_error_status": 400,
        "result": "API Error: Error response",
        "num_turns": 6,
        "session_id": "synthetic-session",
    }
    record.update(overrides)
    return json.dumps(record)


def _assistant_error_line():
    return json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "API Error: Error response"}]},
            "error": "unknown",
            "is_api_error_message": True,
        }
    )


def test_terminal_native_api_error_is_typed_before_prose_heuristics():
    # A 5xx terminal verdict is the provider ending the continuation; a bare 400 is a rejected
    # request and is covered separately as non-retryable.
    stdout = "\n".join([_assistant_error_line(), _result_line(api_error_status=500)]) + "\n"
    classification = classify_cli_failure(
        stdout=stdout, stderr="", runtime_name="claude-code", exit_code=1
    )
    assert classification.failure_class == "provider_response_failed"
    assert classification.structured is True
    assert classification.retryable is True
    assert "unexpectedly" in classification.user_message


def test_terminal_native_auth_error_is_typed_from_status():
    stdout = _result_line(api_error_status=401, result="API Error: 401 authentication_error") + "\n"
    classification = classify_cli_failure(
        stdout=stdout, stderr="", runtime_name="claude-code", exit_code=1
    )
    assert classification.failure_class == "provider_auth_missing"
    assert classification.structured is True


def test_successful_result_is_not_treated_as_a_provider_error():
    stdout = _result_line(is_error=False, terminal_reason="", api_error_status=None, result="done") + "\n"
    classification = classify_cli_failure(
        stdout=stdout, stderr="worker crashed", runtime_name="claude-code", exit_code=1
    )
    assert classification.failure_class != "provider_response_failed"


def test_bare_terminal_400_is_request_rejected_and_not_retryable():
    """The observed provider verdict: HTTP 400 with no typed code must not be replayed."""
    stdout = "\n".join([_assistant_error_line(), _result_line(api_error_status=400)]) + "\n"
    classification = classify_cli_failure(
        stdout=stdout, stderr="", runtime_name="claude-code", exit_code=1
    )
    assert classification.failure_class == "provider_request_rejected"
    assert classification.retryable is False
    assert classification.structured is True


def test_terminal_native_diagnostic_is_redacted_and_bounded():
    secret = "sk-" + "A1b2C3d4E5f6G7h8I9j0" * 2
    stdout = (
        "\n".join(
            [
                _assistant_error_line(),
                _result_line(
                    api_error_status=500,
                    result=(
                        "API Error: upstream failed for api_key=" + secret
                        + " while reading /Users/private-person/Documents/brief.txt "
                        + "bearer " + "Zz9" * 20 + " " + ("x" * 3000)
                    ),
                ),
            ]
        )
        + "\n"
    )
    classification = classify_cli_failure(
        stdout=stdout, stderr="", runtime_name="claude-code", exit_code=1
    )
    assert classification.failure_class == "provider_response_failed"
    summary = classification.diagnostic_summary
    assert len(summary) <= 1200
    assert secret not in summary
    assert "private-person" not in summary
    assert "Zz9Zz9Zz9" not in summary


CODEX_REFUSAL = ("You’ve hit your usage limit. Visit https://provider.example/usage to purchase more credits "
                 "or try again at Oct 1st, 2026 7:43 AM.")


def _codex_stream(*events):
    return "\n".join(json.dumps(event) for event in events)


def test_a_provider_stopped_turn_quotes_the_providers_own_reason_without_rerouting():
    stdout = _codex_stream(
        {"type": "thread.started", "thread_id": "synthetic-thread"},
        {"type": "turn.started"},
        {"type": "error", "message": CODEX_REFUSAL},
        {"type": "turn.failed", "error": {"message": CODEX_REFUSAL}},
    )
    classification = classify_cli_failure(stdout=stdout, stderr="", runtime_name="codex-cli", exit_code=1)
    # Same class and no structured capacity evidence: nothing reroutes or substitutes.
    assert classification.failure_class == "provider_response_failed"
    assert classification.structured is False
    assert classification.user_message == (
        "The model provider stopped the worker turn and said: “" + CODEX_REFUSAL + "”")
    # Both trusted events carry the same words; they are quoted once, and recovery stays neutral.
    assert "workspace_continue" in classification.recommended_recovery


def test_a_transient_provider_stop_quotes_every_distinct_provider_message_in_order():
    stdout = _codex_stream(
        {"type": "response.failed", "error": {"message": "stream disconnected before completion"}},
        {"type": "turn.failed", "error": {"message": "response.failed event received"}},
    )
    classification = classify_cli_failure(stdout=stdout, stderr="", runtime_name="codex-cli", exit_code=1)
    assert classification.failure_class == "provider_response_failed" and classification.retryable
    assert classification.user_message == ("The model provider stopped the worker turn and said: “"
                                           "stream disconnected before completion — response.failed event received”")
    assert "workspace_continue" in classification.recommended_recovery


def test_worker_output_is_never_quoted_as_the_providers_reason():
    stdout = _codex_stream(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Task note: usage limit reached."}},
        {"type": "turn.failed", "error": {}},
    )
    classification = classify_cli_failure(stdout=stdout, stderr="", runtime_name="codex-cli", exit_code=1)
    assert "usage limit" not in classification.user_message
    assert classification.user_message == (
        "The model provider ended the worker turn unexpectedly before the task finished.")


def test_the_quoted_reason_is_one_bounded_printable_line():
    from workers_projects_runtime.failure_classification import provider_stated_reason

    noisy = "Refused\u0007 for\n\nnow " + "x" * 600
    stated = provider_stated_reason(_codex_stream({"type": "turn.failed", "error": {"message": noisy}}))
    assert stated.startswith("Refused for now x") and "\u0007" not in stated and "\n" not in stated
    assert len(stated) == 300 and stated.endswith("…")
    assert provider_stated_reason(_codex_stream({"type": "item.completed", "text": "not a provider event"})) == ""


def _encoded_error(status=400, code="model_not_found"):
    return json.dumps({"type": "error", "status": status,
                       "error": {"code": code, "message": "Synthetic private provider detail"}})


def _encoded_native_turn(status=400, code="model_not_found"):
    return _codex_stream({"type": "turn.failed", "error": {"message": _encoded_error(status, code)}})


def test_encoded_native_model_error_has_safe_actionable_typed_reason():
    for stdout in (_encoded_native_turn(),
                   _codex_stream({"type": "error", "message": _encoded_error()})):
        classification = classify_cli_failure(stdout=stdout, stderr="", runtime_name="codex-cli", exit_code=1)
        assert classification.failure_class == "model_configuration_required"
        assert classification.structured and not classification.retryable
        assert classification.provider_event_source == "provider_native"
        assert classification.user_message == (
            "The selected model is not available for this account. Check the account and model before trying again.")
        assert "Synthetic private" not in classification.user_message
        assert "workspace" in classification.recommended_recovery


def test_encoded_native_errors_reuse_existing_auth_capacity_and_request_classes():
    for status, code, expected in (
        (401, "invalid_api_key", "provider_auth_missing"),
        (429, "rate_limit_exceeded", "provider_rate_limited"),
        (429, "insufficient_quota", "provider_quota_exhausted"),
        (503, "service_unavailable", "provider_response_failed"),
        (400, "invalid_request_error", "provider_request_rejected"),
    ):
        classification = classify_cli_failure(stdout=_encoded_native_turn(status, code), stderr="",
                                              runtime_name="codex-cli", exit_code=1)
        assert classification.failure_class == expected
        assert classification.structured
        assert "Synthetic private" not in classification.user_message


def test_native_stdout_model_verdict_does_not_inherit_old_stderr_auth_or_capacity():
    for stderr in (_encoded_native_turn(401, "invalid_api_key"),
                   _encoded_native_turn(429, "rate_limit_exceeded"),
                   _encoded_native_turn(503, "service_unavailable")):
        classification = classify_cli_failure(stdout=_encoded_native_turn(), stderr=stderr,
                                              runtime_name="codex-cli", exit_code=1)
        assert classification.failure_class == "model_configuration_required"
        assert classification.structured and not classification.retryable
    # Stderr is still usable when the current stdout carries no typed provider verdict.
    classification = classify_cli_failure(stdout=_codex_stream({"type": "turn.failed", "error": {}}),
                                          stderr=_encoded_native_turn(), runtime_name="codex-cli", exit_code=1)
    assert classification.failure_class == "model_configuration_required"


def test_encoded_provider_json_in_assistant_user_or_tool_output_is_not_authority():
    encoded = _encoded_error()
    for event in (
        {"type": "assistant", "message": {"content": [{"type": "text", "text": encoded}]}},
        {"type": "user", "message": encoded},
        {"type": "tool_result", "content": encoded},
        {"type": "item.completed", "item": {"type": "agent_message", "text": encoded}},
        {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": encoded}},
    ):
        classification = classify_cli_failure(stdout=_codex_stream(event), stderr="",
                                              runtime_name="codex-cli", exit_code=1)
        assert classification.failure_class != "model_configuration_required"
        assert classification.structured is False


def test_malformed_or_non_provider_native_message_is_not_decoded_as_model_error():
    for message in ('{"type":"error",broken',
                    json.dumps({"type": "notice", "status": 400, "error": {"code": "model_not_found"}}),
                    json.dumps({"type": "error", "status": 200, "error": {"code": "model_not_found"}}),
                    json.dumps({"type": "error", "status": 400, "error": "model_not_found"}),
                    "x" * 65537):
        classification = classify_cli_failure(stdout=_codex_stream({"type": "turn.failed", "error": {"message": message}}),
                                              stderr="", runtime_name="codex-cli", exit_code=1)
        assert classification.failure_class != "model_configuration_required"
        assert classification.structured is False


def _native_http_failure(status, *, code=None, message="Opaque provider refusal"):
    error = {"message": message,
             "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": status}},
             "additionalDetails": None}
    if code is not None:
        error["code"] = code
    return _codex_stream({"type": "turn.failed", "error": error, "native_status": "failed"})


def test_native_forbidden_without_auth_code_reports_access_denial_not_missing_credentials():
    for message in ("Opaque provider refusal", "The selected model is unavailable", "Invalid API key"):
        for stdout in (_native_http_failure(403, message=message),
                       _result_line(api_error_status=403, result=message)):
            failure = classify_cli_failure(stdout=stdout, stderr="", runtime_name="codex-cli", exit_code=1)
            assert failure.failure_class == "provider_request_rejected"
            assert failure.structured and not failure.retryable
            assert not failure.personal_account_reconnect
            assert "denied access" in failure.user_message
            assert "project permissions" in failure.recommended_recovery
            assert "model access" in failure.recommended_recovery
            assert "workspace" in failure.recommended_recovery
            assert "Fix the provider key" not in failure.recommended_recovery


def test_explicit_model_code_wins_over_generic_forbidden_status():
    for stdout in (_native_http_failure(403, code="model_not_found"),
                   _encoded_native_turn(403, "model_not_found"),
                   _result_line(api_error_status=403, error="model_not_found")):
        failure = classify_cli_failure(stdout=stdout, stderr="", runtime_name="codex-cli", exit_code=1)
        assert failure.failure_class == "model_configuration_required"
        assert failure.structured and not failure.retryable
        assert "not available for this account" in failure.user_message


def test_unauthorized_and_explicit_auth_codes_keep_authentication_recovery():
    for status, code in ((401, None), (401, "model_not_found"),
                         (403, "invalid_api_key"), (403, "authentication_error"),
                         (403, "permission_denied")):
        for stdout in (_native_http_failure(status, code=code),
                       _result_line(api_error_status=status, **({"error": code} if code else {}))):
            failure = classify_cli_failure(stdout=stdout, stderr="", runtime_name="codex-cli", exit_code=1)
            assert failure.failure_class == "provider_auth_missing"
            assert failure.structured and not failure.retryable
            assert "credentials" in failure.user_message


def test_quoted_native_forbidden_payload_is_not_provider_authority():
    quoted = _native_http_failure(403)
    for event in (
        {"type": "assistant", "message": {"content": [{"type": "text", "text": quoted}]}},
        {"type": "user", "message": quoted},
        {"type": "tool_result", "content": quoted},
        {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": quoted}},
    ):
        failure = classify_cli_failure(stdout=_codex_stream(event), stderr="", runtime_name="codex-cli", exit_code=1)
        assert failure.failure_class not in {"provider_request_rejected", "model_configuration_required"}
        assert not failure.structured


@pytest.mark.parametrize("status,code,expected", (
        (401, None, "provider_auth_missing"),
        (401, "model_not_found", "provider_auth_missing"),
        (403, None, "provider_request_rejected"),
        (403, "model_not_found", "model_configuration_required"),
        (403, "invalid_api_key", "provider_auth_missing"),
        (400, None, "provider_request_rejected"),
        (429, None, "provider_rate_limited"),
))
def test_terminal_status_aliases_preserve_auth_and_model_code_precedence(status, code, expected):
    failures = []
    for field in ("api_error_status", "error_status"):
        fields = {"api_error_status": None, field: status}
        if code:
            fields["error"] = code
        failure = classify_cli_failure(
            stdout=_result_line(**fields), stderr="", runtime_name="codex-cli", exit_code=1
        )
        assert failure.failure_class == expected
        assert failure.structured
        assert failure.retryable is (status == 429)
        failures.append(failure)
    assert failures[0].user_message == failures[1].user_message
    assert failures[0].recommended_recovery == failures[1].recommended_recovery
    assert failures[0].personal_account_reconnect == failures[1].personal_account_reconnect
    assert failures[0].provider_event_source == failures[1].provider_event_source


def test_quoted_terminal_status_alias_is_not_provider_authority():
    quoted = _result_line(api_error_status=None, error_status=403)
    for event in (
        {"type": "assistant", "message": {"content": [{"type": "text", "text": quoted}]}},
        {"type": "user", "message": quoted},
        {"type": "tool_result", "content": quoted},
        {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": quoted}},
    ):
        failure = classify_cli_failure(stdout=_codex_stream(event), stderr="",
                                       runtime_name="codex-cli", exit_code=1)
        assert failure.failure_class not in {"provider_request_rejected", "model_configuration_required"}
        assert not failure.structured
