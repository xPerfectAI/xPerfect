import pytest

from workers_projects_runtime.models import ActiveWorkActionRequest, WorkspaceContinuationContextRequest
from workers_projects_runtime.workspace_continuation import accepted_run_input, build_workspace_continuation_context


@pytest.mark.parametrize('serialized', [False, True])
def test_continuation_keeps_exact_accepted_text_and_inherited_evidence(serialized):
    import json

    context = {'version': 1, 'base_instruction': '  Exact original.\n',
        'guidance': ['\nKeep this correction.  '],
        'inherited_conversation': {'version': 1, 'conversation_id': 'synthetic-conversation',
            'sources': [{'id': 'evidence', 'role': 'user', 'text': 'Evidence, not execution guidance.'}]}}
    row = {'run_id': 'synthetic-run', 'instruction': 'Rendered continuation',
        'continuation_context_json': json.dumps(context) if serialized else context}
    assert accepted_run_input(row)['continuation_context'] == context
    assert build_workspace_continuation_context(previous_run=row) == context
    following = build_workspace_continuation_context(previous_run=row, continuation_goal='  New correction.\n')
    assert following == {**context, 'guidance': [*context['guidance'], '  New correction.\n']}


def test_initial_continuation_preserves_original_whitespace():
    original = '\n  Keep this exact original.\n'
    assert build_workspace_continuation_context(previous_run={'instruction': original}) == {
        'version': 1, 'base_instruction': original, 'guidance': []}


def test_continuation_preserves_original_and_all_guidance_without_courier_ceiling():
    text = 'x' * 140_000 + '\nKeep the final condition.'
    guidance = [f'{n}: {text}' for n in range(130)]
    context = {'version': 1, 'base_instruction': text, 'guidance': guidance}
    row = {'run_id': 'synthetic-run', 'instruction': text, 'continuation_context_json': context}
    assert accepted_run_input(row)['continuation_context'] == context
    assert build_workspace_continuation_context(previous_run=row, continuation_goal='Last guidance')['guidance'] == [*guidance, 'Last guidance']
    assert WorkspaceContinuationContextRequest.model_validate(context).guidance == guidance
    assert ActiveWorkActionRequest(action='steer', instruction=text, idempotencyKey='synthetic-guidance').instruction == text
    with pytest.raises(ValueError):
        WorkspaceContinuationContextRequest.model_validate({**context, 'guidance': ['']})
