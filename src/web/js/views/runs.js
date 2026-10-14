/* Runs view: one row per run (a workflow's handling of one trigger event),
   and the Zapier-style flow dialog: trigger → each action with its data. */
import { state } from '../state.js';
import { $, icons, notice, openWhenReady } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, runStatusLine, resolvedLine, formatTimestamp, formatDuration, dataBlock, triggerEventLabel, sentenceCase } from '../format.js';
import { workflowName, workflowLabelHtml } from '../workflow-names.js';

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
  js: 'code-2',
  condition: 'git-branch',
  paths: 'git-branch',
  decision: 'sparkles',
  filter: 'filter',
  union: 'git-merge',
  for_each: 'repeat',
  delay: 'clock',
  digest: 'inbox',
  digest_add: 'inbox',
  digest_flush: 'inbox',
  ai_complete: 'sparkles',
  agent: 'terminal',
};

const BRANCHING_STEP_TYPES = new Set(['condition', 'paths', 'decision']);

export function branchVerdict(step) {
  if (!step || typeof step !== 'object') return null;
  const type = step.action_type || step.type || '';
  const out = step.output && typeof step.output === 'object' ? step.output : null;
  if (!out) return null;
  if (type === 'condition') {
    const branch = out.branch || (out.condition === 'passed' ? 'then' : 'else');
    return {
      kind: 'condition',
      branch,
      badge: 'Branch → ' + branch,
      passed: out.condition !== undefined ? out.condition === 'passed' : branch === 'then',
    };
  }
  if (type === 'paths') {
    const branch = out.matched || out.branch || (out.ran > 0 ? 'else' : null);
    return {
      kind: 'paths',
      branch,
      badge: branch ? ('Path → ' + branch) : 'No path matched',
    };
  }
  if (type === 'decision') {
    const branch = out.branch || (out.choice != null && out.choice !== '' ? String(out.choice) : null);
    const conf = out.confidence != null && Number.isFinite(Number(out.confidence))
      ? Math.round(Number(out.confidence) * 100) + '%'
      : '';
    return {
      kind: 'decision',
      branch,
      choice: out.choice,
      confidence: out.confidence,
      badge: 'Decision → ' + (branch || 'none') + (conf ? ' · ' + conf : ''),
    };
  }
  if (type === 'union') {
    const branch = out.branch || out.from || null;
    return {
      kind: 'union',
      branch,
      badge: 'Union ← ' + (branch || 'branches'),
    };
  }
  if (type === 'filter') {
    const passed = out.filter === 'passed';
    return {
      kind: 'filter',
      branch: passed ? 'pass' : 'stop',
      badge: passed ? 'Filter → passed' : 'Filter → stopped',
    };
  }
  return null;
}

function triggerLabel(run) {
  return triggerEventLabel(run.connector, run.event_type);
}

function machineTrigger(run) {
  return `${run.connector || '?'} · ${run.event_type || '?'}`;
}

/* A failure's step, or the "fixed" pill once it stopped needing action —
   one of the two, never both: the step is the diagnosis, the pill is the
   verdict, and a retired failure reads as the latter. */
function statusCell(run) {
  if (run.resolved) return resolvedLine(run);
  const failed = run.failed_step
    ? `<span class="cell-note" title="${escapeHtml(run.failed_step)}">at <span class="mono">${escapeHtml(run.failed_step)}</span></span>` : '';
  const delayedUntil = run.status === 'delayed' && run.delayed_until
    ? `<span class="cell-note">until <span class="mono">${escapeHtml(formatTimestamp(run.delayed_until))}</span></span>` : '';
  const stalledSince = run.stalled && run.stalled_since
    ? `<span class="cell-note">since <span class="mono">${escapeHtml(formatTimestamp(run.stalled_since))}</span></span>` : '';
  return `${runStatusLine(run)}${failed}${delayedUntil}${stalledSince}`;
}

