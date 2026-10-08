/* A run inbox: scan activity, select a task, read its outcome. */
import { $, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp } from '../format.js';
import { renderMarkdown } from '../md.js';
import { state } from '../state.js';
import { workflowName } from '../workflow-names.js';

const ACTIVE = new Set(['queued', 'running', 'claimed', 'starting', 'started']);
const FAILED = new Set(['failed', 'timed_out', 'interrupted']);
/* The shared status vocabulary (format.js), plus the worker's own phases. */
const LABELS = { claimed: 'Starting', starting: 'Starting', started: 'Running' };
let tasks = [], selected = null, detail = null, filter = 'all', search = '', fetched = false;
let listSeq = 0, detailSeq = 0;

function title(task) {
  if (!task.email_subject?.trim() && task.workflow && workflowName(task.workflow) !== task.workflow) {
    return workflowName(task.workflow);
  }
  return task.email_subject?.trim() || (task.workflow || 'Agent run').replace(/^email-trigger-/, '').replace(/[-_]/g, ' ').replace(/^./, c => c.toUpperCase());
}
function badge(task) { return statusLine(task.status, LABELS); }
function date(task) { return formatTimestamp(task.created_at) || '—'; }
function duration(task) {
  if (!task.started_at) return 'Waiting to start';
  const seconds = Math.max(0, Math.round((Number(task.finished_at) || Date.now() / 1000) - Number(task.started_at)));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}
function shown() {
  return tasks.filter(task => (filter === 'all' || filter === 'active' && ACTIVE.has(task.status) || filter === 'attention' && FAILED.has(task.status) || filter === 'completed' && task.status === 'succeeded') &&
    (!search || [title(task), task.workflow, task.workflow ? workflowName(task.workflow) : '', task.engine, task.summary, task.task_id].join(' ').toLowerCase().includes(search)));
}
function renderList() {
  const rows = shown();
  const counts = { all: tasks.length, active: tasks.filter(t => ACTIVE.has(t.status)).length, attention: tasks.filter(t => FAILED.has(t.status)).length, completed: tasks.filter(t => t.status === 'succeeded').length };
  document.querySelectorAll('[data-agent-filter]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.agentFilter === filter));
    button.querySelector('.dk-filter-chip__count').textContent = counts[button.dataset.agentFilter];
  });
  $('#agent-runs-note').textContent = fetched ? `${rows.length} shown${tasks.length === 200 ? ' · newest 200 loaded' : ''}` : 'Loading runs…';
  $('#agent-runs-list').innerHTML = rows.length ? rows.map(task => `<button type="button" class="agent-run-item ${task.task_id === selected ? 'selected' : ''}" data-agent-task="${escapeHtml(task.task_id).replace(/"/g, '&quot;')}" aria-current="${task.task_id === selected ? 'true' : 'false'}">
    <span class="agent-run-line"><strong>${escapeHtml(title(task))}</strong>${badge(task)}</span>
    <span class="agent-run-meta"><span title="${escapeHtml(task.workflow || '')}">${escapeHtml(task.workflow ? workflowName(task.workflow) : 'Agent run')}</span><time class="mono">${escapeHtml(date(task))}</time></span>
  </button>`).join('') : `<div class="dialog-state"><strong>${tasks.length ? 'No matching runs' : 'No agent runs yet'}</strong><p>${tasks.length ? 'Try another status or search.' : 'Runs appear here when a workflow starts an agent.'}</p></div>`;
}
function setTaskUrl(id) {
  if (state.view !== 'agents') return;
  const url = new URL(window.location.href);
  if (id) url.searchParams.set('task', id); else url.searchParams.delete('task');
  history.replaceState(null, '', url);
}
function resultText(task) {
  try {
    const output = JSON.parse(task.logs?.stdout || '');
    if (typeof output.result === 'string' && output.result.trim()) return output.result;
  } catch (_) { /* Raw output remains available in Logs. */ }
  return task.summary || task.error || '';
}

