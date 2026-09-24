from pathlib import Path


def test_conversation_retry_reuses_durable_turn_key_and_safe_blocker_labels():
    source = (
        Path(__file__).parents[1]
        / "src"
        / "glass_drive_ui"
        / "static"
        / "conversation.js"
    ).read_text(encoding="utf-8")
    assert "idempotency_key:turn.turn_id" in source
    assert "idempotency_key:crypto.randomUUID(),message:turn.message" not in source
    for code in (
        "provider_account_busy",
        "provider_unavailable",
        "provider_auth_missing",
        "ParallelExecutionIsolationError",
        "HostCapacityError",
    ):
        assert code in source