function runRow(run) {
  const name = run.workflow_id ? workflowName(run.workflow_id) : 'Run';
  const started = formatTimestamp(run.started_at) || '—';
  return `<tr class="run-open" data-run="${escapeHtml(run.run_id)}" role="button" tabindex="0">
    <td class="cell-title run-name"><span class="cell-name workflow-label clip" title="${escapeHtml(run.workflow_id || name)}">${workflowLabelHtml(run.workflow_id)}</span></td>
    <td class="run-trigger" data-label="Trigger"${run.connector ? '' : ' data-empty'}><span class="clip" title="${escapeHtml(machineTrigger(run))}">${escapeHtml(triggerLabel(run))}</span>${run.event_summary
      ? `<span class="cell-note clip run-event-summary" title="${escapeHtml(run.event_summary)}">${escapeHtml(run.event_summary)}</span>` : ''}</td>
    <td class="run-status" data-label="Status">${statusCell(run)}</td>
    <td class="num mono run-steps" data-label="Steps">${run.steps ?? '—'}</td>
    <td class="mono muted-cell nowrap run-when" data-label="Started">${escapeHtml(started)}</td>
  </tr>`;
}

/* Server-paged run history: the workflow/status/date filters all go through
   /api/admin/runs (the API owns the matching — the date select becomes the
   API's `since` timestamp), and Load more appends the next page through the
   API's paging token. Until a server fetch happens, the view shows the
   overview's recent sample (state.data.runs), filtered locally as before. */
const runsPage = { runs: null, nextToken: null, bounded: false, workflow: '', status: '', date: '', search: '' };
let fetchSeq = 0;

function selectedFilters() {
  return {
    workflow: $('#runs-workflow-filter').value || '',
    // "Failures" and "Fixed" are the API's `problems` and `resolved` views
    // of the same failed runs, so the select passes its value straight
    // through — the API owns which failures still need action.
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
    runsPage.bounded = Boolean((data.paging || {}).bounded);
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
  ].filter(Boolean))].sort((a, b) => workflowName(a).localeCompare(workflowName(b)));
  workflowFilter.innerHTML = '<option value="">All workflows</option>' +
    workflowIds.map((id) => `<option value="${escapeHtml(id)}">${escapeHtml(workflowName(id))}</option>`).join('');
  const statuses = [...new Set(runs.map((run) => run.status).filter(Boolean))];
  if (selectedStatus && selectedStatus !== 'problems' && !statuses.includes(selectedStatus)) {
    statuses.push(selectedStatus); // keep the choice visible across paged fetches
  }
  const hasFailures = runs.some((run) => ['failed', 'error'].includes(run.status));
  const hasResolved = runs.some((run) => run.resolved);
  statusFilter.innerHTML = '<option value="">All statuses</option>' +
    ((selectedStatus === 'problems' || selectedStatus === 'resolved' || hasFailures)
      ? '<option value="problems">Failures</option>' : '') +
    ((selectedStatus === 'resolved' || hasResolved)
      ? '<option value="resolved">Fixed</option>' : '') +
    statuses.sort().map((status) => `<option value="${escapeHtml(status)}">${escapeHtml(sentenceCase(status === 'completed' ? 'succeeded' : status))}</option>`).join('');
  workflowFilter.value = selectedWorkflow;
  statusFilter.value = selectedStatus;
  let shown = runs.filter((run) =>
    (!workflowFilter.value || run.workflow_id === workflowFilter.value) &&
    (!statusFilter.value || run.status === statusFilter.value ||
      (statusFilter.value === 'problems' && ['failed', 'error'].includes(run.status) && !run.resolved) ||
      (statusFilter.value === 'resolved' && run.resolved)));
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
  const clippedNote = serverPaged && runsPage.bounded
    ? ' (the search window was clipped, so older rows may not have been reached)'
    : '';
  const emptyText = (workflowFilter.value ? 'No recent runs for this workflow'
    : runs.length || statusFilter.value ? 'No runs match these filters' : 'No runs yet')
    + clippedNote;
  $('#run-table').innerHTML = shown.map(runRow).join('') ||
    `<tr class="register-empty"><td colspan="5"><strong>${escapeHtml(emptyText)}</strong></td></tr>`;
  /* The API flags a window its scan budget clipped: with a filter applied,
     the rows shown are the ones that matched inside the window, not proof
     there are no others. Say so — an empty failures list that was only
     empty where we looked is the worst possible thing to imply. */
  const clipped = serverPaged && runsPage.bounded;
  const noun = (count) => `${count} run${count === 1 ? '' : 's'}`;
  $('#runs-sample-note').textContent = serverPaged
    ? `Showing ${noun(shown.length)}${runsPage.nextToken ? ', more to load' : ''}${clipped ? ' (search window clipped)' : ''}`
    : `Showing the latest ${noun(shown.length)}`;
  $('#runs-sample-note').title = clipped
    ? 'Records are kept for 90 days. The search window was clipped, so older rows may exist that these filters did not reach.'
    : 'Records are kept for 90 days.';
  $('#runs-load-more').hidden = !(serverPaged && runsPage.nextToken);
  /* Bulk replay names one workflow (the API's requirement), so the button
     only shows with a workflow picked and failures actually in sight. */
  $('#runs-replay-failed').hidden = !(workflowFilter.value &&
    (selectedStatus === 'problems' ||
      shown.some((run) => ['failed', 'error'].includes(run.status) && !run.resolved)));
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

