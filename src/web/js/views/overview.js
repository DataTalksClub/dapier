/* Overview view: metrics, workflow/run tables, and the detail dialogs. */
import { state } from '../state.js';
import { $, icons, showApp, showStartupError, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, triggerLabel, configRows, pad2, formatTimestamp } from '../format.js';
import { openDesigner, postConnectionsToDesigner } from './designer.js';
import { renderConnections } from './connections.js';
import { renderCredentials } from './credentials.js';
import { renderOAuthClients } from './oauth-clients.js';
import { renderTokens } from './tokens.js';
import { renderEmails } from './emails.js';
import { renderRuns, openRun } from './runs.js';

function workflowRow(workflow) {
  return `<tr class="workflow-open" data-workflow="${escapeHtml(workflow.id)}" role="button" tabindex="0">
    <td class="cell-title mono"><span class="cell-name">${escapeHtml(workflow.id)}</span></td>
    <td class="mono muted-cell" data-label="Trigger">${escapeHtml(triggerLabel(workflow))}</td>
    <td data-label="Status">${statusLine(workflow.enabled ? 'enabled' : 'disabled')}</td>
  </tr>`;
}

function triggerBlocks(workflow) {
  const many = (workflow.triggerCount || 1) > 1;
  const list = many && Array.isArray(workflow.triggers) && workflow.triggers.length
    ? workflow.triggers : [workflow.trigger];
  return list.map((trigger) => {
    const filters = (trigger && trigger.filters) || {};
    const label = `${trigger.connector || '?'} · ${trigger.event || '?'}`;
    return `<p class="detail-trigger">${escapeHtml(label)}</p>
      ${Object.keys(filters).length ? `<h4>Filters</h4>${configRows(filters)}` : ''}`;
  }).join('');
}

