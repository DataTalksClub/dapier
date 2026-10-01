/* Runs view: one row per run (a workflow's handling of one trigger event),
   and the Zapier-style flow dialog: trigger → each action with its data. */
import { state } from '../state.js';
import { $, icons, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, wrapTokens, formatTimestamp, formatDuration, dataBlock } from '../format.js';

const STEP_ICONS = {
  webhook: 'webhook',
  slack: 'slack',
  telegram_send: 'send',
  email_send: 'mail',
  dataops: 'database',
  dropbox_upload: 'cloud-upload',
  dropbox_delete: 'trash-2',
  render_html_to_pdf: 'file-text',
  code: 'code-2',
};

function triggerLabel(run) {
  return `${run.connector || '?'} · ${run.event_type || '?'}`;
}

function runRow(run) {
  const failed = run.failed_step ? ` <span class="muted-cell mono">(${escapeHtml(run.failed_step)})</span>` : '';
  const delayedUntil = run.status === 'delayed' && run.delayed_until
    ? ` <span class="muted-cell mono">(until ${escapeHtml(formatTimestamp(run.delayed_until))})</span>` : '';
  return `<tr class="run-open" data-run="${escapeHtml(run.run_id)}" role="button" tabindex="0">
    <td class="cell-title mono"><span class="cell-name">${escapeHtml(run.workflow_id || 'Run')}</span></td>
    <td class="mono muted-cell" data-label="Trigger"><div>${escapeHtml(triggerLabel(run))}</div>${run.event_summary
      ? `<div class="run-event-summary">${escapeHtml(run.event_summary)}</div>` : ''}</td>
    <td data-label="Status">${statusLine(run.status)}${failed}${delayedUntil}</td>
    <td class="mono muted-cell" data-label="Steps">${run.steps ?? '—'}</td>
    <td class="mono muted-cell" data-label="Started">${escapeHtml(formatTimestamp(run.started_at) || '—')}</td>
  </tr>`;
}

/* Server-paged run history: the workflow/status/date filters all go through
   /api/admin/runs (the API owns the matching — the date select becomes the
   API's `since` timestamp), and Load more appends the next page through the
   API's paging token. Until a server fetch happens, the view shows the
   overview's recent sample (state.data.runs), filtered locally as before. */
const runsPage = { runs: null, nextToken: null, workflow: '', status: '', date: '', search: '' };
let fetchSeq = 0;

function selectedFilters() {
  return {
    workflow: $('#runs-workflow-filter').value || '',
    status: $('#runs-status-filter').value || '',
    date: $('#runs-date-filter').value || '',
    search: ($('#runs-search-filter').value || '').trim(),
  };
}

/* The date select as the API's `since`: an RFC 3339 timestamp the run list
   filters on (started at or after). */
function sinceFor(date) {
  const spans = { day: 86400000, week: 604800000 };
  return spans[date] ? new Date(Date.now() - spans[date]).toISOString() : '';
}

async function fetchRunsPage({ append = false } = {}) {
  const { workflow, status, date, search } = selectedFilters();
  const since = sinceFor(date);
  const seq = ++fetchSeq;
  runsPage.workflow = workflow;
  runsPage.status = status;
  runsPage.date = date;
  runsPage.search = search;
  const params = new URLSearchParams({ limit: '25' });
  if (workflow) params.set('workflow_id', workflow);
  if (status) params.set('status', status);
  if (since) params.set('since', since);
  if (search) params.set('q', search);
  if (append && runsPage.nextToken) params.set('next', runsPage.nextToken);
  try {
    const data = await api(`/api/admin/runs?${params}`);
    if (seq !== fetchSeq) return; // a newer fetch superseded this one
    const fresh = data.runs || [];
    runsPage.nextToken = (data.paging || {}).next || null;
    runsPage.runs = append && runsPage.runs ? [...runsPage.runs, ...fresh] : fresh;
  } catch (error) {
    if (seq === fetchSeq && !append) { runsPage.runs = []; runsPage.nextToken = null; }
    notice(error.message, true);
  }
  renderRuns();
}

/* Programmatic filter changes (the workflows view's "Runs" buttons) set the
   select values directly; pick the change up here and re-fetch server-side. */