/* One flow step: a divided row in the flow list (no card inside a card) —
   head line, then the data it carried. A top-level step carries "Replay
   from here" when a run id is in scope. */
export function stepCard(first, second) {
  const opts = (typeof first === 'object' && first !== null) ? first : (second || {});
  const {
    icon,
    title,
    localTitle,
    scopedId,
    actionType,
    titleMono = false,
    badge,
    branchBadge,
    status,
    duration,
    at,
    data,
    error,
    replayFrom,
    stepId,
    branchLaneHtml = '',
    isBranching = false,
  } = opts;
  const displayTitle = localTitle || title || '';
  const subParts = scopedId
    ? [scopedId, actionType || (badge ? String(badge).toLowerCase() : ''), duration].filter(Boolean)
    : [];
  const subLine = subParts.length
    ? `<span class="flow-sub mono">${escapeHtml(subParts.join(' · '))}</span>`
    : '';
  const sections = Object.entries(data || {})
    .filter(([, value]) => value !== undefined && value !== null && value !== '')
    .map(([label, value]) => `<div class="flow-data-item"><h4>${escapeHtml(label)}</h4>${dataBlock(value, { openRaw: Boolean(error) }) || '<p class="detail-muted">Empty</p>'}</div>`)
    .join('');
  const replayFromHere = replayFrom && stepId
    ? `<button class="dk-button dk-button--secondary dk-button--sm run-replay-from-step" type="button" data-run="${escapeHtml(replayFrom)}" data-step="${escapeHtml(stepId)}"
        title="Re-run from this step: earlier steps do not run again, their recorded outputs seed the rerun">Replay from here</button>`
    : '';
  const meta = [
    branchBadge ? `<span class="flow-branch-pill">${escapeHtml(branchBadge)}</span>` : '',
    status ? statusLine(status) : '',
    duration ? `<span class="mono">${escapeHtml(duration)}</span>` : '',
    at ? `<span class="mono">${escapeHtml(at)}</span>` : '',
  ].filter(Boolean).join('');
  const stepClasses = ['flow-step', isBranching ? 'flow-step-branching' : '', error ? 'failed' : '']
    .filter(Boolean)
    .join(' ');
  return `<li class="${stepClasses}">
    <div class="flow-head">
      <span class="flow-icon"><i data-lucide="${icon}"></i></span>
      <span class="flow-name"><span class="flow-title${titleMono ? ' mono' : ''}">${escapeHtml(displayTitle)}</span>${badge ? `<span class="flow-type">${escapeHtml(badge)}</span>` : ''}${subLine}</span>
      <span class="flow-meta">${meta}</span>
      ${replayFromHere}
    </div>
    ${error ? `<div class="detail-error"><i data-lucide="triangle-alert"></i><span>${escapeHtml(error)}</span></div>` : ''}
    ${sections ? `<div class="flow-data">${sections}</div>` : ''}
    ${branchLaneHtml}
  </li>`;
}

function parseChildRelativeId(parentId, childId) {
  const rest = childId.slice(parentId.length);
  if (rest.startsWith('.')) {
    const trimmed = rest.slice(1);
    const parts = trimmed.split('.');
    if (parts.length > 1) {
      return { branchSegment: parts[0], localTitle: parts[parts.length - 1] };
    }
    return { branchSegment: null, localTitle: trimmed };
  }
  if (rest.startsWith('[')) {
    const closeBracket = rest.indexOf(']');
    if (closeBracket !== -1) {
      const bracketPart = rest.slice(0, closeBracket + 1);
      const after = rest.slice(closeBracket + 1).replace(/^\./, '');
      const parts = after.split('.').filter(Boolean);
      return { branchSegment: bracketPart, localTitle: parts[parts.length - 1] || childId };
    }
  }
  return { branchSegment: null, localTitle: childId };
}

