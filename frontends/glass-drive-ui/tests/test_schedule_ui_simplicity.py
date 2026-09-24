import json
import subprocess
from pathlib import Path


STATIC_DIR = Path(__file__).parents[1] / "src" / "glass_drive_ui" / "static"


def test_schedule_run_recovery_survives_render_history_and_normal_navigation():
    script = Path(__file__).with_name("schedule-recovery.cjs")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "5 schedule recovery causal cases passed" in result.stdout


def test_kickoff_details_are_visible_and_goal_is_the_only_required_field():
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "Add details" not in index
    assert 'id="success_criteria" name="success_criteria"' in index
    assert 'id="context" name="context"' in index
    assert 'id="description" name="description"' in index
    assert 'id="description" name="description" rows="4" placeholder="Describe what you want done…" required' in index
    assert 'id="success_criteria" name="success_criteria" rows="3" placeholder="Anything the result must include?" required' not in index
    assert 'id="context" name="context" rows="3" placeholder="Helpful context, links, or constraints." required' not in index
    assert '<div class="project-details" aria-label="Optional project details">' in index


def test_schedule_surface_starts_with_saved_list_and_compact_editor_controls():
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    control_plane = (STATIC_DIR / "control-plane.js").read_text(encoding="utf-8")

    assert 'id="new-schedule-action"' in index
    assert '>New schedule<' in index
    assert 'id="schedule-create-card"' in index and ' aria-labelledby="recurring-schedule-form-title" hidden>' in index
    for value in ('once', 'daily', 'weekly', 'custom', 'cron', 'rfc5545'):
        assert f'value="{value}"' in index
    assert 'id="recurring-schedule-weekday"' in index
    assert 'id="recurring-schedule-weekly-time"' in index
    assert 'id="recurring-schedule-timezone-note"' in index
    assert 'id="recurring-schedule-workspace-empty"' in index
    assert 'id="recurring-schedule-create-workspace"' in index
    assert '>Start project<' in index
    assert 'Scheduling a one-off run keeps it as a saved workspace.' in index
    assert '>Cancel<' in index
    assert 'Times use this saved time zone.' in index
    assert 'IANA' not in index
    assert 'id="recurring-schedule-tomorrow"' in index
    assert 'schedule-card-more' in control_plane
    assert 'actions.append(runNowButton, editButton)' in control_plane
    assert 'moreActions.append(retireButton)' in control_plane
    assert 'scheduleLoadErrorNotice(scheduleLoadError,' in control_plane
    assert 'if (schedule.last_error) actions.append(historyButton)' in control_plane
    assert "node('details', 'schedule-history-error')" in control_plane
    assert 'Check Error details, fix the cause, then use Run now or resume this schedule.' in control_plane
    assert 'We could not confirm whether it started. Check history before trying again.' in control_plane
    assert 'No duplicate occurrence was created' not in control_plane
    assert 'recurringSchedules = { items: [] };\n    scheduleLoadError = error.message;' not in control_plane
    assert 'No workspaces yet' in control_plane
    assert "dependencies.setView('project')" in control_plane
    assert "document.getElementById('description')?.focus()" in control_plane
    assert "loadWorkspaceChoices('ephemeral')" in control_plane
    assert "{ workspace_kind: 'named' }" in control_plane
    assert 'IANA' not in control_plane
    assert 'scheduleNextLabel' in control_plane
    assert 'formatScheduleDateTime(next, schedule.timezone_name || \'UTC\')' in control_plane
    assert 'setScheduleEditorVisible(scheduleEditorVisible)' in control_plane
    assert 'scheduleEditorVisible = false' in control_plane


def test_schedule_layout_stacks_at_narrow_width_and_keeps_one_time_date_readable():
    styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    assert ".schedule-date-input { grid-template-columns: minmax(180px, 1fr); }" in styles
    assert ".schedule-layout.schedule-editor-open { grid-template-columns: minmax(0, 1fr); }" in styles


