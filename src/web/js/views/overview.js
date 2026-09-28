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
import { renderInbox } from './inbox.js';
import { renderSchedules } from './schedules.js';

const TRIGGER_TEXT = {
  'email.message.received': 'An email arrives',
  'dropbox.file.created': 'A Dropbox file is created',
  'renderer.job.completed': 'A render job finishes',
  'youtube.video.published': 'A YouTube video is published',
  'custom.received': 'A custom event arrives',
};
const ACTION_TEXT = {
  webhook: 'Send webhook',
  dataops: 'Run DataOps',
  dropbox_upload: 'Upload to Dropbox',
  dropbox_delete: 'Delete from Dropbox',
  render_html_to_pdf: 'Render PDF',
  sheets_append_row: 'Add row to Sheets',
  slack: 'Post to Slack',
};

function workflowTriggerText(workflow) {
  const trigger = workflow.trigger || {};
  const key = `${trigger.connector}.${trigger.event}`;
  const text = TRIGGER_TEXT[key] || `${trigger.connector || 'Unknown'}: ${String(trigger.event || 'event').replace(/[._]/g, ' ')}`;
  const extra = (workflow.triggerCount || 1) - 1;
  return extra > 0 ? `${text} +${extra} more` : text;
}

function workflowActionText(action) {
  return ACTION_TEXT[action.type] || String(action.type || 'Action').replace(/[_\.]/g, ' ');
}

