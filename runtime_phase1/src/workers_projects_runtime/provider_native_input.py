"""Owner-scoped foreground requests reuse the existing exact native mailbox."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import time
from pathlib import PurePosixPath
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
    input: str = Field(min_length=1, max_length=64 * 1024)


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
        choice = {'value': value, 'label': str(option.get('name') or option.get('kind') or 'Respond')[:160]}
        if isinstance(option.get('kind'), str) and option['kind'] in {'allow_once', 'allow_always', 'reject_once', 'reject_always'}:
            choice['kind'] = option['kind']
        choices.append(choice)
    if len({choice['value'] for choice in choices}) != len(choices):
        return None
    call = params.get('toolCall')
    prompt = _redact_text(str((call if isinstance(call, dict) else {}).get('title') or 'Approve this action?'))
    if len(prompt) > 8000:
        return None
    # The native model already supplies an action description. Reuse that
    # purpose; the native request remains the complete operation authority.
    meta = call.get('_meta') if isinstance(call, dict) else None
    native = meta.get('x.ai/tool') if isinstance(meta, dict) else None
    native_input = native.get('input') if isinstance(native, dict) else None
    description = native_input.get('description') if isinstance(native_input, dict) else None
    purpose = prompt
    file_purpose = False
    if (isinstance(native, dict) and type(native.get('version')) is int and native['version'] == 1
            and native.get('name') == 'run_terminal_command'
            and native.get('kind') == call.get('kind') == 'execute'
            and isinstance(description, str) and 0 < len(description.strip()) <= 8000):
        purpose = _redact_text(description.strip())
    elif (isinstance(native, dict) and type(native.get('version')) is int and native['version'] == 1
            and native.get('kind') in {'write', 'edit'} and call.get('kind') == 'edit'
            and native.get('read_only') is False
            and ((native.get('name') == 'write' and native.get('kind') == 'write'
                  and native.get('label') is None)
                 or (isinstance(native.get('label'), str) and 0 < len(native['label'].strip()) <= 160
                     and native['label'].isprintable()))
            and isinstance(native_input, dict) and isinstance(native_input.get('path'), str)):
        path = native_input['path']
        raw = call.get('rawInput')
        filename = PurePosixPath(path).name
        if (path and '://' not in path and '\\' not in path and filename not in {'', '.', '..'}
                and not path.endswith('/') and len(filename) <= 160 and filename.isprintable()
                and all(ord(char) >= 32 for char in path)
                and isinstance(raw, dict) and raw.get('file_path') == path
                and ('path' not in raw or raw['path'] == path)):
            label = native.get('label') or 'Write'
            purpose = _redact_text(label.strip()) + ' ' + _redact_text(filename)
            file_purpose = True
    operation_title = purpose if file_purpose else prompt
    # Foreground speech also uses this presentation without a Queen rewrite.
    # Keep a decision cue, with all exact choices in the native form. A full
    # bounded operation description must not be truncated to make room for it.
    offered = _redact_text(purpose + '\nMay I proceed?')
    if len(offered) <= 8000:
        prompt = offered
    fingerprint = hashlib.sha256(json.dumps(item, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'requestId': request_id, 'requestFingerprint': fingerprint,
        'runId': run_id, 'attemptId': attempt_id,
        'expiresAt': datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
        'prompt': prompt, 'choices': choices, 'operationTitle': operation_title}


def project_native_request(item, *, run_id, attempt_id, session_id=None):
    """Project only supported native wire requests, preserving their answer keys."""
    if isinstance(item, dict) and item.get('method') == 'session/request_permission':
        permission = project_permission_request(item, run_id=run_id,
            attempt_id=attempt_id, session_id=session_id)
        return {**permission, 'kind': 'permission'} if permission else None
    if not isinstance(item, dict) or item.get('method') != 'x.ai/ask_user_question':
        return None
    expiry, params, request_id = item.get('expires_at'), item.get('request'), item.get('request_id')
    if (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
            or not math.isfinite(expiry) or expiry <= time.time()
            or not isinstance(params, dict)
            or (session_id and params.get('sessionId') != session_id)
            or not isinstance(request_id, str) or not 1 <= len(request_id) <= 160):
        return None
    questions = params.get('questions')
    if not isinstance(questions, list) or not 1 <= len(questions) <= 16:
        return None
    answers, annotations, prompts, spoken_prompts = {}, {}, [], []
    for question in questions:
        if not isinstance(question, dict):
            return None
        key, options = question.get('question'), question.get('options')
        multi = question.get('multiSelect', question.get('multi_select'))
        if (not isinstance(key, str) or not 1 <= len(key) <= 8000 or key in answers
                or not isinstance(options, list) or len(options) > 16
                or (multi is not None and not isinstance(multi, bool))
                or any(question.get(alias) is not None and not isinstance(question[alias], bool)
                       for alias in ('multiSelect', 'multi_select'))
                or ('multiSelect' in question and 'multi_select' in question
                    and question['multiSelect'] != question['multi_select'])):
            return None
        labels = [option.get('label') if isinstance(option, dict) else None for option in options]
        if (any(not isinstance(label, str) or not 1 <= len(label) <= 160 for label in labels)
                or len(set(labels)) != len(labels)):
            return None
        # Grok's native question contract supplies an automatic Other choice.
        offered = labels if 'Other' in labels else [*labels, 'Other']
        answers[key] = {'type': 'array', 'items': {'type': 'string', 'enum': offered},
            'title': _redact_text(key), 'uniqueItems': True,
            'minItems': 1, 'maxItems': len(offered) if multi else 1}
        annotations[key] = {'type': 'object', 'properties': {
            'notes': {'type': 'string', 'minLength': 1, 'maxLength': 8000}},
            'required': ['notes'], 'additionalProperties': False}
        prompts.append(key)
        spoken_prompts.append(key + ('\n' + '; '.join(offered) + '.' if labels else ''))
    prompt = _redact_text('\n'.join(prompts))
    if not prompt or len(prompt) > 8000:
        return None
    offered_prompt = _redact_text('\n'.join(spoken_prompts))
    if len(offered_prompt) <= 8000:
        prompt = offered_prompt
    schema = {'type': 'object', 'properties': {
        'answers': {'type': 'object', 'properties': answers, 'additionalProperties': False,
                    'minProperties': 1, 'maxProperties': 16},
        'annotations': {'type': 'object', 'properties': annotations, 'additionalProperties': False}},
        'required': ['answers'], 'additionalProperties': False}
    return {'kind': 'question', 'requestId': request_id,
        'requestFingerprint': hashlib.sha256(json.dumps(item, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'runId': run_id, 'attemptId': attempt_id,
        'expiresAt': datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
        'prompt': prompt, 'requestedSchema': schema}


def native_question_response(content, requested_schema=None):
    """Validate the bounded native answer object; replay binds these exact bytes."""
    if not isinstance(content, dict) or not {'answers'} <= set(content) <= {'answers', 'annotations'}:
        raise ValueError('native_input_invalid')
    try:
        if len(json.dumps(content, ensure_ascii=False, separators=(',', ':')).encode()) > 64 * 1024:
            raise ValueError('native_input_invalid')
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError('native_input_invalid') from exc
    answers, annotations = content['answers'], content.get('annotations', {})
    if (not isinstance(answers, dict) or not 1 <= len(answers) <= 16
            or not isinstance(annotations, dict) or not set(annotations) <= set(answers)):
        raise ValueError('native_input_invalid')
    offered_questions = (requested_schema or {}).get('properties', {}).get('answers', {}).get('properties')
    for key, selected in answers.items():
        if (not isinstance(key, str) or not 1 <= len(key) <= 8000
                or not isinstance(selected, list) or not 1 <= len(selected) <= 17
                or any(not isinstance(value, str) or not 1 <= len(value) <= 160 for value in selected)
                or len(set(selected)) != len(selected)):
            raise ValueError('native_input_invalid')
        if offered_questions is not None:
            field = offered_questions.get(key)
            if (not isinstance(field, dict) or len(selected) > field['maxItems']
                    or any(value not in field['items']['enum'] for value in selected)):
                raise ValueError('native_input_invalid')
        notes = annotations.get(key)
        if key in annotations:
            if (not isinstance(notes, dict) or set(notes) != {'notes'}
                    or not isinstance(notes['notes'], str) or not 1 <= len(notes['notes']) <= 8000):
                raise ValueError('native_input_invalid')
            if 'Other' not in selected:
                raise ValueError('native_input_invalid')
    return {'outcome': 'accepted', **content}


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
        projected = project_native_request(item, run_id=run['run_id'],
            attempt_id=attempt_id, session_id=native.get('session_id'))
        if projected is not None:
            if projected['kind'] == 'permission':
                projected.pop('kind')  # Preserve the existing foreground choice envelope.
            result['pending'].append(projected)
    return result


def submit_native_input(provider, record, payload):
    state = native_input_state(provider, record)
    current = next((item for item in state["pending"] if item["requestId"] == payload.get("requestId")), None)
    if not current or any(payload.get(key) != current[key] for key in ["runId", "attemptId", "requestFingerprint"]):
        raise HTTPException(409, "Native input is absent, expired or stale")
    if current.get('kind') == 'question':
        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('native_input_invalid')
                result[key] = value
            return result
        try:
            encoded = payload.get('input')
            if not isinstance(encoded, str) or len(encoded.encode()) > 64 * 1024:
                raise ValueError('native_input_invalid')
            response = native_question_response(json.loads(encoded, object_pairs_hook=unique_keys),
                                                current['requestedSchema'])
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, 'Select a valid native question response') from exc
        control = {'request_id': current['requestId'],
                   'request_fingerprint': current['requestFingerprint'], 'response': response}
    else:
        if payload.get("input") not in [item["value"] for item in current["choices"]]:
            raise HTTPException(422, "Select an offered native response")
        control = {"request_id": current["requestId"], "option_id": payload["input"]}
    run = provider.store.get_run(current["runId"], tenant_id=record["tenant_id"])
    result = provider.service.native_worker_control(run["worker_id"], run_id=current["runId"],
        attempt_id=current["attemptId"], action="permission",
        payload=control)
    if result.get("status") != "permission_submitted":
        raise HTTPException(409, "Native input was not acknowledged")
    return {"version": 1, "accepted": True, "requestId": current["requestId"], "phase": "running"}