function openWorkflow(id) {
  const workflow = (state.data.workflows || []).find((item) => item.id === id);
  if (!workflow) return;
  /* The designer canvas is the workflow view at /workflows/<id>; the
     plain-steps dialog below remains only for workflows without a source
     file in workflows/*.yaml. */
  if (workflow.source) return openDesigner(workflow.id);
  const many = (workflow.triggerCount || 1) > 1;
  $('#workflow-title').textContent = workflow.id;
  $('#workflow-detail').innerHTML = `
    <div class="detail-summary">
      ${statusLine(workflow.enabled ? 'enabled' : 'disabled')}
      ${workflow.source ? `<code>workflows/${escapeHtml(workflow.source)}</code>` : ''}
    </div>
    <section class="detail-block">
      <h3>Trigger${many ? 's' : ''}</h3>
      ${triggerBlocks(workflow)}
      ${workflow.flow ? `<h4>Shared flow</h4><p class="detail-trigger">${escapeHtml(workflow.flow)}</p>` : ''}
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
    designer.href = `/workflows/${encodeURIComponent(workflow.id)}`;
    designer.dataset.workflow = workflow.id;
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

function render() {
  const data = state.data;
  if (!data) return;
  const enabled = data.workflows.filter((workflow) => workflow.enabled);
  const configured = data.credentials.filter((credential) => credential.configured).length;
  const connected = data.connections.filter((connection) => connection.status === 'connected').length;
  const attention = data.connections.filter((connection) => ['ready', 'expired', 'revoked'].includes(connection.status)).length;
  const recentRuns = data.runs || [];
  $('#metric-workflows').textContent = enabled.length;
  $('#metric-connections').textContent = connected;
  $('#metric-connection-detail').textContent = attention ? `${attention} need attention` : 'No setup issues';
  $('#metric-runs').textContent = recentRuns.filter((run) => run.status === 'completed').length;
  $('#metric-runs-detail').textContent = recentRuns.length ? `of ${recentRuns.length} available runs` : 'No recent runs';
  $('#metric-credentials').textContent = `${configured}/${data.credentials.length}`;
  $('#overview-workflows').innerHTML = enabled.slice(0, 5).map(workflowRow).join('');
  $('#overview-workflows-empty').hidden = enabled.length > 0;
  $('#overview-workflows-table').hidden = enabled.length === 0;
  $('#overview-runs').innerHTML = (data.runs || []).slice(0, 6).map((run) =>
    `<tr class="run-open" data-run="${escapeHtml(run.run_id)}" role="button" tabindex="0"><td class="mono">${escapeHtml(run.workflow_id || 'Run')}</td><td data-label="Status">${statusLine(run.status)}</td><td class="mono muted-cell" data-label="Started">${escapeHtml(formatTimestamp(run.started_at) || '—')}</td></tr>`).join('');
  $('#overview-runs-empty').hidden = recentRuns.length > 0;
  $('#overview-runs-table').hidden = recentRuns.length === 0;
  renderAttention(data);
  renderWorkflows();
  renderConnections(data.connections);
  renderCredentials(data.credentials);
  renderOAuthClients(data.oauth_clients || []);
  renderTokens(data.api_tokens || []);
  renderEmails(data.email_triggers);
  renderRuns();
  const now = new Date();
  $('#last-updated').textContent = `Updated ${pad2(now.getHours())}:${pad2(now.getMinutes())}`;
  icons();
}

export function renderWorkflows() {
  const all = state.data?.workflows || [];
  const query = $('#workflow-search').value.trim().toLowerCase();
  const status = $('#workflow-filter').value;
  const shown = all.filter((workflow) =>
    `${workflow.id} ${triggerLabel(workflow)}`.toLowerCase().includes(query) &&
    (status === 'all' || workflow.enabled === (status === 'enabled')));
  $('#workflow-count').textContent = `${shown.length} of ${all.length} workflows`;
  $('#workflow-table').innerHTML = shown.map((workflow) => `<tr class="workflow-open" data-workflow="${escapeHtml(workflow.id)}" role="button" tabindex="0">
    <td class="cell-title mono"><span class="cell-name">${escapeHtml(workflow.id)}</span></td>
    <td class="mono muted-cell" data-label="Trigger">${escapeHtml(triggerLabel(workflow))}</td>
    <td class="mono muted-cell" data-label="Actions">${workflow.actions.map((action) => escapeHtml(action.type)).join(', ')}</td>
    <td data-label="Status"><span class="workflow-status">${statusLine(workflow.enabled ? 'enabled' : 'disabled')}${workflow.published ? '' : ' <span class="muted-cell">(deploying)</span>'}
      <button type="button" class="workflow-toggle" data-file="${escapeHtml(workflow.source || '')}" data-enabled="${workflow.enabled ? 'true' : 'false'}">${workflow.enabled ? 'Disable' : 'Enable'}</button></span></td>
  </tr>`).join('');
  $('#workflow-empty').hidden = all.length > 0;
  $('#workflow-filter-empty').hidden = all.length === 0 || shown.length > 0;
  $('#workflow-table-wrap').hidden = shown.length === 0;
}

function renderAttention(data) {
  const failed = (data.runs || []).filter((run) => ['failed', 'error'].includes(run.status));
  const connections = data.connections.filter((connection) => connection.status !== 'connected');
  const items = [];
  if (failed.length) items.push(`<a class="attention-item view-link" href="/runs" data-target="runs" data-run-status="problems"><strong>${failed.length} failed ${failed.length === 1 ? 'run' : 'runs'} in recent history</strong><span>Inspect failures →</span></a>`);
  if (connections.length) items.push(`<a class="attention-item view-link" href="/connections" data-target="connections" data-connection-status="attention"><strong>${connections.length} ${connections.length === 1 ? 'account needs' : 'accounts need'} attention</strong><span>Complete setup or reconnect →</span></a>`);
  $('#overview-attention').innerHTML = items.length
    ? `<h3>Needs attention</h3><div class="attention-items">${items.join('')}</div>`
    : `<h3>${data.workflows.length ? 'No issues in recent history' : 'Start your first automation'}</h3><p class="sub">${data.workflows.length ? 'No failed runs or disconnected accounts in the loaded records.' : 'Connect an account, then create a workflow to automate a task.'}</p>${data.workflows.length ? '' : '<a class="text-link view-link" href="/connections" data-target="connections">Connect an account →</a>'}`;
}

$('#workflow-search').addEventListener('input', renderWorkflows);
$('#workflow-filter').addEventListener('change', renderWorkflows);

export async function refresh() {
  $('#loading').hidden = false;
  try {
    state.data = await api('/api/admin/overview');
    render();
    showApp();
    // Fresh connections data for an open designer: the iframe may have
    // mounted before this fetch answered.
    if (state.view === 'designer') postConnectionsToDesigner();
    return true;
  } catch (error) {
    if (!$('#forbidden-view').hidden) return;
    if ($('#app').hidden) showStartupError(error.message);
    else notice(error.message, true);
    return false;
  } finally {
    $('#loading').hidden = true;
  }
}

export function openRowFor(event) {
  if (event.target.closest('.workflow-toggle')) return; // the toggle handles itself
  const workflowRow = event.target.closest('.workflow-open');
  if (workflowRow) return openWorkflow(workflowRow.dataset.workflow);
  const runRow = event.target.closest('.run-open');
  if (runRow) return openRun(runRow.dataset.run);
}