function syncServerFilters() {
  const { workflow, status, date, search } = selectedFilters();
  const stale = runsPage.runs === null
    ? Boolean(workflow || status || date || search)
    : workflow !== runsPage.workflow || status !== runsPage.status ||
      date !== runsPage.date || search !== runsPage.search;
  if (stale) fetchRunsPage();
}

export function renderRuns() {
  const serverPaged = runsPage.runs !== null;
  const runs = runsPage.runs ?? (state.data?.runs || []);
  syncServerFilters();
  const workflowFilter = $('#runs-workflow-filter');
  const statusFilter = $('#runs-status-filter');
  const dateFilter = $('#runs-date-filter');
  const selectedWorkflow = workflowFilter.value;
  const selectedStatus = statusFilter.value;
  const workflowIds = [...new Set([
    ...runs.map((run) => run.workflow_id),
    ...(state.data?.workflows || []).map((workflow) => workflow.id),
    selectedWorkflow,
  ].filter(Boolean))].sort();
  workflowFilter.innerHTML = '<option value="">All workflows</option>' +
    workflowIds.map((id) => `<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`).join('');
  const statuses = [...new Set(runs.map((run) => run.status).filter(Boolean))];
  if (selectedStatus && selectedStatus !== 'problems' && !statuses.includes(selectedStatus)) {
    statuses.push(selectedStatus); // keep the choice visible across paged fetches
  }
  statusFilter.innerHTML = '<option value="">All statuses</option>' +
    ((selectedStatus === 'problems' || runs.some((run) => ['failed', 'error'].includes(run.status))) ? '<option value="problems">Failures</option>' : '') +
    statuses.sort().map((status) => `<option value="${escapeHtml(status)}">${escapeHtml(status)}</option>`).join('');
  workflowFilter.value = selectedWorkflow;
  statusFilter.value = selectedStatus;
  let shown = runs.filter((run) =>
    (!workflowFilter.value || run.workflow_id === workflowFilter.value) &&
    (!statusFilter.value || run.status === statusFilter.value ||
      (statusFilter.value === 'problems' && ['failed', 'error'].includes(run.status))));
  if (!serverPaged) {
    // Overview sample only: server pages already matched the filters, the
    // date window included — this just narrows the sample until the fetch.
    const now = Date.now();
    const maxAge = dateFilter.value === 'day' ? 86400000 : dateFilter.value === 'week' ? 604800000 : null;
    if (maxAge !== null) {
      shown = shown.filter((run) => Number.isFinite(Date.parse(run.started_at)) &&
        now - Date.parse(run.started_at) <= maxAge);
    }
    const search = ($('#runs-search-filter').value || '').trim().toLowerCase();
    if (search) {
      shown = shown.filter((run) => JSON.stringify(run).toLowerCase().includes(search));
    }
  }
  const emptyText = workflowFilter.value ? 'No recent runs for this workflow'
    : runs.length || statusFilter.value ? 'No runs match these filters' : 'No runs yet';
  $('#run-table').innerHTML = shown.map(runRow).join('') ||
    `<tr><td colspan="5" class="muted-cell">${emptyText}</td></tr>`;
  $('#runs-sample-note').textContent = serverPaged
    ? `${shown.length} of ${runs.length}${runsPage.nextToken ? ' · more' : ''}`
    : `${shown.length} of ${runs.length} · sample of 25`;
  $('#runs-sample-note').title = 'Records are kept for 90 days.';
  $('#runs-load-more').hidden = !(serverPaged && runsPage.nextToken);
  /* Bulk replay names one workflow (the API's requirement), so the button
     only shows with a workflow picked and failures actually in sight. */
  $('#runs-replay-failed').hidden = !(workflowFilter.value &&
    (selectedStatus === 'problems' || shown.some((run) => ['failed', 'error'].includes(run.status))));
}

$('#runs-load-more').addEventListener('click', () => fetchRunsPage({ append: true }));
$('#runs-workflow-filter').addEventListener('change', () => fetchRunsPage());
$('#runs-status-filter').addEventListener('change', () => fetchRunsPage());
$('#runs-date-filter').addEventListener('change', () => fetchRunsPage());
/* Content search goes through the API like the other filters; debounced so
   typing does not fire a request per keystroke. */
