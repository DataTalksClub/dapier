/* Runs view: one row per run (a workflow's handling of one trigger event),
   and the Zapier-style flow dialog: trigger → each action with its data. */
import { state } from '../state.js';
import { $, icons } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, wrapTokens, formatTimestamp, formatDuration, jsonBlock, emptyRow } from '../format.js';

const STEP_ICONS = {
  webhook: 'webhook',
  slack: 'slack',
  telegram_send: 'send',
  email_send: 'mail',
  dataops: 'database',
  dropbox_upload: 'cloud-upload',
  dropbox_delete: 'trash-2',
  render_html_to_pdf: 'file-text',
};

function triggerLabel(run) {
  return `${run.connector || '?'} · ${run.event_type || '?'}`;
}

function runRow(run) {
  const failed = run.failed_step ? ` <span class="muted-cell mono">(${escapeHtml(run.failed_step)})</span>` : '';
  return `<tr class="run-open" data-run="${escapeHtml(run.run_id)}" role="button" tabindex="0">
    <td class="cell-title mono"><span class="cell-name">${escapeHtml(run.workflow_id || 'Run')}</span></td>
    <td class="mono muted-cell" data-label="Trigger">${escapeHtml(triggerLabel(run))}</td>
    <td data-label="Status">${statusLine(run.status)}${failed}</td>
    <td class="mono muted-cell" data-label="Steps">${run.steps ?? '—'}</td>
    <td class="mono muted-cell" data-label="Started">${escapeHtml(formatTimestamp(run.started_at) || '—')}</td>
  </tr>`;
}

export function renderRuns() {
  const runs = state.data?.runs || [];
  $('#run-table').innerHTML = runs.map(runRow).join('') || emptyRow(5);
}

/* One flow card: head row plus optional data sections. */
function stepCard({ icon, title, badge, status, duration, at, data, error }) {
  const sections = Object.entries(data || {})
    .filter(([, value]) => value !== undefined && value !== null && value !== '')
    .map(([label, value]) => `<div class="flow-data-item"><h4>${escapeHtml(label)}</h4>${jsonBlock(value) || '<p class="detail-muted">Empty</p>'}</div>`)
    .join('');
  return `<div class="flow-step ${error ? 'failed' : ''}">
    <div class="flow-head">
      <span class="flow-icon"><i data-lucide="${icon}"></i></span>
      <span class="flow-title">${escapeHtml(title)}</span>
      ${badge ? `<span class="action-type">${escapeHtml(badge)}</span>` : ''}
      <span class="flow-meta">${[status ? statusLine(status) : '', duration ? escapeHtml(duration) : '', at ? escapeHtml(at) : ''].filter(Boolean).join(' ')}</span>
    </div>
    ${error ? `<div class="detail-error"><i data-lucide="alert-triangle"></i><span>${escapeHtml(error)}</span></div>` : ''}
    ${sections ? `<div class="flow-data">${sections}</div>` : ''}
  </div>`;
}

function flow(data) {
  const run = data.run || {};
  const steps = data.steps || [];
  const firstInput = steps.find((step) => step.input !== undefined && step.input !== null);
  const cards = [];
  cards.push(stepCard({
    icon: 'zap',
    title: 'Trigger',
    badge: `${run.connector || '?'} · ${run.event_type || '?'}`,
    at: formatTimestamp(run.started_at),
    data: firstInput ? { 'Event data': firstInput.input } : null,
  }));
  steps.forEach((step, index) => {
    cards.push(`<div class="flow-link" aria-hidden="true"></div>`);
    cards.push(stepCard({
      icon: STEP_ICONS[step.action_type] || 'arrow-right',
      title: step.action_id || `Step ${index + 1}`,
      badge: step.action_type,
      status: step.status,
      duration: formatDuration(step.duration_ms),
      at: formatTimestamp(step.finished_at),
      data: { Input: step.input, Output: step.output },
      error: step.error,
    }));
  });
  return `<div class="flow">${cards.join('')}</div>`;
}

export async function openRun(runId) {
  $('#run-title').textContent = 'Run';
  $('#run-detail').innerHTML = '<p class="detail-muted">Loading flow…</p>';
  $('#run-dialog').showModal();
  let data;
  try {
    data = await api(`/api/admin/runs/${encodeURIComponent(runId)}`);
  } catch (error) {
    $('#run-detail').innerHTML = `<p class="detail-muted">${escapeHtml(error.message)}</p>`;
    return;
  }
  const run = data.run || {};
  $('#run-title').textContent = run.workflow_id || 'Run';
  $('#run-detail').innerHTML = `
    <div class="detail-summary">
      ${statusLine(run.status)}
      <code>${wrapTokens(run.run_id || runId)}</code>
      ${run.duration_ms != null ? `<span class="flow-total mono">${escapeHtml(formatDuration(run.duration_ms))} total</span>` : ''}
      <button class="button secondary run-replay" type="button" data-run="${escapeHtml(run.run_id || runId)}"
        title="Re-inject this run's original trigger event">Replay</button>
    </div>
    ${flow(data)}`;
  icons();
}