def test_schedule_policy_keeps_weekly_timezone_and_anchor_semantics():
    module = (STATIC_DIR / "schedule-policy.js").as_uri()
    script = f"""
      import {{
        recurrenceSubmissionPolicy,
        scheduleEditorType,
        tomorrowDateTimeLocal,
        weekdayAtInstant,
        weeklyStartDateTimeLocal,
      }} from {json.dumps(module)};
      const policy = recurrenceSubmissionPolicy('weekly', {{ timezoneName: 'America/Toronto' }});
      if (JSON.stringify(policy) !== JSON.stringify({{
        recurrenceType: 'rfc5545',
        intervalSeconds: null,
        timezoneName: 'America/Toronto',
        rrule: 'FREQ=WEEKLY',
      }})) throw new Error(JSON.stringify(policy));
      if (scheduleEditorType({{ recurrence_type: 'rfc5545', rrule: 'FREQ=WEEKLY' }}) !== 'weekly') throw new Error('weekly mapping missing');
      const reference = new Date('2026-09-22T13:00:00Z');
      if (weekdayAtInstant(reference, 'America/Toronto') !== 'TU') throw new Error('weekday mismatch');
      if (weeklyStartDateTimeLocal('FR', '09:00', 'America/Toronto', reference) !== '2026-09-25T09:00') throw new Error('weekly anchor mismatch');
      if (weeklyStartDateTimeLocal('TU', '09:00', 'America/Toronto', reference) !== '2026-09-29T09:00') throw new Error('same-day rollover mismatch');
      if (tomorrowDateTimeLocal('America/Toronto', reference) !== '2026-09-23T09:00') throw new Error('tomorrow shortcut mismatch');
      if (weeklyStartDateTimeLocal('FR', '09:00', 'Not/AZone', reference) !== '') throw new Error('invalid zone did not fail closed');
    """
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_instruction_edit_preserves_complex_schedule_definition_fields():
    module = (STATIC_DIR / "schedule-policy.js").as_uri()
    script = f"""
      import {{ scheduleEditUpdates }} from {json.dumps(module)};
      const original = {{
        recurrence_type: 'interval',
        interval_seconds: 17,
        local_time: '',
        timezone_name: 'UTC',
        dst_policy: 'elapsed',
        cron_expression: '',
        rrule: '',
        starts_at: '2026-09-22T12:34:56.789123+00:00',
        ends_at: '2026-09-30T12:34:57.654321+00:00',
        misfire_grace_seconds: 301,
        catch_up_policy: 'bounded',
        max_catch_up_occurrences: 4,
        jitter_seconds: 17,
        overlap_policy: 'queue',
        enabled: true,
      }};
      const initial = {{
        instruction: 'Keep the exact report cadence', type: 'custom', customType: 'interval',
        intervalValue: '17', intervalUnit: 'seconds', localTime: '', timezone: 'UTC',
        startsAt: '2026-09-22T12:34', weeklyDay: 'MO', weeklyTime: '09:00',
        endsAt: '2026-09-30T12:34', cron: '', rrule: '', dstPolicy: 'elapsed', enabled: true,
        overlapPolicy: 'queue', misfireGrace: '5.016666666666667', catchUpPolicy: 'bounded',
        catchUpLimit: '4', jitter: '17',
      }};
      const current = {{ ...initial, instruction: 'Use the exact report cadence and add context' }};
      const payload = {{
        instruction: current.instruction, recurrence_type: 'interval', interval_seconds: 17,
        local_time: '', timezone_name: 'UTC', dst_policy: 'elapsed', cron_expression: '', rrule: '',
        starts_at: '2026-09-22T12:34', ends_at: '2026-09-30T12:34', enabled: true,
        overlap_policy: 'queue', misfire_grace_seconds: 301, catch_up_policy: 'bounded',
        max_catch_up_occurrences: 4, jitter_seconds: 17, schedule_text: 'Every 17 seconds',
      }};
      const updates = scheduleEditUpdates({{ original, initial, current, payload }});
      if (JSON.stringify(updates) !== JSON.stringify({{ instruction: current.instruction }})) throw new Error(JSON.stringify(updates));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_weekly_edits_submit_derived_anchor_and_rule_without_roundtripping_untouched_fields():
    module = (STATIC_DIR / "schedule-policy.js").as_uri()
    script = f"""
      import {{ scheduleEditUpdates }} from {json.dumps(module)};
      const original = {{
        recurrence_type: 'rfc5545', rrule: 'FREQ=WEEKLY', interval_seconds: null,
        local_time: '', timezone_name: 'America/Toronto', dst_policy: 'elapsed',
        starts_at: '2026-09-28T13:00:00.123456+00:00', ends_at: null,
      }};
      const initial = {{
        instruction: 'Report', type: 'weekly', customType: 'interval',
        intervalValue: '1', intervalUnit: 'hours', localTime: '09:00',
        timezone: 'America/Toronto', startsAt: '2026-09-28T09:00',
        weeklyDay: 'MO', weeklyTime: '09:00', rrule: 'FREQ=WEEKLY',
      }};
      const payload = {{ ...original, starts_at: '2026-09-29T11:30', schedule_text: 'Every week' }};
      const changedTime = scheduleEditUpdates({{
        original, initial, current: {{ ...initial, weeklyDay: 'TU', weeklyTime: '11:30' }}, payload,
      }});
      if (changedTime.starts_at !== payload.starts_at) throw new Error('weekly day/time was ignored');
      if (changedTime.rrule !== original.rrule) throw new Error('weekly rule drifted');
      if ('dst_policy' in changedTime) throw new Error('stored exact DST policy was needlessly patched');
      const onceOriginal = {{ ...original, recurrence_type: 'once', rrule: '', dst_policy: 'elapsed' }};
      const onceInitial = {{ ...initial, type: 'once', rrule: '' }};
      const converted = scheduleEditUpdates({{
        original: onceOriginal, initial: onceInitial,
        current: {{ ...onceInitial, type: 'weekly' }},
        payload: {{ ...payload, dst_policy: 'next_valid_earliest' }},
      }});
      if (converted.recurrence_type !== 'rfc5545') throw new Error('weekly type missing');
      if (converted.rrule !== 'FREQ=WEEKLY') throw new Error('weekly rule missing');
      if (converted.starts_at !== payload.starts_at) throw new Error('weekly anchor missing');
      if (converted.dst_policy !== 'next_valid_earliest') throw new Error('calendar DST policy missing');
      const unchanged = scheduleEditUpdates({{
        original, initial, current: {{ ...initial, instruction: 'New instruction' }},
        payload: {{ ...payload, instruction: 'New instruction' }},
      }});
      if (JSON.stringify(unchanged) !== JSON.stringify({{ instruction: 'New instruction' }}))
        throw new Error('untouched exact fields were roundtripped');
    """
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