function workflowRow(workflow) {
  return `<tr class="workflow-open" data-workflow="${escapeHtml(workflow.id)}" role="button" tabindex="0">
    <td class="cell-title mono"><span class="cell-name">${escapeHtml(workflow.id)}</span></td>
    <td class="mono muted-cell" data-label="Trigger">${escapeHtml(triggerLabel(workflow))}</td>
    <td data-label="State">${statusLine(workflow.enabled ? 'enabled' : 'disabled', { enabled: 'On', disabled: 'Off' })}</td>
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

export function openWorkflow(id) {
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
      ${statusLine(workflow.enabled ? 'enabled' : 'disabled', { enabled: 'On', disabled: 'Off' })}
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
  /* Export: the canonical YAML the API renders from the stored definition
     (the same bytes `dapier workflows export` writes). */
  const download = $('#workflow-download-yaml');
  download.dataset.file = workflow.source || '';
  download.hidden = !workflow.source;
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
  renderErrors();
  renderAudit();
  renderUsage();
  renderWorkflows();
  renderConnections(data.connections);
  renderCredentials(data.credentials);
  renderOAuthClients(data.oauth_clients || []);
  renderTokens(data.api_tokens || []);
  renderEmails(data.email_triggers);
  renderRuns();
  renderInbox();
  renderSchedules();
  const now = new Date();
  $('#last-updated').textContent = `Updated ${pad2(now.getHours())}:${pad2(now.getMinutes())}`;
  icons();
}

export function renderWorkflows() {
  const all = state.data?.workflows || [];
  const query = $('#workflow-search').value.trim().toLowerCase();
  const status = $('#workflow-filter').value;
  // The server owns text search once its ?q= result is in
  // (state.workflowSearchIds); while that call is in flight or has failed,
  // the loaded payload filters client-side.
  const serverIds = query ? state.workflowSearchIds : null;
  const matchesText = (workflow) =>
    `${workflow.id} ${triggerLabel(workflow)} ${workflowTriggerText(workflow)} ${(workflow.actions || []).map((action) => `${action.type} ${workflowActionText(action)}`).join(' ')}`.toLowerCase().includes(query);
  const shown = all.filter((workflow) =>
    (serverIds ? serverIds.has(workflow.id) : matchesText(workflow)) &&
    (status === 'all' || workflow.enabled === (status === 'enabled')));
  $('#workflow-count').textContent = `${shown.length} of ${all.length} workflows`;
  const runs = state.data?.runs || [];
  $('#workflow-table').innerHTML = shown.map((workflow) => {
    const recent = runs.find((run) => run.workflow_id === workflow.id);
    const actions = (workflow.actions || []).map((action) => escapeHtml(workflowActionText(action))).join(' <span class="workflow-separator" aria-hidden="true">→</span> ');
    const id = escapeHtml(workflow.id);
    const detail = workflow.source
      ? `<a class="cell-name mono workflow-edit" href="/workflows/${encodeURIComponent(workflow.id)}" data-workflow="${id}">${id}</a>`
      : `<button class="cell-name mono workflow-detail" type="button" data-workflow="${id}">${id}</button>`;
    const edit = workflow.source
      ? `<a class="button secondary workflow-edit" href="/workflows/${encodeURIComponent(workflow.id)}" data-workflow="${id}">Edit</a>`
      : `<button class="button secondary workflow-detail" type="button" data-workflow="${id}">Details</button>`;
    return `<tr class="workflow-list-row">
      <td class="cell-title">${detail}</td>
      <td data-label="When">${escapeHtml(workflowTriggerText(workflow))}</td>
      <td data-label="Do"><span class="workflow-action-chain">${actions || '—'}</span></td>
      <td data-label="Recent run">${recent ? `<button class="workflow-run-link" type="button" data-run="${escapeHtml(recent.run_id)}">${statusLine(recent.status)} <span>${escapeHtml(formatTimestamp(recent.started_at) || '')}</span></button>` : '<span class="muted-cell">No recent runs</span>'}</td>
      <td data-label="State">${statusLine(workflow.enabled ? 'enabled' : 'disabled', { enabled: 'On', disabled: 'Off' })}</td>
      <td class="action-cell workflow-actions" data-label="Manage">
        ${edit}
        <button type="button" class="button secondary workflow-runs" data-workflow="${escapeHtml(workflow.id)}">Runs</button>
        <button type="button" class="button secondary workflow-versions" data-workflow="${escapeHtml(workflow.id)}" ${workflow.source ? '' : 'disabled title="No source file available"'}>Versions</button>
        <button type="button" class="button secondary workflow-toggle" data-file="${escapeHtml(workflow.source || '')}" data-enabled="${workflow.enabled ? 'true' : 'false'}" ${workflow.source ? '' : 'disabled title="No source file available"'}>${workflow.enabled ? 'Turn off' : 'Turn on'}</button>
      </td>
    </tr>`;
  }).join('');
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

function renderErrors() {
  const rows = (state.errors && state.errors.workflows) || [];
  $('#overview-errors').innerHTML = rows.slice(0, 5).map((row) =>
    `<tr><td class="cell-title mono"><button type="button" class="cell-name workflow-runs" data-workflow="${escapeHtml(row.workflow_id)}">${escapeHtml(row.workflow_id)}</button></td><td data-label="Failed">${escapeHtml(row.failed_runs)}</td><td class="mono muted-cell" data-label="Last failure">${escapeHtml(formatTimestamp(row.last_failed_at) || '—')}</td></tr>`).join('');
  $('#overview-errors-empty').hidden = rows.length > 0;
  $('#overview-errors-table').hidden = rows.length === 0;
}

/* Activity: the operator-action audit trail (same rows `dapier audit`
   prints). The action dropdown filters the loaded rows client-side; the API
   exposes the same filter as ?action= for the full trail. */
function renderAudit() {
  const rows = (state.audit && state.audit.events) || [];
  const filter = $('#audit-action-filter');
  const selected = filter.value;
  const actions = [...new Set(rows.map((row) => row.action).filter(Boolean))].sort();
  if (filter.dataset.actions !== actions.join(',')) {
    filter.dataset.actions = actions.join(',');
    filter.innerHTML = '<option value="all">All actions</option>'
      + actions.map((action) => `<option value="${escapeHtml(action)}">${escapeHtml(action)}</option>`).join('');
    filter.value = actions.includes(selected) ? selected : 'all';
  }
  const shown = filter.value === 'all' ? rows : rows.filter((row) => row.action === filter.value);
  $('#overview-audit').innerHTML = shown.slice(0, 8).map((row) =>
    `<tr><td class="mono muted-cell" data-label="When">${escapeHtml(formatTimestamp(row.timestamp) || '—')}</td>`
    + `<td class="mono" data-label="Action">${escapeHtml(row.action || '—')}</td>`
    + `<td class="mono muted-cell" data-label="Actor">${escapeHtml(row.actor_subject || '—')}</td>`
    + `<td data-label="Outcome">${statusLine(row.outcome === 'ok' ? 'completed' : String(row.outcome || 'unknown'), { completed: 'ok' })}`
    + `${row.error ? `<span class="field-hint" title="${escapeHtml(row.error)}">${escapeHtml(row.error)}</span>` : ''}</td></tr>`).join('');
  $('#overview-audit-empty').hidden = rows.length > 0;
  $('#overview-audit-table').hidden = rows.length === 0;
}

$('#audit-action-filter').addEventListener('change', renderAudit);

/* Usage: the per-workflow-per-month task rollup the overview payload
   carries (last 3 months). Ranked by 3-month total; workflows with no
   recorded tasks stay off the list. */
function renderUsage() {
  const rows = (state.data && state.data.usage) || [];
  const now = new Date();
  const monthKey = `${now.getFullYear()}${pad2(now.getMonth() + 1)}`;
  const byWorkflow = new Map();
  for (const row of rows) {
    const tasks = Number(row.tasks) || 0;
    const entry = byWorkflow.get(row.workflow_id) || { current: 0, total: 0 };
    entry.total += tasks;
    if (row.month === monthKey) entry.current += tasks;
    byWorkflow.set(row.workflow_id, entry);
  }
  const shown = [...byWorkflow.entries()].sort((a, b) => b[1].total - a[1].total).slice(0, 5);
  $('#overview-usage').innerHTML = shown.map(([workflowId, entry]) =>
    `<tr><td class="cell-title mono"><button type="button" class="cell-name workflow-runs" data-workflow="${escapeHtml(workflowId)}">${escapeHtml(workflowId)}</button></td>`
    + `<td class="mono" data-label="This month">${entry.current}</td>`
    + `<td class="mono muted-cell" data-label="3-month total">${entry.total}</td></tr>`).join('');
  $('#overview-usage-empty').hidden = shown.length > 0;
  $('#overview-usage-table').hidden = shown.length === 0;
}

let searchSequence = 0;

/* The search box queries server-side (?q= on the overview endpoint — the
   server matches workflow id, description, trigger, and action types),
   debounced so typing does not fire a request per key. A failed fetch falls
   back to the client-side filter over the already-loaded payload. */
async function searchWorkflows() {
  const query = $('#workflow-search').value.trim();
  const stamp = ++searchSequence;
  if (!query) {
    state.workflowSearchIds = null;
    renderWorkflows();
    return;
  }
  try {
    const data = await api(`/api/admin/overview?q=${encodeURIComponent(query)}`);
    if (stamp !== searchSequence) return; // a newer keystroke superseded this
    state.workflowSearchIds = new Set((data.workflows || []).map((workflow) => workflow.id));
  } catch (error) {
    if (stamp !== searchSequence) return;
    state.workflowSearchIds = null;
  }
  renderWorkflows();
}

$('#workflow-search').addEventListener('input', () => {
  clearTimeout(searchWorkflows.timer);
  searchWorkflows.timer = setTimeout(searchWorkflows, 250);
});
$('#workflow-filter').addEventListener('change', renderWorkflows);

export async function refresh() {
  $('#loading').hidden = false;
  try {
    state.data = await api('/api/admin/overview');
    render();
    showApp();
    // Failed runs by workflow trails the overview fetch so it never delays
    // the page; a miss leaves the section quietly empty.
    try {
      state.errors = await api('/api/admin/errors/summary?days=7');
    } catch (summaryError) {
      state.errors = null;
    }
    renderErrors();
    // The audit trail trails too; a miss leaves Activity quietly empty.
    try {
      state.audit = await api('/api/admin/audit?limit=20');
    } catch (auditError) {
      state.audit = null;
    }
    renderAudit();
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

/* Version history: every save, toggle, and rollback publishes a version
   record; Restore republishes an old one as the next revision. */
async function loadVersions(file) {
  $('#versions-detail').innerHTML = '<p class="sub">Loading…</p>';
  $('#versions-feedback').hidden = true;
  let data;
  try {
    data = await api(`/api/admin/designer/workflows/${encodeURIComponent(file)}/versions`);
  } catch (error) {
    $('#versions-detail').innerHTML = `<p class="sub">${escapeHtml(error.message)}</p>`;
    return;
  }
  const rows = (data.versions || []).map((version) => `<tr>
    <td class="mono">v${version.revision}${version.current ? ' <span class="muted-cell">(current)</span>' : ''}</td>
    <td data-label="When" class="mono muted-cell">${escapeHtml(formatTimestamp(version.published_at) || version.published_at || '—')}</td>
    <td data-label="Change">${escapeHtml(version.cause || 'save')}</td>
    <td data-label="By" class="mono muted-cell">${escapeHtml(version.published_by || '—')}</td>
    <td data-label="State">${version.enabled ? 'On' : 'Off'}</td>
    <td class="action-cell" data-label="Manage">${version.current ? '' : `<button type="button" class="button secondary version-restore" data-revision="${version.revision}">Restore</button>`}</td>
  </tr>`).join('');
  $('#versions-detail').innerHTML = rows
    ? `<div class="table-wrap"><table><thead><tr><th>Version</th><th>When</th><th>Change</th><th>By</th><th>State</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
    : '<p class="sub">No version history yet. Every save, toggle, and rollback is recorded from now on.</p>';
  icons();
}

