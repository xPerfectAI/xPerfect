function tokenValue(field) {
  return field && Number.isSafeInteger(field.value) && field.value >= 0
    && ['reported', 'partial'].includes(field.status) ? field.value.toLocaleString() : 'Unknown';
}

export function usageSummaryText(usage) {
  if (!usage || (usage.coverage === 'unknown' && !(usage.money || []).length)) return 'Usage: Unknown';
  const status = usage.coverage === 'complete' ? 'Reported' : usage.coverage === 'partial' ? 'Partial' : 'Unknown';
  const tokens = usage.tokens || {};
  const canonicalInput = tokenValue(tokens.input_tokens);
  const input = canonicalInput === 'Unknown' ? tokenValue(usage.native_totals?.input_tokens) : canonicalInput;
  const output = tokenValue(tokens.output_tokens) === 'Unknown' ? tokenValue(usage.native_totals?.output_tokens) : tokenValue(tokens.output_tokens);
  const inputLabel = canonicalInput === 'Unknown' && input !== 'Unknown' ? 'total input' : 'input';
  const parts = [`Usage: ${status}`, `${input} ${inputLabel}`, `${output} output`];
  for (const key of ['cache_read_input_tokens', 'cache_creation_input_tokens']) {
    const field = tokens[key];
    if (field?.value > 0) parts.push(`${tokenValue(field)} ${key === 'cache_read_input_tokens' ? 'cached input read' : 'cache creation'}`);
  }
  for (const money of usage.money || []) {
    if (typeof money.amount !== 'string' || typeof money.currency !== 'string') continue;
    parts.push(`${money.source === 'provider_reported' ? 'Reported' : 'Estimated'} ${money.currency} ${money.amount}`);
  }
  if (!(usage.money || []).length) parts.push('Cost: Unknown');
  else if (usage.money_coverage !== 'complete') parts.push('Cost coverage: Partial');
  return parts.join(' · ');
}

export function failureSupportText(run) {
  if (!run) return '';
  const labels = { http_response: 'HTTP response', harness_reported: 'Reported by worker', adapter_synthesized: 'Recovery status', unavailable: 'Unknown' };
  const source = labels[run.failure_status_source] || 'Unknown';
  const code = Number.isInteger(run.failure_status_code) ? String(run.failure_status_code) : 'Unknown';
  return run.failure_status_code || run.upstream_request_id
    ? `Provider status: ${code} (${source})${run.upstream_request_id ? ` · Support reference: ${run.upstream_request_id}` : ''}` : '';
}
