"""The operator sees unknown coverage and exact reported money."""
import base64
from pathlib import Path
import subprocess

from glass_drive_ui.server import STATIC_DIR


def test_usage_and_failure_presenter_preserve_unknown_zero_and_money_provenance():
    source = (Path(STATIC_DIR) / "usage-presenter.js").read_bytes()
    module = "data:text/javascript;base64," + base64.b64encode(source).decode()
    script = f"const {{usageSummaryText,failureSupportText}}=await import({module!r});" + r'''
import assert from 'node:assert/strict';
assert.equal(usageSummaryText(null),'Usage: Unknown');
assert.equal(usageSummaryText({coverage:'unknown',tokens:{input_tokens:{value:0,status:'reported'}}}),'Usage: Unknown');
const usage={coverage:'partial',tokens:{input_tokens:{value:0,status:'reported'},output_tokens:{value:null,status:'not_reported'}},money:[]};
assert.match(usageSummaryText(usage),/Partial · 0 input · Unknown output/);
assert.match(usageSummaryText(usage),/Cost: Unknown/);
usage.coverage='complete';usage.tokens.output_tokens={value:0,status:'reported'};
usage.money=[{currency:'USD',amount:'123456789012345.000001',source:'provider_reported'},{currency:'EUR',amount:'0.0002',source:'estimate'}];
usage.money_coverage='partial';
assert.match(usageSummaryText(usage),/Reported USD 123456789012345.000001/);
assert.match(usageSummaryText(usage),/Estimated EUR 0.0002/);
assert.match(usageSummaryText(usage),/Cost coverage: Partial/);
const aggregate={coverage:'partial',tokens:{input_tokens:{value:null,status:'not_reported'},output_tokens:{value:null,status:'not_reported'}},native_totals:{input_tokens:{value:42,status:'partial'},output_tokens:{value:0,status:'reported'}},money:[]};
assert.match(usageSummaryText(aggregate),/Partial · 42 total input · 0 output/);
assert.match(failureSupportText({failure_status_code:429,failure_status_source:'adapter_synthesized',upstream_request_id:'support-ref'}),/Recovery status/);
assert.match(failureSupportText({failure_status_code:403,failure_status_source:'harness_reported'}),/Reported by worker/);
assert.equal(failureSupportText({}),'');
'''
    subprocess.run(["node", "--input-type=module", "--eval", script], check=True, text=True, capture_output=True)