function renderDetail() {
  const panel = $('#agent-run-detail');
  $('.agent-workspace').classList.toggle('has-selection', Boolean(selected));
  if (!selected) {
    panel.innerHTML = '<div class="dialog-state"><strong>Select a run</strong><p>Read its result and check what happened.</p></div>';
    return;
  }
  const task = detail;
  if (!task) { panel.innerHTML = '<div class="dialog-state" role="status"><p>Loading the run…</p></div>'; return; }
  const active = ACTIVE.has(task.status), failed = FAILED.has(task.status), result = resultText(task);
  const logs = task.logs;
  panel.innerHTML = `<div class="section-head agent-detail-band">
      <button class="agent-back dk-button dk-button--secondary dk-button--sm" type="button">Back to runs</button>
      <div class="agent-detail-status">${badge(task)}<span>${escapeHtml(task.engine || 'Agent')} · <span class="mono">${escapeHtml(duration(task))}</span></span></div>
    </div>
    <div class="agent-detail-body">
    <div class="agent-detail-head">
      <h2 tabindex="-1">${escapeHtml(title(task))}</h2>
      <dl class="run-summary">
        <div><dt>Workflow</dt><dd><a class="text-link" href="/workflows/${encodeURIComponent(task.workflow || '')}" title="${escapeHtml(task.workflow || '')}">${escapeHtml(task.workflow ? workflowName(task.workflow) : 'Workflow')}</a></dd></div>
        <div><dt>${task.started_at ? 'Started' : task.status === 'queued' ? 'Queued' : 'Created'}</dt><dd class="mono">${escapeHtml(formatTimestamp(task.started_at || task.created_at) || '—')}</dd></div>
        ${task.finished_at ? `<div><dt>Finished</dt><dd class="mono">${escapeHtml(formatTimestamp(task.finished_at))}</dd></div>` : ''}
      </dl>
    </div>
    <section class="agent-result" aria-labelledby="agent-result-heading">
      <h3 id="agent-result-heading">${failed ? 'What went wrong' : active ? 'Progress' : 'Result'}</h3>
      ${result ? `<div class="agent-result-text">${renderMarkdown(result)}</div>` : `<div class="agent-progress"><p>${task.status === 'queued' ? 'Waiting for a worker.' : active ? 'The agent is working on this task.' : 'This run finished without a recorded result.'}</p>${active ? '<p class="sub">This screen checks for updates every 15 seconds. Output is available when the run finishes.</p>' : ''}</div>`}
    </section>
    <details class="agent-log-section"><summary>Logs ${logs?.stderr ? '<span class="agent-log-hint">Error output available</span>' : ''}</summary>
      ${logs ? `${logs.truncated ? '<p class="sub">Only the end of the output was retained.</p>' : ''}
        <div class="dk-cluster agent-log-tabs" role="group" aria-label="Log stream"><button type="button" class="dk-filter-chip" data-agent-stream="stdout" aria-pressed="true">Output</button><button type="button" class="dk-filter-chip" data-agent-stream="stderr" aria-pressed="false">Errors</button></div>
        <pre class="agent-log-output" tabindex="0">${escapeHtml(logs.stdout || 'No output recorded.')}</pre>`
        : `<p class="sub">${active ? 'Logs will be available after this run finishes.' : 'Detailed logs were not recorded for this run. Its available result is shown above.'}</p>`}
    </details>
    <details class="agent-technical"><summary>Run details</summary><dl>
      <dt>Task ID</dt><dd class="mono">${escapeHtml(task.task_id)}</dd>
      <dt>Required capabilities</dt><dd>${escapeHtml((task.requires || []).join(", ") || "None")}</dd>
      <dt>Workspace</dt><dd>${escapeHtml(task.workspace || 'Worker default')}</dd>
      ${task.exit_code != null ? `<dt>Exit code</dt><dd>${escapeHtml(String(task.exit_code))}</dd>` : ''}
      ${task.notified_at ? `<dt>Completion email sent</dt><dd>${escapeHtml(formatTimestamp(task.notified_at))}</dd>` : ''}
    </dl></details>
    </div>`;
}
async function selectTask(id, { updateUrl = true } = {}) {
  /* A newly picked run opens at its head; the 15s quiet refresh of the same
     run must not yank a reader back to the top of a long result. */
  const changed = selected !== id;
  selected = id; detail = null;
  if (changed) $('#agent-run-detail').scrollTop = 0;
  const seq = ++detailSeq;
  renderList(); renderDetail();
  if (updateUrl) setTaskUrl(id);
  try {
    const data = await api(`/api/admin/agent-tasks?task_id=${encodeURIComponent(id)}`);
    if (seq !== detailSeq || selected !== id) return;
    detail = data.task; renderDetail();
  } catch (error) {
    if (seq !== detailSeq) return;
    $('#agent-run-detail').innerHTML = `<div class="dialog-state"><strong>This run could not be loaded</strong><p>The task service answered: ${escapeHtml(error.message)}</p><div class="dk-cluster"><button class="agent-back dk-button dk-button--secondary" type="button">Back to runs</button><button class="dk-button dk-button--secondary" id="agent-detail-retry" type="button">Try again</button></div></div>`;
  }
}
/* Tasks run on a worker — a machine running `dapier worker`. When work is
   waiting and none has checked in recently, say so: nothing will move until
   one starts. */
