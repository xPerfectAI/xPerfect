"""The final native `result` control event is the provider's typed verdict for a CLI attempt."""

import json

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
