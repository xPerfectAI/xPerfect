"""Watch keeps its terminal on the latest run: a new run re-attaches it so it follows that
run's work, and a finished run keeps its output. Runs the real watch.js functions under Node."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WATCH_JS = Path(__file__).resolve().parents[1] / "src/glass_drive_ui/static/watch.js"


def _function(source: str, name: str) -> str:
    match = re.search(rf"^function {name}\(.*?^}}\n", source, flags=re.S | re.M)
    assert match, name
    return match.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to run watch.js functions")
def test_terminal_url_changes_only_when_a_new_run_starts():
    source = WATCH_JS.read_text()
    script = "\n".join([
        "let signedToken = 'link token';",
        _function(source, "terminalViewUrl"),
        _function(source, "withAuth"),
        "const url = (run) => withAuth(terminalViewUrl('http://127.0.0.1:8780', 'wrk_1', run));",
        "console.log(JSON.stringify({before: url(''), first: url('run_a'), again: url('run_a'),",
        "  next: url('run_b'), plain: terminalViewUrl('', 'wrk_1', '')}));",
    ])
    result = json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)
    assert result["plain"] == "/ui/workers/wrk_1/terminal"
    assert result["before"] == "http://127.0.0.1:8780/ui/workers/wrk_1/terminal?gh_token=link%20token"
    assert result["first"] == "http://127.0.0.1:8780/ui/workers/wrk_1/terminal?run=run_a&gh_token=link%20token"
    assert result["again"] == result["first"]
    assert result["next"] != result["first"] and "run=run_b" in result["next"]


def test_watch_attaches_the_terminal_for_the_latest_run():
    source = WATCH_JS.read_text()
    assert "currentTerminalUrl = withAuth(terminalViewUrl(runtimeBase, workerId, String(data.latest_run?.run_id || '')));" in source