export function groupStepsTree(steps) {
  const nodes = (steps || []).map((step, index) => ({
    step,
    index,
    id: String(step?.action_id || ''),
    children: [],
    parent: null,
    branchSegment: null,
    localTitle: null,
  }));
  for (const node of nodes) {
    if (!node.id) continue;
    let bestParent = null;
    for (const candidate of nodes) {
      if (candidate === node || !candidate.id) continue;
      if (node.id.startsWith(candidate.id + '.') || node.id.startsWith(candidate.id + '[')) {
        if (!bestParent || candidate.id.length > bestParent.id.length) {
          bestParent = candidate;
        }
      }
    }
    if (bestParent) {
      node.parent = bestParent;
      const parsed = parseChildRelativeId(bestParent.id, node.id);
      node.branchSegment = parsed.branchSegment;
      node.localTitle = parsed.localTitle;
      bestParent.children.push(node);
    }
  }
  return nodes.filter((node) => !node.parent);
}

export function flow(data) {
  const run = data.run || {};
  const steps = data.steps || [];
  const firstInput = steps.find((step) => step.input !== undefined && step.input !== null);
  const replayFrom = run.run_id || '';
  const cards = [];
  cards.push(stepCard({
    icon: 'zap',
    title: 'Trigger',
    badge: triggerLabel(run),
    at: formatTimestamp(run.started_at),
    data: firstInput ? { 'Event data': firstInput.input } : null,
  }));

  function renderNode(node) {
    const { step, index } = node;
    const type = String(step.action_type || step.type || '');
    const inferredBranch = node.children.find((child) => child.branchSegment)?.branchSegment || null;
    const verdict = branchVerdict(step) || (
      inferredBranch && BRANCHING_STEP_TYPES.has(type)
        ? branchVerdict({ ...step, output: { branch: inferredBranch, matched: inferredBranch, choice: inferredBranch } })
        : null
    );
    const isBranching = BRANCHING_STEP_TYPES.has(type) || node.children.length > 0;
    const branchName = verdict?.branch || inferredBranch || (type === 'for_each' ? 'loop' : 'else');
    let branchLaneHtml = '';
    if (isBranching) {
      const childrenHtml = node.children.map((child) => renderNode(child)).join('');
      branchLaneHtml = `<div class="flow-branch-lane">
      <div class="flow-branch-header"><span class="flow-branch-tag">Branch taken: <strong>${escapeHtml(branchName)}</strong></span></div>
      ${childrenHtml
        ? `<ol class="flow-branch-steps">${childrenHtml}</ol>`
        : `<div class="flow-branch-empty">No steps in ${escapeHtml(branchName)} branch — continued</div>`}
    </div>`;
    }
    /* The API replays from top-level steps only (a nested id like
       `branch.post` cannot start a chain), and the action targets a failed
       step — offer it exactly there. */
    const replayable = !node.parent && step.status === 'failed' &&
      step.action_id && !String(step.action_id).includes('.');
    const isChild = Boolean(node.parent);
    return stepCard(index, {
      icon: STEP_ICONS[type] || 'arrow-right',
      title: step.action_id || `Step ${index + 1}`,
      localTitle: isChild ? (node.localTitle || step.action_id) : undefined,
      scopedId: isChild ? step.action_id : undefined,
      actionType: type,
      titleMono: Boolean(step.action_id),
      badge: sentenceCase(type.replace(/[._]+/g, ' ')),
      branchBadge: verdict?.badge || '',
      status: step.status,
      duration: formatDuration(step.duration_ms),
      at: formatTimestamp(step.finished_at),
      data: { Output: step.output },
      error: step.error,
      replayFrom: replayable ? replayFrom : '',
      stepId: step.action_id,
      branchLaneHtml,
      isBranching,
    });
  }

  const rootNodes = groupStepsTree(steps);
  rootNodes.forEach((node, idx) => {
    cards.push(renderNode(node));
    const type = String(node.step.action_type || node.step.type || '');
    const hadBranchActivity = BRANCHING_STEP_TYPES.has(type) || node.children.length > 0;
    const nextNode = rootNodes[idx + 1];
    const nextType = nextNode ? String(nextNode.step.action_type || nextNode.step.type || '') : '';
    if (hadBranchActivity && nextNode && nextType !== 'union') {
      cards.push('<li class="flow-union-divider" aria-label="Branches merged"><span>Union · flow continues</span></li>');
    }
  });
  return `<ol class="flow">${cards.join('')}</ol>`;
}