let runsSearchTimer = null;
$('#runs-search-filter').addEventListener('input', () => {
  clearTimeout(runsSearchTimer);
  runsSearchTimer = setTimeout(fetchRunsPage, 300);
});

/* Export the filtered run history as CSV (the audit view's export button,
   over /api/admin/runs/export): the same server filters the list applies,
   capped well past what the 25-row page shows. */
$('#runs-export').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const { workflow, status, date, search } = selectedFilters();
    const since = sinceFor(date);
    const params = new URLSearchParams({ max_rows: '5000' });
    if (workflow) params.set('workflow_id', workflow);
    if (status) params.set('status', status);
    if (since) params.set('since', since);
    if (search) params.set('q', search);
    const data = await api(`/api/admin/runs/export?${params}`);
    const blob = new Blob([data.csv || ''], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = data.filename || 'dapier-runs.csv';
    link.click();
    URL.revokeObjectURL(url);
    notice(`Exported ${data.count || 0} runs${data.truncated ? ' (capped — narrow the filters for the rest)' : ''}`);
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
});

/* One flow card: head row plus optional data sections. A top-level step
   card carries "Replay from here" when a run id is in scope. */
function stepCard({ icon, title, badge, status, duration, at, data, error, replayFrom, stepId }) {
  const sections = Object.entries(data || {})
    .filter(([, value]) => value !== undefined && value !== null && value !== '')
    .map(([label, value]) => `<div class="flow-data-item"><h4>${escapeHtml(label)}</h4>${dataBlock(value, { openRaw: Boolean(error) }) || '<p class="detail-muted">Empty</p>'}</div>`)
    .join('');
  const replayFromHere = replayFrom && stepId
    ? `<button class="button secondary run-replay-from-step" type="button" data-run="${escapeHtml(replayFrom)}" data-step="${escapeHtml(stepId)}"
        title="Re-run from this step: earlier steps do not run again, their recorded outputs seed the rerun">Replay from here</button>`
    : '';
  return `<div class="flow-step ${error ? 'failed' : ''}">
    <div class="flow-head">
      <span class="flow-icon"><i data-lucide="${icon}"></i></span>
      <span class="flow-title">${escapeHtml(title)}</span>
      ${badge ? `<span class="action-type">${escapeHtml(badge)}</span>` : ''}
      <span class="flow-meta">${[status ? statusLine(status) : '', duration ? escapeHtml(duration) : '', at ? escapeHtml(at) : ''].filter(Boolean).join(' ')}</span>
      ${replayFromHere}
    </div>
    ${error ? `<div class="detail-error"><i data-lucide="alert-triangle"></i><span>${escapeHtml(error)}</span></div>` : ''}
    ${sections ? `<div class="flow-data">${sections}</div>` : ''}
  </div>`;
}

function flow(data) {
  const run = data.run || {};
  const steps = data.steps || [];
  const firstInput = steps.find((step) => step.input !== undefined && step.input !== null);
  const replayFrom = run.run_id || '';
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
    /* The API replays from top-level steps only (a nested id like
       `branch.post` cannot start a chain), and the action targets a failed
       step — offer it exactly there. */
    const replayable = step.status === 'failed' &&
      step.action_id && !String(step.action_id).includes('.');
    /* Input is the run's trigger envelope on every step — the Trigger card
       shows it once; the action card carries what the step added. */
    cards.push(stepCard({
      icon: STEP_ICONS[step.action_type] || 'arrow-right',
      title: step.action_id || `Step ${index + 1}`,
      badge: step.action_type,
      status: step.status,
      duration: formatDuration(step.duration_ms),
      at: formatTimestamp(step.finished_at),
      data: { Output: step.output },
      error: step.error,
      replayFrom: replayable ? replayFrom : '',
      stepId: step.action_id,
    }));
  });
  return `<div class="flow">${cards.join('')}</div>`;
}

