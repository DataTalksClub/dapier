/* Overview view: metrics, workflow/run tables, and the detail dialogs. */
import { createOverviewLoader } from '../overview-loader.js';
import { homeModel } from '../home-model.js';
import { state } from '../state.js';
import { $, icons, showApp, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, triggerLabel, configRows, pad2, formatTimestamp } from '../format.js';
import { openDesigner, postConnectionsToDesigner } from './designer.js';
import { renderConnections } from './connections.js';
import { renderCredentials } from './credentials.js';
import { renderOAuthClients } from './oauth-clients.js';
import { renderTokens } from './tokens.js';
import { renderEmails, renderEmailFrom } from './emails.js';
import { renderAgentTasks } from './agents.js';
import { renderWorkers } from './workers.js';
import { renderRuns, openRun } from './runs.js';
import { renderInbox } from './inbox.js';
import { renderSchedules } from './schedules.js';
import { renderTriggers } from './triggers.js';
import { renderStorage } from './storage.js';

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

/* The State cell: auto-paused outranks the on/off toggle — the workflow is
   On but the engine stopped it after consecutive failed runs (the API's
   auto_paused fields; re-enabling is the resume verb). */
function workflowState(workflow) {
  if (workflow.auto_paused) {
    const failures = Number(workflow.failures || 0);
    return statusLine('auto-paused', { 'auto-paused': `Auto-paused (${failures} failed run${failures === 1 ? '' : 's'})` });
  }
  return statusLine(workflow.enabled ? 'enabled' : 'disabled', { enabled: 'On', disabled: 'Off' });
}