export async function openVersions(id) {
  const workflow = (state.data.workflows || []).find((item) => item.id === id);
  if (!workflow || !workflow.source) return;
  $('#versions-title').textContent = `Version history — ${workflow.id}`;
  $('#versions-dialog').dataset.file = workflow.source;
  $('#versions-dialog').showModal();
  await loadVersions(workflow.source);
}

export async function restoreVersion(button) {
  const dialog = $('#versions-dialog');
  const file = dialog.dataset.file;
  const revision = button.dataset.revision;
  const feedback = $('#versions-feedback');
  /* Restoring republishes the old definition as the next revision; confirm
     first — the current definition stays restorable in this list. */
  const confirmDialog = $('#versions-confirm-dialog');
  $('#versions-confirm-message').textContent =
    `Restore ${file} to v${revision}? It is republished live and committed to Git; the current definition stays in this history.`;
  confirmDialog.returnValue = '';
  confirmDialog.showModal();
  const confirmed = await new Promise(
    (resolve) => confirmDialog.addEventListener('close', () => resolve(confirmDialog.returnValue === 'confirm'), { once: true }));
  if (!confirmed) return;
  button.disabled = true;
  try {
    const data = await api(`/api/admin/designer/workflows/${encodeURIComponent(file)}/rollback`, {
      method: 'POST',
      body: JSON.stringify({ revision: Number(revision) }),
    });
    feedback.textContent = data.published
      ? `Restored v${revision} — live now.`
      : `Restored v${revision}. The deploy pipeline publishes it in a few minutes.`;
    feedback.hidden = false;
    await refresh();
    await loadVersions(file);
  } catch (error) {
    feedback.textContent = error.message;
    feedback.hidden = false;
    button.disabled = false;
  }
  icons();
}

export function openRowFor(event) {
  if (event.target.closest('.workflow-toggle')) return; // the toggle handles itself
  const workflowRow = event.target.closest('.workflow-open');
  if (workflowRow) return openWorkflow(workflowRow.dataset.workflow);
  const runRow = event.target.closest('.run-open');
  if (runRow) return openRun(runRow.dataset.run);
}
