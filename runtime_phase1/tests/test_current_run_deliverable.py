"""Current reply files are distinct from a reusable workspace's saved files."""
import os
from pathlib import Path

import pytest

from workers_projects_runtime.deliverables import candidate_artifact_paths, deliverable_payload

RUN = {"run_id": "run-current", "state": "completed", "started_at": "1970-01-01T00:30:00Z", "ended_at": "1970-01-01T00:35:00Z"}


def workspace(tmp_path, monkeypatch, entries):
    times = {}
    for relative, (mtime, ctime) in entries.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("A real native file")
        times[path] = (mtime, ctime)
    original = Path.stat

    def stat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path not in times:
            return result
        values = list(result)
        values[8], values[9] = times[path]
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", stat)
    return {"worker_id": "worker-expert", "workspace_dir": str(tmp_path),
            "execution_mode": "host", "workspace_kind": "named", "_execution_workspace_mode": "isolated"}


@pytest.mark.parametrize("entries,expected", [
    ({"Reports/old.txt": (1000, 1000), "new.txt": (2000, 2000)}, "new.txt"),
    ({"Reports/copied.txt": (1000, 1799), "copy-proof.txt": (2000, 2000)}, "copy-proof.txt"),
    ({"Reports/old.txt": (1000, 1000), "appended.txt": (2000, 2000)}, "appended.txt"),
    ({"same-path.txt": (2000, 2000)}, "same-path.txt"),
    ({"Reports/old.txt": (1000, 1000), "download.txt": (1000, 2000)}, "download.txt"),
    ({"Reports/old.txt": (1000, 1000), "index.html": (1000, 1000), "answer.txt": (2000, 2000)}, "answer.txt"),
])
def test_current_run_file_wins_over_prior_workspace_files(tmp_path, monkeypatch, entries, expected):
    worker = workspace(tmp_path, monkeypatch, entries)
    result = deliverable_payload(worker, RUN, "Native answer")
    assert result["workspace_path"] == expected
    # The complete retained workspace inventory is still available.
    assert set(candidate_artifact_paths(worker)) == {tmp_path / path for path in entries}


def test_text_only_followup_has_no_stale_file_primary(tmp_path, monkeypatch):
    worker = workspace(tmp_path, monkeypatch, {"Reports/old.txt": (1000, 1000)})
    assert deliverable_payload(worker, RUN, "Scheduled check complete.") is None
    assert candidate_artifact_paths(worker) == [tmp_path / "Reports/old.txt"]


def test_file_changed_after_terminal_run_is_not_its_output(tmp_path, monkeypatch):
    worker = workspace(tmp_path, monkeypatch, {"later.txt": (2200, 2200)})
    assert deliverable_payload(worker, RUN, "Native answer") is None


def test_preview_before_any_run_retains_existing_inventory(tmp_path, monkeypatch):
    worker = workspace(tmp_path, monkeypatch, {"existing.txt": (1000, 1000)})
    assert deliverable_payload(worker, None, "")["workspace_path"] == "existing.txt"


def test_shared_files_still_cannot_be_attributed_by_timestamps(tmp_path, monkeypatch):
    worker = workspace(tmp_path, monkeypatch, {"sibling.txt": (2000, 2000)})
    assert deliverable_payload({**worker, "_execution_workspace_mode": "shared"}, RUN, "Reply") is None


def test_retained_file_cap_does_not_hide_current_native_result(tmp_path, monkeypatch):
    entries = {f'Reports/old-{i:02}.txt': (1000, 1000) for i in range(60)}
    entries['answer.txt'] = (2000, 2000)
    worker = workspace(tmp_path, monkeypatch, entries)
    assert deliverable_payload(worker, RUN, 'Native answer')['workspace_path'] == 'answer.txt'


def test_retained_html_cap_does_not_hide_current_native_page(tmp_path, monkeypatch):
    entries = {f'old-{i:02}.html': (1000, 1000) for i in range(25)}
    entries['z-current.html'] = (2000, 2000)
    worker = workspace(tmp_path, monkeypatch, entries)
    assert deliverable_payload(worker, RUN, 'Native page')['workspace_path'] == 'z-current.html'