function workflowRow(workflow) {
  const latest = homeModel(state.data).latest.get(workflow.id);
  return `<article class="home-workflow">
    <div><button type="button" class="cell-name workflow-detail" data-workflow="${escapeHtml(workflow.id)}">${escapeHtml(workflow.id)}</button>
      <p class="sub">${escapeHtml(workflow.description || workflowTriggerText(workflow))}</p>
      <div class="home-workflow-state">${workflowState(workflow)}${latest ? `<span class="sub">Latest run: ${statusLine(latest.status)}</span>` : `<span class="sub">${state.loadedSections.has('activity') ? 'No recent runs' : 'Loading recent runs…'}</span>`}</div>
    </div>
    <button type="button" class="dk-button dk-button--secondary workflow-detail" data-workflow="${escapeHtml(workflow.id)}">${workflow.source ? 'Open' : 'Details'}</button>
  </article>`;
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
     file that no trigger runs either. Hook-backed workflows (run by a
     stored webhook/telegram/... trigger, no source file) open in the
     designer too — the API serves them by id and the canvas is read-only. */
  if (workflow.source || workflow.hook_backed) return openDesigner(workflow.id);
  const many = (workflow.triggerCount || 1) > 1;
  $('#workflow-title').textContent = workflow.id;
  $('#workflow-detail').innerHTML = `
    <div class="detail-summary">
      ${workflowState(workflow)}
      ${workflow.source ? `<code>workflows/${escapeHtml(workflow.source)}</code>` : ''}
      ${workflow.auto_paused ? `<p class="sub">Paused by the engine after ${escapeHtml(String(workflow.failures || 0))} failed run(s) ${escapeHtml(formatTimestamp(workflow.auto_paused_at) || '')} — last error: ${escapeHtml(workflow.auto_paused_reason || 'unknown')}. Turn the workflow off and on again (or Resume) to clear it.</p>` : ''}
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
  /* Export: the canonical YAML the API renders from the stored definition
     (the same bytes `dapier workflows export` writes). */
  const download = $('#workflow-download-yaml');
  download.dataset.file = workflow.source || '';
  download.hidden = !workflow.source;
  $('#workflow-dialog').showModal();
  icons();
}

function render(section) {
  const data = state.data;
  if (!data) return;
  const model = homeModel(data);
  if (['workflows', 'activity'].includes(section) && data.workflows) {
    $('#home-workflow-count').textContent = `${data.workflows.length} workflow${data.workflows.length === 1 ? '' : 's'} · ${model.running} on`;
    $('#overview-workflows').innerHTML = model.workflows.slice(0, 5).map(workflowRow).join('');
    $('#overview-workflows-empty').hidden = data.workflows.length > 0;
  }
  if (section === 'activity') {
    $('#overview-runs').innerHTML = model.runs.slice(0, 6).map((run) =>
      `<button type="button" class="home-result workflow-run-link" data-run="${escapeHtml(run.run_id)}">
        <span class="home-result-title">${escapeHtml(run.workflow_id || 'Run')}${statusLine(run.status)}</span>
        <span class="sub">${escapeHtml(formatTimestamp(run.started_at) || '—')}${run.failed_step ? ` · Failed at ${escapeHtml(run.failed_step)}` : ''}</span>
      </button>`).join('');
    $('#overview-runs-empty').hidden = model.runs.length > 0;
  }
  renderAttention(data);
  if (section === 'usage') renderUsage();
  if (['workflows', 'activity'].includes(section) && data.workflows) renderWorkflows();
  if (section === 'connections') renderConnections(data.connections);
  if (section === 'credentials') {
    renderCredentials(data.credentials);
    renderOAuthClients(data.oauth_clients || []);
  }
  if (section === 'tokens') renderTokens(data.api_tokens || []);
  if (section === 'emails') {
    renderEmails(data.email_triggers);
    renderEmailFrom(data.email_from);
  }
  if (data.runs && ['workflows', 'activity'].includes(section)) renderRuns();
  if (section === 'workflows') renderStorage();
  const now = new Date();
  $('#last-updated').textContent = `Updated ${pad2(now.getHours())}:${pad2(now.getMinutes())}`;
  icons();
}

export function renderWorkflows() {
  const all = state.data?.workflows || [];
  const query = $('#workflow-search').value.trim().toLowerCase();
  const status = $('#workflow-filter').value;
  // Zapier-style tag filter: the option list tracks the tags actually in
  // use (client-side; the API exposes the same narrowing as ?tag=).
  const tagSelect = $('#workflow-tag-filter');
  const tagNames = [...new Set(all.flatMap((workflow) => workflow.tags || []).map((tag) => String(tag).toLowerCase()))].sort();
  if (tagSelect.dataset.tags !== tagNames.join(',')) {
    const selectedTag = tagSelect.value;
    tagSelect.dataset.tags = tagNames.join(',');
    tagSelect.innerHTML = '<option value="all">All tags</option>'
      + tagNames.map((tag) => `<option value="${escapeHtml(tag)}">${escapeHtml(tag)}</option>`).join('');
    tagSelect.value = tagNames.includes(selectedTag) ? selectedTag : 'all';
  }
  const tagFilter = tagSelect.value;
  // Zapier-style folder filter, beside the tag one: flat folders, one per
  // workflow (the API exposes the same narrowing as ?folder=).
  const folderSelect = $('#workflow-folder-filter');
  const folderNames = [...new Set(all.flatMap((workflow) => {
    const folder = String(workflow.folder || '').trim();
    return folder ? [folder] : [];
  }))].sort((a, b) => a.localeCompare(b));
  if (folderSelect.dataset.folders !== folderNames.join(',')) {
    const selectedFolder = folderSelect.value;
    folderSelect.dataset.folders = folderNames.join(',');
    folderSelect.innerHTML = '<option value="all">All folders</option>'
      + folderNames.map((folder) => `<option value="${escapeHtml(folder)}">${escapeHtml(folder)}</option>`).join('');
    folderSelect.value = folderNames.includes(selectedFolder) ? selectedFolder : 'all';
  }
  const folderFilter = folderSelect.value;
  // The server owns text search once its ?q= result is in
  // (state.workflowSearchIds); while that call is in flight or has failed,
  // the loaded payload filters client-side.
  const serverIds = query ? state.workflowSearchIds : null;
  const matchesText = (workflow) =>
    `${workflow.id} ${triggerLabel(workflow)} ${workflowTriggerText(workflow)} ${(workflow.actions || []).map((action) => `${action.type} ${workflowActionText(action)}`).join(' ')}`.toLowerCase().includes(query);
  const shown = all.filter((workflow) =>
    (serverIds ? serverIds.has(workflow.id) : matchesText(workflow)) &&
    (status === 'all' || workflow.enabled === (status === 'enabled')) &&
    (tagFilter === 'all' || (workflow.tags || []).some((tag) => String(tag).toLowerCase() === tagFilter)) &&
    (folderFilter === 'all' || String(workflow.folder || '').trim().toLowerCase() === folderFilter.toLowerCase()));
  $('#workflow-count').textContent = `${shown.length} of ${all.length} shown`;
  const runs = state.data?.runs || [];
  $('#workflow-table').innerHTML = shown.map((workflow, index) => {
    const recent = runs.find((run) => run.workflow_id === workflow.id);
    const actions = (workflow.actions || []).map((action) => escapeHtml(workflowActionText(action))).join(' <span class="workflow-separator" aria-hidden="true">→</span> ');
    const id = escapeHtml(workflow.id);
    const opensDesigner = workflow.source || workflow.hook_backed;
    const detail = opensDesigner
      ? `<a class="cell-name workflow-edit" href="/workflows/${encodeURIComponent(workflow.id)}" data-workflow="${id}">${id}</a>`
      : `<button class="cell-name workflow-detail" type="button" data-workflow="${id}">${id}</button>`;
    const edit = opensDesigner
      ? `<a class="dk-button dk-button--secondary workflow-edit" href="/workflows/${encodeURIComponent(workflow.id)}" data-workflow="${id}">Edit</a>`
      : `<button class="dk-button dk-button--secondary workflow-detail" type="button" data-workflow="${id}">Details</button>`;
    const tags = (workflow.tags || []).map((tag) => `<span class="tag-chip">${escapeHtml(tag)}</span>`).join(' ');
    const folderChip = String(workflow.folder || '').trim()
      ? `<span class="tag-chip folder-chip" title="Folder">${escapeHtml(String(workflow.folder).trim())}</span>` : '';
    const sourceButtons = workflow.source ? '' : 'disabled title="No source file available"';
    const selected = selectedWorkflows.has(workflow.source);
    return `<tr class="workflow-list-row">
      <td class="select-col" data-label="Select"><input type="checkbox" class="workflow-select" data-workflow="${id}" data-file="${escapeHtml(workflow.source || '')}" aria-label="Select ${id}" ${selected ? 'checked' : ''} ${workflow.source ? '' : 'disabled title="No source file available"'}></td>
      <td class="cell-title">${detail}${(folderChip || tags) ? `<div class="cell-tags">${folderChip} ${tags}</div>` : ''}</td>
      <td class="workflow-flow" data-label="Flow"><div class="workflow-flow-line"><span class="workflow-flow-label">When</span><span>${escapeHtml(workflowTriggerText(workflow))}</span></div><div class="workflow-flow-line"><span class="workflow-flow-label">Then</span><span class="workflow-action-chain" title="${escapeHtml((workflow.actions || []).map(workflowActionText).join(' → '))}">${actions || '—'}</span></div></td>
      <td data-label="Latest run">${recent ? `<button class="workflow-run-link" type="button" aria-label="Inspect latest run for ${id}" data-run="${escapeHtml(recent.run_id)}">${statusLine(recent.status)} <span>${escapeHtml(formatTimestamp(recent.started_at) || '')}</span></button>` : `<span class="muted-cell">${state.loadedSections.has('activity') ? 'No runs yet' : 'Loading recent runs…'}</span>`}</td>
      <td data-label="State"><div class="workflow-state-control">${workflow.auto_paused
          ? `${statusLine('auto-paused', { 'auto-paused': 'Auto-paused' })}<span class="visually-hidden">${Number(workflow.failures || 0)} failed runs</span><button type="button" class="dk-button dk-button--secondary workflow-resume" data-file="${escapeHtml(workflow.source || '')}" ${sourceButtons} title="Re-enable — clears the auto-pause and resets the failure streak">Resume</button>`
          : `<button type="button" class="workflow-switch workflow-toggle" role="switch" aria-checked="${workflow.enabled ? 'true' : 'false'}" aria-label="Enable ${id}" data-file="${escapeHtml(workflow.source || '')}" data-enabled="${workflow.enabled ? 'true' : 'false'}" ${sourceButtons}><span class="workflow-switch-track" aria-hidden="true"></span><span aria-hidden="true">${workflow.enabled ? 'On' : 'Off'}</span></button>`}
      </div></td>
      <td class="action-cell workflow-actions" data-label="Manage">
        <button type="button" class="icon-button workflow-more" popovertarget="workflow-menu-${index}" aria-label="More actions for ${id}"><i data-lucide="more-horizontal" aria-hidden="true"></i></button>
        <div id="workflow-menu-${index}" class="workflow-menu" popover aria-label="Actions for ${id}">
        ${edit}
        <button type="button" class="dk-button dk-button--secondary workflow-runs" data-workflow="${escapeHtml(workflow.id)}">Runs</button>
        <button type="button" class="dk-button dk-button--secondary workflow-versions" data-workflow="${escapeHtml(workflow.id)}" ${sourceButtons}>Versions</button>
        <button type="button" class="dk-button dk-button--secondary workflow-tags" data-file="${escapeHtml(workflow.source || '')}" data-tags="${escapeHtml((workflow.tags || []).join(','))}" ${sourceButtons}>Tags</button>
        <button type="button" class="dk-button dk-button--secondary workflow-folder" data-file="${escapeHtml(workflow.source || '')}" data-workflow="${escapeHtml(workflow.id)}" data-folder="${escapeHtml(String(workflow.folder || '').trim())}" ${sourceButtons}>Folder</button>
        <button type="button" class="dk-button dk-button--secondary workflow-duplicate" data-file="${escapeHtml(workflow.source || '')}" data-workflow="${escapeHtml(workflow.id)}" ${sourceButtons} title="Copy this workflow under a new name">Duplicate</button>
        <button type="button" class="dk-button dk-button--danger workflow-delete" data-file="${escapeHtml(workflow.source || '')}" data-workflow="${escapeHtml(workflow.id)}" ${sourceButtons}>Delete</button>
        </div>
      </td>
    </tr>`;
  }).join('');
  $('#workflow-empty').hidden = all.length > 0;
  $('#workflow-filter-empty').hidden = all.length === 0 || shown.length > 0;
  $('#workflow-table-wrap').hidden = shown.length === 0;
  renderBulkBar(shown);
}

/* Bulk selection: checkboxes collect file names across the shown rows; the
   bar applies one enable/disable call to all of them at once. Selection
   survives re-renders (filtering, refresh) and drops files that leave the
   list. */
const selectedWorkflows = new Set();

function selectableFiles(rows) {
  return rows.map((workflow) => workflow.source).filter(Boolean);
}

function renderBulkBar(shown) {
  const files = selectableFiles(shown);
  selectedWorkflows.forEach((file) => {
    if (!files.includes(file)) selectedWorkflows.delete(file);
  });
  /* Scope buttons: with a tag filter or search active, the whole shown set is
     one bulk target — no checkbox picking needed. The count is exact: the
     same rows the table shows are the ids the call sends. */
  const query = $('#workflow-search').value.trim();
  const tagFilter = $('#workflow-tag-filter').value;
  const folderFilter = $('#workflow-folder-filter').value;
  const scoped = (query !== '' || tagFilter !== 'all' || folderFilter !== 'all') && files.length > 0;
  const scopeNote = $('#workflow-scope-note');
  const scopeLabel = tagFilter !== 'all' ? `tag “${tagFilter}”`
    : folderFilter !== 'all' ? `folder “${folderFilter}”` : `search “${query}”`;
  if (scopeNote) {
    scopeNote.hidden = !scoped;
    scopeNote.textContent = scoped ? `${files.length} match ${scopeLabel}` : '';
  }
  const pauseShown = $('#workflow-scope-pause');
  const resumeShown = $('#workflow-scope-resume');
  if (pauseShown) {
    pauseShown.hidden = !scoped;
    pauseShown.textContent = `Pause shown (${files.length})`;
    pauseShown.dataset.scope = scopeLabel;
  }
  if (resumeShown) {
    resumeShown.hidden = !scoped;
    resumeShown.textContent = `Resume shown (${files.length})`;
    resumeShown.dataset.scope = scopeLabel;
  }
  const bar = $('#workflow-bulk-bar');
  bar.hidden = selectedWorkflows.size === 0 && !scoped;
  $('#workflow-bulk-count').textContent = selectedWorkflows.size
    ? `${selectedWorkflows.size} selected` : '';
  $('#workflow-select-all').checked = files.length > 0
    && files.every((file) => selectedWorkflows.has(file));
}

function renderAttention(data) {
  const model = homeModel(data);
  const items = model.problems.map(({ workflow, run }) => `<article class="home-problem">
    <div><strong>${escapeHtml(workflow.id)} ${workflow.auto_paused ? 'is auto-paused' : 'failed its latest run'}</strong>
      <p class="sub">${escapeHtml(workflow.auto_paused_reason || (run?.failed_step ? `Failed at ${run.failed_step}` : 'Open the run to see what went wrong.'))}</p></div>
    <div class="home-create-actions">${run ? `<button class="dk-button dk-button--secondary workflow-run-link" type="button" data-run="${escapeHtml(run.run_id)}">Inspect failure</button>` : ''}<button class="dk-button dk-button--secondary workflow-detail" type="button" data-workflow="${escapeHtml(workflow.id)}">Open workflow</button></div>
  </article>`);
  for (const connection of model.connections) items.push(`<article class="home-problem">
    <div><strong>${escapeHtml(connection.display_name || connection.connection_id)} needs attention</strong><p class="sub">${connection.status === 'ready' ? 'Finish setup to use this account.' : 'Check this account’s access before its next run.'}</p></div>
    <button class="dk-button dk-button--secondary home-connection" type="button" data-connection="${escapeHtml(connection.connection_id)}">Manage connection</button>
  </article>`);
  if (model.quotaBlocked) items.unshift('<article class="home-problem"><div><strong>Monthly task limit reached</strong><p class="sub">Workflow actions are blocked until the limit is raised or the month resets.</p></div><a class="dk-button dk-button--secondary view-link" href="/runs" data-target="runs">Review limit</a></article>');
  $('#overview-attention').classList.toggle('home-needs-attention', items.length > 0);
  $('#overview-attention').hidden = items.length === 0;
  $('#overview-attention').innerHTML = items.length ? `<h3>Needs attention</h3>${items.join('')}` : '';
}

function renderErrors() {
  const rows = (state.errors && state.errors.workflows) || [];
  $('#overview-errors').innerHTML = rows.slice(0, 5).map((row) =>
    `<tr><td class="cell-title mono"><button type="button" class="cell-name workflow-runs" data-workflow="${escapeHtml(row.workflow_id)}">${escapeHtml(row.workflow_id)}</button></td><td data-label="Failed">${escapeHtml(row.failed_runs)}</td><td class="mono muted-cell" data-label="Last failure">${escapeHtml(formatTimestamp(row.last_failed_at) || '—')}</td></tr>`).join('');
  $('#overview-errors-empty').hidden = rows.length > 0;
  $('#overview-errors-table').hidden = rows.length === 0;
}

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
  const shown = [...byWorkflow.entries()].sort((a, b) => b[1].total - a[1].total);
  $('#overview-usage').innerHTML = shown.map(([workflowId, entry]) =>
    `<tr><td class="cell-title mono"><button type="button" class="cell-name workflow-runs" data-workflow="${escapeHtml(workflowId)}">${escapeHtml(workflowId)}</button></td>`
    + `<td class="mono" data-label="This month">${entry.current}</td>`
    + `<td class="mono muted-cell" data-label="3-month total">${entry.total}</td></tr>`).join('');
  $('#overview-usage-empty').hidden = shown.length > 0;
  $('#overview-usage-table').hidden = shown.length === 0;
  renderQuota();
}

/* The monthly task quota (the budget the worker enforces on action steps):
   a status line plus an inline limit editor. Absent payload = metering is
   not wired, so the editor stays hidden rather than pretending to work. */
function renderQuota() {
  const quota = (state.data && state.data.quota) || null;
  const line = $('#overview-quota-line');
  const form = $('#quota-form');
  if (!line || !form) return;
  line.hidden = !quota;
  form.hidden = !quota;
  if (!quota) return;
  const used = Number(quota.used) || 0;
  if (quota.enabled) {
    const left = quota.remaining;
    line.textContent = `Monthly limit ${quota.limit} — ${used} used`
      + (left != null ? `, ${left} left this month (${quota.month}).` : ` (${quota.month}).`);
  } else {
    line.textContent = `No monthly limit — ${used} tasks used this month (${quota.month}).`;
  }
  $('#quota-limit').value = quota.enabled ? quota.limit : '';
}

$('#quota-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const raw = $('#quota-limit').value.trim();
  const limit = raw === '' ? 'off' : Number(raw);
  try {
    await api('/api/admin/quota', { method: 'PUT', body: JSON.stringify({ limit }) });
    notice(limit === 'off' ? 'Task quota removed — usage is uncapped again.'
                           : `Monthly task quota set to ${limit}.`);
    await refresh();
  } catch (error) {
    notice(error.message || 'Could not set the task quota.', true);
  }
});

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
    const data = await api(`/api/admin/overview?section=workflows&q=${encodeURIComponent(query)}`);
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
$('#workflow-tag-filter').addEventListener('change', renderWorkflows);
$('#workflow-folder-filter').addEventListener('change', renderWorkflows);

const sectionTargets = {
  workflows: ['.view[data-page="workflows"]', '[aria-labelledby="home-workflows-title"]'],
  activity: ['[aria-labelledby="home-results-title"]', '.view[data-page="runs"]'],
  usage: ['[aria-labelledby="runs-usage-title"]'],
  connections: ['.view[data-page="connections"]'],
  credentials: ['.view[data-page="credentials"]'],
  tokens: ['.view[data-page="tokens"]'],
  emails: ['.view[data-page="emails"]'],
};

function sectionStatus(section, message = '') {
  for (const selector of sectionTargets[section]) {
    const target = $(selector);
    let status = target.querySelector(':scope > .section-loading');
    if (!status?.classList.contains('section-loading')) {
      status = document.createElement('p');
      status.className = 'section-loading sub';
      status.setAttribute('role', 'status');
      target.prepend(status);
    }
    status.textContent = message;
    status.hidden = !message;
    target.dataset.pending = String(Boolean(message) && !state.loadedSections.has(section));
    target.setAttribute('aria-busy', String(message.startsWith('Loading')));
  }
}

const loadOverview = createOverviewLoader(api, (section, data) => {
  Object.assign(state.data, data);
  state.loadedSections.add(section);
  sectionStatus(section);
  render(section);
  if (section === 'connections' && state.view === 'designer') postConnectionsToDesigner();
}, (section, error) => {
  sectionStatus(section, `Could not load ${section}: ${error.message}. Use Refresh to retry.`);
});

let refreshSequence = 0;
export async function refresh() {
  const sequence = ++refreshSequence;
  state.data ||= {};
  for (const section of Object.keys(sectionTargets)) sectionStatus(section, `Loading ${section}…`);
  $('#loading').hidden = false;
  if ($('#forbidden-view').hidden) showApp();
  renderAgentTasks();
  renderWorkers();
  renderInbox();
  renderSchedules();
  renderTriggers();
  // Error history is independent of both startup and the other reads.
  api('/api/admin/errors/summary?days=7').then((data) => {
    if (sequence !== refreshSequence) return;
    state.errors = data;
    renderErrors();
  }).catch(() => {
    if (sequence !== refreshSequence) return;
    state.errors = null;
    renderErrors();
  });
  const success = await loadOverview();
  if (sequence === refreshSequence) $('#loading').hidden = true;
  return success;
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
    <td class="action-cell" data-label="Manage">${version.current ? '' : `<button type="button" class="dk-button dk-button--secondary version-restore" data-revision="${version.revision}">Restore</button>`}</td>
  </tr>`).join('');
  // The rollback half of the list: pick any two revisions and see what
  // changed before restoring one (`dapier workflows diff` prints the same).
  const ascending = [...(data.versions || [])].map((version) => version.revision).reverse();
  const revisionOptions = (selected) => ascending.map((rev) =>
    `<option value="${rev}"${rev === selected ? ' selected' : ''}>v${rev}</option>`).join('');
  const diffUI = ascending.length >= 2 ? `
    <div class="versions-diff-row">
      <label class="sub">Diff
        <select class="mono version-diff-from">${revisionOptions(ascending[ascending.length - 2])}</select>
        →
        <select class="mono version-diff-to">${revisionOptions(ascending[ascending.length - 1])}</select>
      </label>
      <button type="button" class="dk-button dk-button--secondary version-diff">Diff…</button>
    </div>
    <pre class="mono version-diff-output" hidden></pre>` : '';
  $('#versions-detail').innerHTML = (rows
    ? `<div class="table-wrap"><table><thead><tr><th>Version</th><th>When</th><th>Change</th><th>By</th><th>State</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
    : '<p class="sub">No version history yet. Every save, toggle, and rollback is recorded from now on.</p>') + diffUI;
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

/* The Versions dialog's Diff button: the unified diff between the two
   picked revisions, the same plain text `dapier workflows diff` prints. */
async function diffVersions(button) {
  const dialog = $('#versions-dialog');
  const file = dialog.dataset.file;
  const from = dialog.querySelector('.version-diff-from').value;
  const to = dialog.querySelector('.version-diff-to').value;
  const output = dialog.querySelector('.version-diff-output');
  const feedback = $('#versions-feedback');
  button.disabled = true;
  output.hidden = true;
  try {
    const data = await api(`/api/admin/designer/workflows/${encodeURIComponent(file)}/versions/diff?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`);
    output.innerHTML = escapeHtml(data.same
      ? `v${from} and v${to} are identical.`
      : data.diff || '');
    output.hidden = false;
  } catch (error) {
    feedback.textContent = error.message;
    feedback.hidden = false;
  } finally {
    button.disabled = false;
  }
}

document.addEventListener('click', (event) => {
  const button = event.target.closest('.version-diff');
  if (!button || button.disabled) return;
  void diffVersions(button);
});

export function openRowFor(event) {
  if (event.target.closest('.workflow-toggle, .workflow-resume, .workflow-tags, .workflow-folder, .workflow-delete, .workflow-duplicate')) return; // the button handles itself
  const workflowRow = event.target.closest('.workflow-open');
  if (workflowRow) return openWorkflow(workflowRow.dataset.workflow);
  const runRow = event.target.closest('.run-open');
  if (runRow) return openRun(runRow.dataset.run);
}

/* Send the operator error digest now (POST /api/admin/errors/digest) — the
   same render-and-send the daily schedule runs; the API answers with what
   was sent, or skipped when nothing failed. */
const sendDigestButton = $('#overview-errors-send-digest');
if (sendDigestButton) sendDigestButton.addEventListener('click', async () => {
  sendDigestButton.disabled = true;
  try {
    const data = await api('/api/admin/errors/digest', { method: 'POST', body: '{}' });
    notice(data.skipped
      ? `Nothing failed in the last ${data.window_days || 1} day(s) — no digest sent.`
      : `Digest sent to ${data.to} (${data.total_failed_runs} failed).`);
  } catch (error) {
    notice(error.message, true);
  } finally {
    sendDigestButton.disabled = false;
  }
});

/* ---- Bulk selection (POST /api/admin/designer/workflows/bulk) ---- */

$('#workflow-table').addEventListener('change', (event) => {
  const checkbox = event.target.closest('.workflow-select');
  if (!checkbox) return;
  if (checkbox.checked) selectedWorkflows.add(checkbox.dataset.file);
  else selectedWorkflows.delete(checkbox.dataset.file);
  renderWorkflows();
});

$('#workflow-select-all').addEventListener('change', (event) => {
  // Select-all scopes to the rows the table is showing right now.
  const shown = selectableFiles(currentShownWorkflows());
  if (event.target.checked) shown.forEach((file) => selectedWorkflows.add(file));
  else shown.forEach((file) => selectedWorkflows.delete(file));
  renderWorkflows();
});

/* The exact rows the table is showing right now (search + status + tag +
   folder). */
function currentShownWorkflows() {
  const all = state.data?.workflows || [];
  const query = $('#workflow-search').value.trim().toLowerCase();
  const status = $('#workflow-filter').value;
  const tagFilter = $('#workflow-tag-filter').value;
  const folderFilter = $('#workflow-folder-filter').value;
  const serverIds = query ? state.workflowSearchIds : null;
  return all.filter((workflow) =>
    (serverIds ? serverIds.has(workflow.id)
      : `${workflow.id} ${triggerLabel(workflow)} ${workflowTriggerText(workflow)} ${(workflow.actions || []).map((action) => `${action.type} ${workflowActionText(action)}`).join(' ')}`.toLowerCase().includes(query)) &&
    (status === 'all' || workflow.enabled === (status === 'enabled')) &&
    (tagFilter === 'all' || (workflow.tags || []).some((tag) => String(tag).toLowerCase() === tagFilter)) &&
    (folderFilter === 'all' || String(workflow.folder || '').trim().toLowerCase() === folderFilter.toLowerCase()));
}

async function bulkToggle(action, button) {
  if (selectedWorkflows.size === 0) return;
  const ids = [...selectedWorkflows];
  button.disabled = true;
  try {
    const data = await api('/api/admin/designer/workflows/bulk', {
      method: 'POST',
      body: JSON.stringify({ ids, action }),
    });
    const failed = (data.results || []).filter((result) => !result.ok);
    notice(failed.length
      ? `${data.ok || 0} of ${data.requested || ids.length} ${action}d; ${failed.length} failed: ${failed.map((result) => `${result.id} — ${result.error}`).join('; ')}`
      : `${data.ok ?? ids.length} workflow${ids.length === 1 ? '' : 's'} ${action}d — live now.`);
    selectedWorkflows.clear();
    await refresh();
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
}

$('#workflow-bulk-enable').addEventListener('click', (event) => bulkToggle('enable', event.target.closest('button')));
$('#workflow-bulk-disable').addEventListener('click', (event) => bulkToggle('disable', event.target.closest('button')));

/* Pause/Resume every workflow the current tag filter or search matches —
   the same ids the table shows, so the confirmed count is exact. */
async function bulkToggleShown(action, button) {
  const ids = selectableFiles(currentShownWorkflows());
  if (!ids.length) return;
  const verb = action === 'disable' ? 'Pause' : 'Resume';
  const done = action === 'disable' ? 'paused' : 'resumed';
  if (!window.confirm(`${verb} ${ids.length} workflow${ids.length === 1 ? '' : 's'} matching ${button.dataset.scope || 'the current filter'}? They ${action === 'disable' ? 'stop on their next trigger until resumed' : 'run on their next trigger'}.`)) return;
  button.disabled = true;
  try {
    const data = await api('/api/admin/designer/workflows/bulk', {
      method: 'POST',
      body: JSON.stringify({ ids, action }),
    });
    const failed = (data.results || []).filter((result) => !result.ok);
    notice(failed.length
      ? `${data.ok || 0} of ${data.requested || ids.length} ${done}; failures: ${failed.map((result) => `${result.id} — ${result.error}`).join('; ')}`
      : `${data.ok ?? ids.length} workflow${ids.length === 1 ? '' : 's'} ${done} — live now.`);
    await refresh();
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
}

$('#workflow-scope-pause').addEventListener('click', (event) => bulkToggleShown('disable', event.target.closest('button')));
$('#workflow-scope-resume').addEventListener('click', (event) => bulkToggleShown('enable', event.target.closest('button')));

/* ---- Delete (DELETE /api/admin/designer/workflows/<file>) ----
   Permanent: the live item, its version history, and the repo YAML all go. */
document.addEventListener('click', async (event) => {
  const button = event.target.closest('.workflow-delete');
  if (!button || button.disabled || !button.dataset.file) return;
  /* Disable-first messaging: an On workflow stops the moment it is deleted,
     and the confirm says so before anything else. */
  const workflow = (state.data.workflows || []).find((item) => item.id === button.dataset.workflow);
  const dialog = $('#workflow-delete-confirm-dialog');
  $('#workflow-delete-confirm-message').textContent =
    `Delete ${button.dataset.workflow} permanently? `
    + (workflow && workflow.enabled ? 'The workflow is On — deleting stops it immediately. ' : '')
    + `Its version history is removed and workflows/${button.dataset.file} is deleted from the repository. Past runs stay in history. This cannot be undone.`;
  dialog.returnValue = '';
  dialog.showModal();
  const confirmed = await new Promise(
    (resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true }));
  if (!confirmed) return;
  button.disabled = true;
  try {
    await api(`/api/admin/designer/workflows/${encodeURIComponent(button.dataset.file)}`, {
      method: 'DELETE',
    });
    notice(`Deleted ${button.dataset.workflow}. It is off now and no deploy will bring it back.`);
    await refresh();
  } catch (error) {
    notice(error.message, true);
    button.disabled = false;
  }
});

/* ---- Tags editor (PUT /api/admin/designer/workflows/<file>/tags) ---- */
$('#workflow-table').addEventListener('click', (event) => {
  const button = event.target.closest('.workflow-tags');
  if (!button || button.disabled || !button.dataset.file) return;
  const dialog = $('#workflow-tags-dialog');
  $('#workflow-tags-title').textContent = `Tags — ${button.dataset.workflow || button.dataset.file.replace(/\.yaml$/, '')}`;
  $('#workflow-tags-blurb').textContent = `Organize ${button.dataset.file} with labels you can filter the list by.`;
  $('#workflow-tags-input').value = button.dataset.tags || '';
  const error = $('#workflow-tags-error');
  error.hidden = true;
  dialog.dataset.file = button.dataset.file;
  dialog.showModal();
});

$('#workflow-tags-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const dialog = $('#workflow-tags-dialog');
  const file = dialog.dataset.file;
  const save = $('#workflow-tags-save');
  const error = $('#workflow-tags-error');
  const tags = $('#workflow-tags-input').value
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean);
  save.disabled = true;
  try {
    await api(`/api/admin/designer/workflows/${encodeURIComponent(file)}/tags`, {
      method: 'PUT',
      body: JSON.stringify({ tags }),
    });
    dialog.close();
    notice(tags.length ? `Tags saved for ${file}.` : `Tags removed from ${file}.`);
    await refresh();
  } catch (caught) {
    error.textContent = caught.message;
    error.hidden = false;
  } finally {
    save.disabled = false;
  }
});

/* ---- Folder editor (PUT /api/admin/designer/workflows/<file>/folder) ----
   Flat Zapier-style folders: one per workflow; an empty value clears it. */
$('#workflow-table').addEventListener('click', (event) => {
  const button = event.target.closest('.workflow-folder');
  if (!button || button.disabled || !button.dataset.file) return;
  const dialog = $('#workflow-folder-dialog');
  $('#workflow-folder-title').textContent = `Folder — ${button.dataset.workflow || button.dataset.file.replace(/\.yaml$/, '')}`;
  $('#workflow-folder-blurb').textContent = `File ${button.dataset.file} under a folder you can filter the list by.`;
  $('#workflow-folder-input').value = button.dataset.folder || '';
  const error = $('#workflow-folder-error');
  error.hidden = true;
  dialog.dataset.file = button.dataset.file;
  dialog.showModal();
});

$('#workflow-folder-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const dialog = $('#workflow-folder-dialog');
  const file = dialog.dataset.file;
  const save = $('#workflow-folder-save');
  const error = $('#workflow-folder-error');
  const folder = $('#workflow-folder-input').value.trim();
  save.disabled = true;
  try {
    await api(`/api/admin/designer/workflows/${encodeURIComponent(file)}/folder`, {
      method: 'PUT',
      body: JSON.stringify({ folder }),
    });
    dialog.close();
    notice(folder ? `${file} moved to folder “${folder}”.` : `Folder removed from ${file}.`);
    await refresh();
  } catch (caught) {
    error.textContent = caught.message;
    error.hidden = false;
  } finally {
    save.disabled = false;
  }
});

/* ---- Duplicate (POST /api/admin/designer/workflows/<file>/duplicate) ----
   The same fork the CLI's `workflows duplicate` runs: a fresh id derived
   from the name (<id>-copy by default), run state stripped. The copy is
   saved live through the standard save path; the list refreshes to show it. */
$('#workflow-table').addEventListener('click', async (event) => {
  const button = event.target.closest('.workflow-duplicate');
  if (!button || button.disabled || !button.dataset.file) return;
  button.disabled = true;
  try {
    const data = await api(`/api/admin/designer/workflows/${encodeURIComponent(button.dataset.file)}/duplicate`, {
      method: 'POST',
      body: '{}',
    });
    const newId = String(data.file || '').replace(/\.yaml$/, '') || button.dataset.workflow;
    notice(`Duplicated ${button.dataset.workflow} as ${newId}${data.published ? ' — live now' : ''}.`);
    await refresh();
  } catch (error) {
    notice(error.message, true);
    button.disabled = false;
  }
});

/* Export all: the server-built zip of every workflow's canonical YAML — the
   same bundle `dapier workflows export --all` writes. The zip arrives base64
   in the JSON body with an attachment content-disposition naming it (fetch
   the JSON, build a Blob, download it under the server-suggested name). */
const exportAllButton = $('#export-all-yaml');
if (exportAllButton) exportAllButton.addEventListener('click', async () => {
  exportAllButton.disabled = true;
  try {
    const data = await api('/api/admin/designer/export');
    const raw = atob(data.b64 || '');
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
    const blob = new Blob([bytes], { type: 'application/zip' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = data.filename || 'dapier-workflows.zip';
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    const skipped = data.skipped || [];
    notice(skipped.length
      ? `Exported ${data.count} workflow(s); skipped (no source file): ${skipped.join(', ')}.`
      : `Exported ${data.count} workflow(s).`);
  } catch (error) {
    notice(error.message, true);
  } finally {
    exportAllButton.disabled = false;
  }
});

/* Resume: the auto-pause's undo. PUTs the workflow resource with
   enabled:true — the exact call the console's Turn on and `dapier workflows
   on` make (designer_store.api_toggle on enable clears the auto_paused flag
   and zeroes the failure streak), so there is no second route to keep in
   sync. */
document.addEventListener('click', async (event) => {
  const button = event.target.closest('.workflow-resume');
  if (!button || button.disabled) return;
  button.disabled = true;
  try {
    await api(`/api/admin/designer/workflows/${encodeURIComponent(button.dataset.file)}`, {
      method: 'PUT',
      body: JSON.stringify({ enabled: true }),
    });
    await refresh();
    notice('Workflow resumed — the auto-pause is cleared.');
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
});


/* Keep row actions in the viewport, including the last row and narrow screens.
   The native popover handles Escape, outside clicks, and one open menu at a time. */
document.addEventListener('toggle', (event) => {
  const menu = event.target;
  if (!menu.matches?.('.workflow-menu') || event.newState !== 'open') return;
  const trigger = document.querySelector(`[popovertarget="${menu.id}"]`);
  const anchor = trigger.getBoundingClientRect();
  menu.style.maxHeight = `${window.innerHeight - 24}px`;
  const bounds = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(12, Math.min(anchor.right - bounds.width, window.innerWidth - bounds.width - 12))}px`;
  const below = anchor.bottom + 6;
  menu.style.top = `${Math.max(12, below + bounds.height <= window.innerHeight - 12 ? below : anchor.top - bounds.height - 6)}px`;
}, true);

// Dismiss after choosing an enabled action; existing delegated handlers run
// unchanged even when the popover closes.
$('#workflow-table').addEventListener('click', (event) => {
  const action = event.target.closest('.workflow-menu button, .workflow-menu a');
  if (action && !action.disabled) action.closest('.workflow-menu').hidePopover();
});
