/* Overview view: metrics, workflow/run tables, and the detail dialogs. */
import { state } from '../state.js';
import { $, icons, showApp, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, triggerLabel, wrapTokens, formatTimestamp, formatDay, emptyRow, configRows, detailRows, pad2 } from '../format.js';
import { openDesigner } from './designer.js';
import { renderConnections } from './connections.js';
import { renderCredentials } from './credentials.js';
import { renderOAuthClients } from './oauth-clients.js';
import { renderTokens } from './tokens.js';

function workflowRow(workflow) {
  return `<tr class="workflow-open" data-workflow="${escapeHtml(workflow.id)}" role="button" tabindex="0">
    <td class="cell-title mono"><span class="cell-name">${escapeHtml(workflow.id)}</span></td>
    <td class="mono muted-cell" data-label="Trigger">${escapeHtml(triggerLabel(workflow))}</td>
    <td data-label="Status">${statusLine(workflow.enabled ? 'enabled' : 'disabled')}</td>
  </tr>`;
}

function openWorkflow(id) {
  const workflow = (state.data.workflows || []).find((item) => item.id === id);
  if (!workflow) return;
  /* The designer canvas is the workflow view; the plain-steps dialog below
     remains only for workflows without a source file in workflows/*.yaml. */
  if (workflow.source) return openDesigner(workflow.source);
  const filters = workflow.trigger.filters || {};
  $('#workflow-title').textContent = workflow.id;
  $('#workflow-detail').innerHTML = `
    <div class="detail-summary">
      ${statusLine(workflow.enabled ? 'enabled' : 'disabled')}
      ${workflow.source ? `<code>workflows/${escapeHtml(workflow.source)}</code>` : ''}
    </div>
    <section class="detail-block">
      <h3>Trigger</h3>
      <p class="detail-trigger">${escapeHtml(triggerLabel(workflow))}</p>
      ${Object.keys(filters).length ? `<h4>Filters</h4>${configRows(filters)}` : ''}
    </section>
    <section class="detail-block">
      <h3>Actions</h3>
      ${(workflow.actions || []).map((action) => `<div class="detail-action">
        <div class="detail-action-head"><span class="action-id">${escapeHtml(action.id)}</span><span class="action-type">${escapeHtml(action.type)}</span></div>
        ${configRows(action)}
      </div>`).join('')}
    </section>`;
  const designer = $('#workflow-edit');
  if (workflow.source) {
    designer.href = `/designer?workflow=${encodeURIComponent(workflow.source)}`;
    designer.dataset.source = workflow.source;
    designer.hidden = false;
  } else {
    designer.hidden = true;
  }
  const edit = $('#workflow-edit-github');
  if (workflow.source && state.data.workflows_edit_base) {
    edit.href = `${state.data.workflows_edit_base}/${encodeURIComponent(workflow.source)}`;
    edit.hidden = false;
  } else {
    edit.hidden = true;
  }
  $('#workflow-dialog').showModal();
  icons();
}

function openRun(executionId) {
  const run = (state.data.executions || []).find((item) => item.execution_id === executionId);
  if (!run) return;
  const parts = String(executionId).split(':');
  const workflowId = run.workflow_id || parts[0];
  const actionId = run.action_id || parts[1];
  $('#run-title').textContent = workflowId || 'Run';
  $('#run-detail').innerHTML = `
    <div class="detail-summary">${statusLine(run.status)}<code>${wrapTokens(executionId)}</code></div>
    ${run.error ? `<div class="detail-error"><i data-lucide="alert-triangle"></i><span>${escapeHtml(run.error)}</span></div>` : ''}
    <section class="detail-block">${detailRows([
      ['Workflow', workflowId],
      ['Action', actionId],
      ['Connector', run.connector],
      ['Event', run.event_type],
      ['Correlation ID', run.correlation_id],
      ['Started', formatTimestamp(run.started_at)],
      ['Finished', formatTimestamp(run.finished_at)],
      ['Retention until', formatTimestamp(run.expires_at)],
    ])}</section>`;
  $('#run-dialog').showModal();
  icons();
}

function render() {
  const data = state.data;
  if (!data) return;
  const enabled = data.workflows.filter((workflow) => workflow.enabled);
  const configured = data.credentials.filter((credential) => credential.configured).length;
  $('#metric-workflows').textContent = enabled.length;
  $('#metric-connections').textContent = data.connections.length;
  $('#metric-runs').textContent = data.executions.filter((execution) => execution.status === 'completed').length;
  $('#metric-credentials').textContent = `${configured}/${data.credentials.length}`;
  $('#overview-workflows').innerHTML = enabled.slice(0, 5).map(workflowRow).join('');
  $('#overview-runs').innerHTML = data.executions.slice(0, 6).map((execution) =>
    `<tr class="run-open" data-run="${escapeHtml(execution.execution_id)}" role="button" tabindex="0"><td class="mono">${wrapTokens(execution.execution_id)}</td><td data-label="Status">${statusLine(execution.status)}</td></tr>`).join('') || emptyRow(2);
  $('#workflow-table').innerHTML = data.workflows.map((workflow) => `<tr class="workflow-open" data-workflow="${escapeHtml(workflow.id)}" role="button" tabindex="0">
    <td class="cell-title mono"><span class="cell-name">${escapeHtml(workflow.id)}</span></td>
    <td class="mono muted-cell" data-label="Trigger">${escapeHtml(triggerLabel(workflow))}</td>
    <td class="mono muted-cell" data-label="Actions">${workflow.actions.map((action) => escapeHtml(action.type)).join(', ')}</td>
    <td data-label="Status">${statusLine(workflow.enabled ? 'enabled' : 'disabled')}</td>
  </tr>`).join('');
  renderConnections(data.connections);
  renderCredentials(data.credentials);
  renderOAuthClients(data.oauth_clients || []);
  renderTokens(data.api_tokens || []);
  $('#run-table').innerHTML = data.executions.map((execution) => `<tr class="run-open" data-run="${escapeHtml(execution.execution_id)}" role="button" tabindex="0">
    <td class="cell-title mono"><span class="cell-name">${wrapTokens(execution.execution_id)}</span></td>
    <td data-label="Status">${statusLine(execution.status)}</td>
    <td class="mono muted-cell" data-label="Retention">${execution.expires_at ? wrapTokens(formatDay(execution.expires_at)) : '—'}</td>
  </tr>`).join('') || emptyRow(3);
  const now = new Date();
  $('#last-updated').textContent = `Updated ${pad2(now.getHours())}:${pad2(now.getMinutes())}`;
  icons();
}

export async function refresh() {
  $('#loading').hidden = false;
  try {
    state.data = await api('/api/admin/overview');
    showApp();
    render();
  } catch (error) {
    if (!$('#app').hidden) notice(error.message, true);
  } finally {
    $('#loading').hidden = true;
  }
}

export function openRowFor(event) {
  const workflowRow = event.target.closest('.workflow-open');
  if (workflowRow) return openWorkflow(workflowRow.dataset.workflow);
  const runRow = event.target.closest('.run-open');
  if (runRow) return openRun(runRow.dataset.run);
}