async function renderWorkerNote(tasks, seq) {
  const strip = $('#agent-workers-strip');
  if (!strip) return;
  try {
    const data = await api('/api/admin/workers');
    if (seq !== listSeq) return;
    const workers = data.workers || [];
    const active = workers.filter((worker) => worker.active).length;
    const queued = tasks.some((task) => ACTIVE.has(task.status));
    strip.hidden = false;
    if (!workers.length) {
      strip.innerHTML = 'No worker has checked in; tasks stay queued until one runs <code>dapier worker</code>.';
      return;
    }
    const wait = queued && active === 0 ? ' Queued tasks wait for one.' : '';
    strip.innerHTML = `${active ? `<span class="status ok"><span class="status-dot" aria-hidden="true"></span>${active} worker${active === 1 ? '' : 's'} active</span>` : '<span class="status warn"><span class="status-dot" aria-hidden="true"></span>No worker active</span>'}${wait}`;
  } catch (error) {
    if (seq === listSeq) strip.hidden = true;
  }
}

export async function renderAgentTasks({ quiet = false } = {}) {
  const seq = ++listSeq;
  try {
    const data = await api('/api/admin/agent-tasks?limit=200');
    if (seq !== listSeq) return;
    tasks = data.tasks || []; fetched = true; renderList();
    renderWorkerNote(tasks, seq);
    if (state.view !== 'agents') return;
    const fromUrl = state.view === 'agents' ? new URLSearchParams(window.location.search).get('task') : null;
    const id = fromUrl || selected || (window.matchMedia('(min-width: 861px)').matches ? (tasks.find(t => ACTIVE.has(t.status)) || tasks[0])?.task_id : null);
    if (id && (!quiet || id !== selected || !detail || ACTIVE.has(detail.status))) await selectTask(id, { updateUrl: state.view === 'agents' });
    else renderDetail();
  } catch (error) {
    if (seq !== listSeq) return;
    if (!fetched) $('#agent-runs-list').innerHTML = '<div class="dialog-state"><strong>Agent runs could not be loaded</strong><p>The task service did not answer. Use Refresh to try again.</p></div>';
    if (!quiet) notice(error.message, true);
  }
}
$('#agent-runs-search').addEventListener('input', event => { search = event.target.value.trim().toLowerCase(); renderList(); });
document.addEventListener('click', event => {
  const filterButton = event.target.closest('[data-agent-filter]');
  if (filterButton) { filter = filterButton.dataset.agentFilter; renderList(); }
  const row = event.target.closest('[data-agent-task]');
  if (row) selectTask(row.dataset.agentTask).then(() => {
    if (selected === row.dataset.agentTask && window.matchMedia('(max-width: 860px)').matches) $('#agent-run-detail h2')?.focus();
  });
  if (event.target.closest('.agent-back')) { ++detailSeq; selected = null; detail = null; setTaskUrl(null); renderList(); renderDetail(); $('#agent-runs-search').focus(); }
  if (event.target.closest('#agent-detail-retry') && selected) selectTask(selected);
  const stream = event.target.closest('[data-agent-stream]');
  if (stream && detail?.logs) {
    document.querySelectorAll('[data-agent-stream]').forEach(button => button.setAttribute('aria-pressed', String(button === stream)));
    $('.agent-log-output').textContent = detail.logs[stream.dataset.agentStream] || (stream.dataset.agentStream === 'stderr' ? 'No errors recorded.' : 'No output recorded.');
  }
});
setInterval(() => {
  if (state.view === 'agents' && !document.hidden && (tasks.some(t => ACTIVE.has(t.status)) || ACTIVE.has(detail?.status))) renderAgentTasks({ quiet: true });
}, 15000);
