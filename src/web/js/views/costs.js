import { $ } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, formatTimestamp } from '../format.js';
const usd = (value) => value == null ? 'Unknown' : `$${Number(value).toFixed(6)}`;
export async function renderCosts() {
  const summary = $('#costs-summary');
  summary.textContent = 'Loading costs…';
  try {
    const data = await api(`/api/admin/costs?months=${Number($('#costs-months').value)}`);
    summary.textContent = `${usd(data.reported_usd)} reported · ${usd(data.estimated_usd)} estimated · ${data.unpriced_requests} unpriced requests${data.truncated ? ' · Partial results: totals cover returned requests only' : ''}${data.tracking_enabled ? '' : ' · Tracking is not enabled'}`;
    $('#costs-rows').innerHTML = (data.requests || []).map((row) => `<tr><td>${escapeHtml(formatTimestamp(row.timestamp))}</td><td>${escapeHtml(row.provider)}<br>${escapeHtml(row.model)}</td><td>${escapeHtml(row.context === 'connection-test' ? `Connection test: ${row.connection_id}` : row.workflow_id || row.context)}</td><td>${row.input_tokens == null ? '?' : escapeHtml(row.input_tokens)} / ${row.output_tokens == null ? '?' : escapeHtml(row.output_tokens)}</td><td>${usd(row.cost_usd)}</td><td>${escapeHtml(row.cost_source)}</td></tr>`).join('') || '<tr><td colspan="6">No paid API requests recorded for this period.</td></tr>';
  } catch (error) {
    summary.textContent = error.message;
  }
}
$('#costs-months').addEventListener('change', renderCosts);
