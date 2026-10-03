"""Owner-scoped foreground requests reuse the existing exact native mailbox."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import time
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from .profile_runtime import _redact_text


class NativeInputResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    requestId: str = Field(min_length=1, max_length=160)
    requestFingerprint: str = Field(min_length=64, max_length=64)
    runId: str = Field(min_length=1, max_length=160)
    attemptId: str = Field(min_length=1, max_length=160)
    input: str = Field(min_length=1, max_length=160)


def project_permission_request(item, *, run_id, attempt_id, session_id=None):
    """Share the exact native permission projection across foreground and missions."""
    if not isinstance(item, dict) or item.get('method') != 'session/request_permission':
        return None
    expiry = item.get('expires_at')
    if not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= time.time():
        return None
    params = item.get('request')
    if not isinstance(params, dict) or (session_id and params.get('sessionId') != session_id):
        return None
    request_id = item.get('request_id')
    options = params.get('options')
    if not isinstance(request_id, str) or not request_id or len(request_id) > 160:
        return None
    if not isinstance(options, list) or not 1 <= len(options) <= 16:
        return None
    choices = []
    for option in options:
        if not isinstance(option, dict):
            return None
        value = option.get('optionId')
        if not isinstance(value, str) or not value or len(value) > 160:
            return None
        choices.append({'value': value, 'label': str(option.get('name') or option.get('kind') or 'Respond')[:160]})
    if len({choice['value'] for choice in choices}) != len(choices):
        return None
    call = params.get('toolCall')
    prompt = _redact_text(str((call if isinstance(call, dict) else {}).get('title') or 'Approve this action?'))
    if len(prompt) > 8000:
        return None
    fingerprint = hashlib.sha256(json.dumps(item, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'requestId': request_id, 'requestFingerprint': fingerprint,
        'runId': run_id, 'attemptId': attempt_id,
        'expiresAt': datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
        'prompt': prompt, 'choices': choices}


def native_input_state(provider, record):
    result = {"version": 1, "state": record["state"], "pending": []}
    if record["state"] in {"completed", "failed", "cancelled"}:
        return result
    run = provider.store.get_run(str(record.get("run_id") or ""), tenant_id=record["tenant_id"])
    if not run or run.get("state") != "running":
        return result
    worker = provider.store.get_worker(run["worker_id"])
    if (not worker or worker.get("owner_id") != record["owner_id"]
            or worker.get("tenant_id") != record["tenant_id"]):
        raise HTTPException(409, "Native input owner changed")
    if worker.get("profile") != "grok-build":
        return {**result, "state": "unsupported"}
    attempt_id = str(run.get("active_attempt_id") or "")
    try:
        native = provider.service.native_worker_control(worker["worker_id"], run_id=run["run_id"], attempt_id=attempt_id)
    except (ValueError, RuntimeError):
        return result
    if native.get("run_id") != run["run_id"] or native.get("attempt_id") != attempt_id:
        raise HTTPException(409, "Native input attempt changed")
    for item in native.get("pending_requests") or []:
        projected = project_permission_request(item, run_id=run['run_id'],
            attempt_id=attempt_id, session_id=native.get('session_id'))
        if projected is not None:
            result['pending'].append(projected)
    return result


def submit_native_input(provider, record, payload):
    state = native_input_state(provider, record)
    current = next((item for item in state["pending"] if item["requestId"] == payload.get("requestId")), None)
    if not current or any(payload.get(key) != current[key] for key in ["runId", "attemptId", "requestFingerprint"]):
        raise HTTPException(409, "Native input is absent, expired or stale")
    if payload.get("input") not in [item["value"] for item in current["choices"]]:
        raise HTTPException(422, "Select an offered native response")
    run = provider.store.get_run(current["runId"], tenant_id=record["tenant_id"])
    result = provider.service.native_worker_control(run["worker_id"], run_id=current["runId"],
        attempt_id=current["attemptId"], action="permission",
        payload={"request_id": current["requestId"], "option_id": payload["input"]})
    if result.get("status") != "permission_submitted":
        raise HTTPException(409, "Native input was not acknowledged")
    return {"version": 1, "accepted": True, "requestId": current["requestId"], "phase": "running"}