export function openRun(runId, trigger) {
  return openWhenReady(trigger, 'run', (signal) => loadRun(runId, signal), (data) => renderRun(data, runId));
}

function loadRun(runId, signal) {
  return api(`/api/admin/runs/${encodeURIComponent(runId)}`, { signal });
}

/* Re-render the open run dialog in place after an action inside it (Mark
   fixed, Cancel): the dialog stays open, the acting button carries the busy
   state. */
async function refreshRun(runId) {
  renderRun(await loadRun(runId), runId);
}

function renderRun(data, runId) {
  $('#run-replay-result').hidden = true;
  $('#run-replay-result').textContent = '';
  $('#run-replay-result').classList.remove('error');
  const run = data.run || {};
  const id = run.run_id || runId;
  $('#run-title').textContent = run.workflow_id ? workflowName(run.workflow_id) : 'Run';
  /* Cancel drops a parked continuation, so it sits apart at the start of
     the foot; Mark fixed and Replay sit with Close at the end. */
  $('#run-actions-start').innerHTML = run.status === 'delayed'
    ? `<button class="dk-button dk-button--danger run-cancel" type="button" data-run="${escapeHtml(id)}"
        title="Drop the parked continuation: the remaining actions will never fire">Cancel run</button>`
    : '';
  /* Mark fixed is for the failures no rerun can settle: a dead workflow's
     last run, a negative test meant to fail. A failure a completed replay
     already recovered arrives here resolved (the API derives it) and shows
     the verdict instead of the button — nothing left to press. */
  const fixable = ['failed', 'error'].includes(run.status) && !run.resolved;
  $('#run-actions').innerHTML = `${fixable
    ? `<button class="dk-button dk-button--secondary run-mark-fixed" type="button" data-run="${escapeHtml(id)}"
        title="Stop treating this failure as a problem: it keeps its status and error in history, but leaves the failure views">Mark fixed</button>`
    : ''}<button class="dk-button dk-button--secondary run-replay" type="button" data-run="${escapeHtml(id)}"
        title="Re-inject this run's original trigger event">Replay</button>`;
  const summary = [
    ['Status', `${runStatusLine(run)}${run.resolved ? resolvedLine(run) : ''}`],
    ['Run', `<span class="mono clip" title="${escapeHtml(id)}">${escapeHtml(id)}</span>`],
    ['Started', run.started_at ? `<span class="mono">${escapeHtml(formatTimestamp(run.started_at))}</span>` : ''],
    ['Duration', run.duration_ms != null ? `<span class="mono">${escapeHtml(formatDuration(run.duration_ms))}</span>` : ''],
  ].filter(([, value]) => value);
  $('#run-detail').innerHTML = `
    <dl class="run-summary">${summary.map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`).join('')}</dl>
    <section class="run-flow" aria-label="Steps">${flow(data)}</section>`;
  icons();
  return $('#run-dialog');
}

/* Mark fixed retires a failure nothing will re-derive away: it stamps the
   run's steps server-side (the same route `dapier runs resolve` calls), so
   the run keeps its failed status and error in history while dropping out
   of the failure views. A completed rerun does this on its own — the API
   derives that — so this button is only for the failures replay cannot
   settle. Same confirm-then-refresh shape as the cancel handler below. */
document.addEventListener('click', async (event) => {
  const button = event.target.closest('.run-mark-fixed');
  if (!button || !button.dataset.run || button.disabled) return;
  const runId = button.dataset.run;
  if (!confirm(`Mark ${runId} fixed? It stays in run history with its error, but stops showing up as a problem.`)) return;
  button.disabled = true;
  try {
    await api(`/api/admin/runs/${encodeURIComponent(runId)}/resolve`, {
      method: 'POST',
      body: '{}',
    });
    await refreshRun(runId); // re-render with the fixed verdict
    const result = $('#run-replay-result');
    result.classList.remove('error');
    result.textContent = 'Marked fixed: this run keeps its failed status and error in history, '
      + 'but it no longer counts as a problem.';
    result.hidden = false;
    fetchRunsPage(); // the list row reads fixed
  } catch (error) {
    const result = $('#run-replay-result');
    result.textContent = error.message;
    result.classList.add('error');
    result.hidden = false;
  } finally {
    button.disabled = false;
  }
});

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
    await refreshRun(runId); // re-render with the rolled-up status
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
