"""Watch attaches each dropped upload once. The automatic attach and Add to workspace can both
see a ready upload; a second attach would add another copy to the workspace. Runs the real
watch.js functions under Node with a slow attach, like a large file being copied."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WATCH_JS = Path(__file__).resolve().parents[1] / "src/glass_drive_ui/static/watch.js"
node = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to run the page scripts")


def _source() -> str:
    source = WATCH_JS.read_text()
    start = source.index("const attachStarted = new Set();")
    end = source.index("const syncAttachButton")
    return source[start:end]


@node
def test_an_upload_is_attached_once_even_when_add_to_workspace_is_clicked_during_its_automatic_attach():
    script = r"""
let fail = false; const calls = [];
const workspaceFiles = { attach: async (ids) => { calls.push([...ids]); await new Promise((r) => setTimeout(r, 20));
  if (fail) throw new Error('503'); } };
__SOURCE__
(async () => {
  const automatic = attachOnce(['u1']);
  const offered = unattachedIds(['u1', 'u2']);
  const clicked = attachOnce(offered.length ? ['u1', 'u2'] : []);
  await Promise.all([automatic, clicked]);
  const afterBoth = unattachedIds(['u1', 'u2']);
  fail = true; const failure = await attachOnce(['u3']).then(() => '', (error) => error.message);
  const offeredAgain = unattachedIds(['u3']);
  fail = false; await attachOnce(['u3']);
  console.log(JSON.stringify({ calls, offered, afterBoth, failure, offeredAgain }));
})();
""".replace("__SOURCE__", _source())
    result = json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)
    assert result["offered"] == ["u2"]
    assert result["calls"] == [["u1"], ["u2"], ["u3"], ["u3"]]
    assert result["afterBoth"] == []
    assert result["failure"] == "503"
    assert result["offeredAgain"] == ["u3"]