export async function openRun(runId) {
  $('#run-title').textContent = 'Run';
  $('#run-replay-result').hidden = true;
  $('#run-replay-result').textContent = '';
  $('#run-replay-result').classList.remove('error');
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
  const cancel = run.status === 'delayed'
    ? `<button class="button secondary run-cancel" type="button" data-run="${escapeHtml(run.run_id || runId)}"
        title="Drop the parked continuation: the remaining actions will never fire">Cancel</button>`
    : '';
  $('#run-detail').innerHTML = `
    <div class="detail-summary">
      ${statusLine(run.status)}
      <code>${wrapTokens(run.run_id || runId)}</code>
      ${run.duration_ms != null ? `<span class="flow-total mono">${escapeHtml(formatDuration(run.duration_ms))} total</span>` : ''}
      ${cancel}
      <button class="button secondary run-replay" type="button" data-run="${escapeHtml(run.run_id || runId)}"
        title="Re-inject this run's original trigger event">Replay</button>
    </div>
    ${flow(data)}`;
  icons();
}

/* Cancel drops a suspended run's parked continuation: the delay steps close
   out cancelled and the worker consumes the envelope when it next surfaces,
   so the remaining actions never fire. Same confirm-then-refresh shape as
   the replay handler in main.js — confirm() keeps this view self-contained. */
document.addEventListener('click', async (event) => {
  const button = event.target.closest('.run-cancel');
  if (!button || !button.dataset.run || button.disabled) return;
  const runId = button.dataset.run;
  if (!confirm(`Cancel suspended run ${runId}? Its remaining actions will never run.`)) return;
  button.disabled = true;
  try {
    await api(`/api/admin/runs/${encodeURIComponent(runId)}/cancel`, {
      method: 'POST',
      body: '{}',
    });
    await openRun(runId); // re-render with the rolled-up status
    const result = $('#run-replay-result');
    result.classList.remove('error');
    result.textContent = 'Run cancelled: the parked continuation is dropped and the remaining actions will not run.';
    result.hidden = false;
    fetchRunsPage(); // the list row reads cancelled again
  } catch (error) {
    const result = $('#run-replay-result');
    result.textContent = error.message;
    result.classList.add('error');
    result.hidden = false;
  } finally {
    button.disabled = false;
  }
});

/* Replay-from-step retries a run at one step (the same route
   `dapier runs replay --from-step` calls): the API rebuilds the worker's
   resume envelope from the recorded outputs of the earlier steps, so the
   trigger and the already-done actions do not fire again. The rerun lands
   in run history as its own run, tied to this one by correlation id. */
document.addEventListener('click', async (event) => {
  const button = event.target.closest('.run-replay-from-step');
  if (!button || !button.dataset.run || button.disabled) return;
  const runId = button.dataset.run;
  const stepId = button.dataset.step;
  if (!confirm(`Replay ${runId} from step '${stepId}'? Earlier steps will not run again.`)) return;
  button.disabled = true;
  try {
    const data = await api(`/api/admin/runs/${encodeURIComponent(runId)}/replay`, {
      method: 'POST',
      body: JSON.stringify({ from_step: stepId }),
    });
    const result = $('#run-replay-result');
    result.classList.remove('error');
    result.textContent = `Replay from step '${stepId}' accepted: the rerun (${data.run_id || 'pending'}) `
      + 'appears in run history once the worker picks it up.';
    result.hidden = false;
  } catch (error) {
    const result = $('#run-replay-result');
    result.textContent = error.message;
    result.classList.add('error');
    result.hidden = false;
  } finally {
    button.disabled = false;
  }
});

/* Bulk replay re-injects the workflow's recent failed runs (up to the API's
   cap) through the same route `dapier runs replay-failed` calls; runs whose
   event data was never recorded are skipped by the API and reported. Same
   confirm-then-refresh shape as the cancel handler above. */
document.addEventListener('click', async (event) => {
  const button = event.target.closest('#runs-replay-failed');
  if (!button || button.hidden || button.disabled) return;
  const workflowId = $('#runs-workflow-filter').value;
  if (!workflowId || !confirm(`Replay the recent failed runs of ${workflowId}?`)) return;
  button.disabled = true;
  try {
    const data = await api('/api/admin/runs/replay-failed', {
      method: 'POST',
      body: JSON.stringify({ workflow_id: workflowId }),
    });
    notice(`Replayed ${data.replayed} failed run(s) of ${workflowId}` +
      (data.skipped ? ` — ${data.skipped} skipped (no recorded event data)` : '') + '.');
    fetchRunsPage(); // the replayed rows re-enter history as running
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
});
