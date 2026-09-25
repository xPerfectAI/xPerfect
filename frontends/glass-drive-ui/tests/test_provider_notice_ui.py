"""A ready account whose provider stopped its last run says so where people choose it."""
import base64
import subprocess
from pathlib import Path

from glass_drive_ui import server


def run_case(case):
    source = (Path(server.STATIC_DIR) / 'launch-policy.js').read_bytes()
    uri = 'data:text/javascript;base64,' + base64.b64encode(source).decode()
    script = f"import {{ workerAccountSummary }} from '{uri}';\n" + r'''
import assert from 'node:assert/strict';
const data = {new_workspace_options: [{profile: 'codex-cli', label: 'Codex'}], bootstrap_sections: {provider_accounts: 'ready'},
  provider_accounts: [{account_id: 'acct_a', label: 'Personal Codex', status: 'ready'}]};
''' + case
    subprocess.run(['node', '--input-type=module', '--eval', script], check=True, capture_output=True, text=True)


def test_launch_summary_names_a_provider_stop_on_a_ready_account():
    run_case(r'''
const summary = (accounts) => workerAccountSummary({workspaceValue: 'new:codex-cli', accountId: 'acct_a',
  policy: 'personal_required', data: {...data, provider_accounts: accounts}});
assert.equal(summary(data.provider_accounts), 'Codex · Personal Codex');
assert.equal(summary([{...data.provider_accounts[0], provider_notice: {message: 'The provider said: usage limit.'}}]),
  'Codex · Personal Codex · Provider stopped its last run (see Connections)');
assert.equal(summary([{...data.provider_accounts[0], status: 'action_required', provider_notice: {message: 'x'}}]),
  'Codex · Personal Codex · Needs attention');
''')


def test_connections_show_the_providers_last_word_for_every_status():
    source = (Path(server.STATIC_DIR) / 'control-plane.js').read_text()
    # Shown whatever the account status is: a ready account can still be refused by its provider.
    assert ("    if (account.provider_notice?.message) {\n"
            "      // The provider's own reason its last run stopped; a completed run clears it.\n"
            "      copy.append(node('span', 'connection-recovery', `Last run: ${String(account.provider_notice.message)}`));\n"
            "    }\n") in source
