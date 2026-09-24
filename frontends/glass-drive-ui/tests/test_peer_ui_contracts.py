"""Guard the owner-visible peer controls alongside real browser/API journey QA."""

from pathlib import Path
import subprocess


STATIC = Path(__file__).parents[1] / "src/glass_drive_ui/static"


def test_peer_ui_modules_parse_and_keep_exact_current_owner_choice():
    controls = (STATIC / "peer-controls.js").read_text()
    permissions = (STATIC / "peer-permissions.js").read_text()
    for name in ("peer-controls.js", "peer-permissions.js"):
        subprocess.run(["node", "--check", str(STATIC / name)], check=True)

    assert "defaultAll = preservedSelection === null && available <= options.max_targets" in permissions
    assert "selectionToPreserve = new Set(" in permissions
    assert "input.checked=defaultAll || (preservedSelection?.has(member.worker_id) ?? false)" in permissions
    assert "form.querySelector('[data-select-all]').disabled=available>options.max_targets" in permissions
    assert "target_worker_ids:targets,workspaces:snapshots" in permissions
    assert "includes_future_members !== false" in permissions
    assert "An idle worker cannot receive a new message until this is allowed." in permissions
    assert "[data-options-reload]" in permissions
    assert "peer-permissions.js?v=20260922peers2" in controls


def test_peer_ui_keeps_discovery_separate_and_revokes_only_active_directions():
    controls = (STATIC / "peer-controls.js").read_text()
    assert "discovery: policyForm.elements.discoverEnabled.checked" in controls
    assert "access_enabled: policyForm.elements.access.checked" in controls
    assert "if (grant.status === 'active')" in controls
    assert "Revoke messages ${outgoing ? 'to' : 'from'} ${otherName}" in controls
    assert "Number(right.status === 'active') - Number(left.status === 'active')" in controls
    assert "Message blocked. If the worker is idle" in controls
    assert "Some content was already delivered and cannot be erased." in controls
    assert "A model read receipt is not available." in controls
