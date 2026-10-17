/* A run inbox: scan activity, select a task, read its outcome. The list and
   the run sit in dakit's list–detail split (docs/list-detail.md): the URL's
   ?task= is the selection (pushState for a pick, replaceState for the
   desktop auto-pick), each pane scrolls on its own, and on phones one pane
   shows at a time with Back returning to the row it came from. */
import { $, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp } from '../format.js';
import { renderMarkdown } from '../md.js';
import { state } from '../state.js';
import { workflowName } from '../workflow-names.js';
import { ago } from './activity.js';
import { withOwner } from '../view-as.js';

const ACTIVE = new Set(['queued', 'running', 'claimed', 'starting', 'started']);
const FAILED = new Set(['failed', 'timed_out', 'interrupted']);
/* The shared status vocabulary (format.js), plus the worker's own phases. */
const LABELS = { claimed: 'Starting', starting: 'Starting', started: 'Running' };
let tasks = [], selected = null, detail = null, filter = 'all', search = '', fetched = false;
let listSeq = 0, detailSeq = 0;
let lastOpened = null, listScrollTop = 0;
const DESKTOP = '(min-width: 821px)';
const split = () => $('.agent-split');

function title(task) {
  if (!task.email_subject?.trim() && task.workflow && workflowName(task.workflow) !== task.workflow) {
    return workflowName(task.workflow);
  }
  return task.email_subject?.trim() || (task.workflow || 'Agent run').replace(/^email-trigger-/, '').replace(/[-_]/g, ' ').replace(/^./, c => c.toUpperCase());
}
function badge(task) { return statusLine(task.status, LABELS); }
/* Emails' reading: "3 h ago" to scan, the exact minute in the title. Task
   times are epoch seconds. */
function when(value) {
  if (!value) return '<span class="muted-cell">—</span>';
  const ms = /^\d+$/.test(String(value)) ? Number(value) * 1000 : value;
  return `<time title="${escapeHtml(formatTimestamp(value) || '')}">${escapeHtml(ago(ms))}</time>`;
}
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
    const on = button.dataset.agentFilter === filter;
    button.setAttribute('aria-checked', String(on));
    button.tabIndex = on ? 0 : -1;
    button.querySelector('.dk-count').textContent = counts[button.dataset.agentFilter];
  });
  /* The filter chips carry the counts; the note only says what is partial. */
  $('#agent-runs-note').textContent = fetched ? (tasks.length === 200 ? 'Newest 200' : '') : 'Loading runs…';
  $('#agent-runs-list').innerHTML = rows.length ? rows.map(task => {
    const id = escapeHtml(task.task_id).replace(/"/g, '&quot;');
    return `<li><a class="dk-split__item agent-run-item" href="/agents?task=${encodeURIComponent(task.task_id)}" data-agent-task="${id}"${task.task_id === selected ? ' aria-current="true"' : ''}>
    <span class="dk-split__name agent-run-line"><span class="agent-run-title" title="${escapeHtml(title(task))}">${escapeHtml(title(task))}</span>${badge(task)}</span>
    <span class="dk-split__meta agent-run-meta"><span title="${escapeHtml(task.workflow || '')}">${escapeHtml(task.workflow ? workflowName(task.workflow) : 'Agent run')}</span><span class="dk-mono">${when(task.created_at)}</span></span>
  </a></li>`;
  }).join('') : `<li class="dk-empty dk-empty--compact"><p class="dk-empty__title">${tasks.length ? 'No matching runs' : 'No agent runs yet'}</p><p class="dk-empty__desc">${tasks.length ? 'Try another status or search.' : 'Runs appear here when a workflow starts an agent.'}</p></li>`;
}
function setTaskUrl(id, { push = false } = {}) {
  if (state.view !== 'agents') return;
  const url = new URL(window.location.href);
  if (id) url.searchParams.set('task', id); else url.searchParams.delete('task');
  if (url.href === window.location.href) return;
  if (push) history.pushState(null, '', url); else history.replaceState(null, '', url);
}
function resultText(task) {
  try {
    const output = JSON.parse(task.logs?.stdout || '');
    if (typeof output.result === 'string' && output.result.trim()) return output.result;
  } catch (_) { /* Raw output remains available in Logs. */ }
  return task.summary || task.error || '';
}

function showDetailPane(open) {
  $('#agent-run-head').hidden = !open;
  $('#agent-run-detail').hidden = !open;
  $('#agent-run-empty').hidden = open;
  globalThis.dakitSplit?.show(split(), open ? 'detail' : 'list', { focus: false, resetScroll: false });
}

function renderDetail() {
  const panel = $('#agent-run-detail');
  showDetailPane(Boolean(selected));
  if (!selected) return;
  const task = detail;
  const listed = tasks.find(t => t.task_id === selected);
  $('#agent-run-title').textContent = task ? title(task) : listed ? title(listed) : 'Agent run';
  if (!task) { panel.innerHTML = '<p class="sub" role="status">Loading the run…</p>'; return; }
  const active = ACTIVE.has(task.status), failed = FAILED.has(task.status), result = resultText(task);
  const logs = task.logs;
  const startedLabel = task.started_at ? 'Started' : task.status === 'queued' ? 'Queued' : 'Created';
  /* One compact head, like an Emails message: the title, then ONE meta line
     (status · workflow · when · how long). Exact times, engine and ids sit
     in a Gmail-style Details popover on the workflow name. */
  panel.innerHTML = `<div class="agent-detail-body">
    <div class="agent-detail-head">
      <p class="agent-detail-meta">${badge(task)}<span class="agent-meta-item"><a class="text-link" href="/workflows/${encodeURIComponent(task.workflow || '')}" title="${escapeHtml(task.workflow || '')}">${escapeHtml(task.workflow ? workflowName(task.workflow) : 'Workflow')}</a><button class="agent-details-toggle" type="button" popovertarget="agent-task-details" aria-haspopup="dialog">Details</button></span><span class="agent-meta-item">${startedLabel}&nbsp;${when(task.started_at || task.created_at)}</span><span class="agent-meta-item mono">${escapeHtml(duration(task))}</span></p>
      <div id="agent-task-details" class="agent-details-pop" popover role="dialog" aria-label="Details"><dl>
        <dt>${startedLabel}</dt><dd class="mono">${escapeHtml(formatTimestamp(task.started_at || task.created_at) || '—')}</dd>
        ${task.finished_at ? `<dt>Finished</dt><dd class="mono">${escapeHtml(formatTimestamp(task.finished_at))}</dd>` : ''}
        <dt>Duration</dt><dd class="mono">${escapeHtml(duration(task))}</dd>
        <dt>Engine</dt><dd>${escapeHtml(task.engine || 'Agent')}</dd>
        <dt>Task ID</dt><dd class="mono">${escapeHtml(task.task_id)}</dd>
        <dt>Required capabilities</dt><dd>${escapeHtml((task.requires || []).join(", ") || "None")}</dd>
        <dt>Workspace</dt><dd>${escapeHtml(task.workspace || 'Worker default')}</dd>
        ${task.exit_code != null ? `<dt>Exit code</dt><dd>${escapeHtml(String(task.exit_code))}</dd>` : ''}
        ${task.notified_at ? `<dt>Completion email sent</dt><dd>${escapeHtml(formatTimestamp(task.notified_at))}</dd>` : ''}
      </dl></div>
    </div>
    <section class="agent-result" aria-labelledby="agent-result-heading">
      <h3 id="agent-result-heading">${failed ? 'What went wrong' : active ? 'Progress' : 'Result'}</h3>
      ${result ? `<div class="agent-result-text">${renderMarkdown(result)}</div>` : `<div class="agent-progress"><p>${task.status === 'queued' ? 'Waiting for a worker.' : active ? 'The agent is working on this task.' : 'This run finished without a recorded result.'}</p>${active ? '<p class="sub">This screen checks for updates every 15 seconds. Output is available when the run finishes.</p>' : ''}</div>`}
    </section>
    <details class="agent-log-section"><summary>Logs ${logs?.stderr ? '<span class="agent-log-hint">Error output available</span>' : ''}</summary>
      ${logs ? `${logs.truncated ? '<p class="sub">Only the end of the output was retained.</p>' : ''}
        <div class="dk-tabs dk-tabs--sm agent-log-tabs" role="tablist" aria-label="Log stream"><button type="button" class="dk-tab" role="tab" id="agent-log-tab-stdout" data-agent-stream="stdout" aria-controls="agent-log-output" aria-selected="true" tabindex="0">Output</button><button type="button" class="dk-tab" role="tab" id="agent-log-tab-stderr" data-agent-stream="stderr" aria-controls="agent-log-output" aria-selected="false" tabindex="-1">Errors</button></div>
        <pre id="agent-log-output" class="agent-log-output" role="tabpanel" aria-labelledby="agent-log-tab-stdout" tabindex="0">${escapeHtml(logs.stdout || 'No output recorded.')}</pre>`
        : `<p class="sub">${active ? 'Logs will be available after this run finishes.' : 'Detailed logs were not recorded for this run. Its available result is shown above.'}</p>`}
    </details>
    </div>`;
}
async function selectTask(id, { updateUrl = true } = {}) {
  /* A newly picked run opens at its head; the 15s quiet refresh of the same
     run must not yank a reader back to the top of a long result. */
  const changed = selected !== id;
  const detailsOpen = !changed && Boolean(document.getElementById('agent-task-details')?.matches(':popover-open'));
  selected = id;
  if (changed) { detail = null; $('#agent-run-detail').scrollTop = 0; }
  if (id) lastOpened = id;
  const seq = ++detailSeq;
  renderList(); renderDetail();
  if (updateUrl) setTaskUrl(id);
  try {
    const data = await api(`/api/admin/agent-tasks?task_id=${encodeURIComponent(id)}`);
    if (seq !== detailSeq || selected !== id) return;
    detail = data.task; renderDetail();
    if (detailsOpen) document.getElementById('agent-task-details')?.showPopover();
  } catch (error) {
    if (seq !== detailSeq) return;
    $('#agent-run-detail').innerHTML = `<div class="dk-callout dk-callout--danger" role="alert"><div class="dk-callout__body"><p class="dk-callout__title">This run could not be loaded</p><p>The task service answered: ${escapeHtml(error.message)}</p></div><div class="dk-callout__actions"><button class="dk-button dk-button--secondary dk-button--sm" id="agent-detail-retry" type="button">Try again</button></div></div>`;
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
    const params = withOwner(new URLSearchParams({ limit: '200' }));
    const data = await api(`/api/admin/agent-tasks?${params}`);
    if (seq !== listSeq) return;
    tasks = data.tasks || []; fetched = true; renderList();
    renderWorkerNote(tasks, seq);
    if (state.view !== 'agents') return;
    /* The URL is the selection. Without one, a desktop opens the first
       active (else newest) run in place; a phone shows the list. */
    const fromUrl = new URLSearchParams(window.location.search).get('task');
    const desktop = window.matchMedia(DESKTOP).matches;
    const id = fromUrl || (desktop ? (selected || (tasks.find(t => ACTIVE.has(t.status)) || tasks[0])?.task_id) : null);
    if (!id) { closeTask(); return; }
    if (!quiet || id !== selected || !detail || ACTIVE.has(detail.status)) await selectTask(id);
    else renderDetail();
  } catch (error) {
    if (seq !== listSeq) return;
    if (!fetched) $('#agent-runs-list').innerHTML = '<li class="dk-empty dk-empty--compact"><p class="dk-empty__title">Agent runs could not be loaded</p><p class="dk-empty__desc">The task service did not answer. Use Refresh to try again.</p></li>';
    if (!quiet) notice(error.message, true);
  }
}
$('#agent-runs-search').addEventListener('input', event => { search = event.target.value.trim().toLowerCase(); renderList(); });
window.addEventListener('dapier-view-as', () => { void renderAgentTasks(); });
/* Back (phones): the list returns where it was, focus on the row it came
   from. The selection leaves the URL, so a reload lands on the list too. */
function closeTask() {
  ++detailSeq;
  selected = null; detail = null;
  setTaskUrl(null);
  renderList(); renderDetail();
  const scroller = $('#agent-runs-scroll');
  scroller.scrollTop = listScrollTop;
  const row = lastOpened && [...document.querySelectorAll('#agent-runs-list [data-agent-task]')].find(r => r.dataset.agentTask === lastOpened);
  if (row && !window.matchMedia(DESKTOP).matches) {
    const top = row.offsetTop - scroller.offsetTop;
    if (top < scroller.scrollTop || top + row.offsetHeight > scroller.scrollTop + scroller.clientHeight) scroller.scrollTop = Math.max(0, top - scroller.clientHeight / 3);
    row.focus({ preventScroll: true });
  }
}

document.addEventListener('click', event => {
  const filterButton = event.target.closest('[data-agent-filter]');
  if (filterButton) { filter = filterButton.dataset.agentFilter; renderList(); }
  const row = event.target.closest('a[data-agent-task]');
  if (row && !(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0)) {
    event.preventDefault();
    listScrollTop = $('#agent-runs-scroll').scrollTop;
    const id = row.dataset.agentTask;
    setTaskUrl(id, { push: true });
    selectTask(id, { updateUrl: false });
    globalThis.dakitSplit?.show(split(), 'detail'); // a phone moves focus to the run's title
  }
  const back = event.target.closest('.agent-back');
  if (back && !(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)) {
    event.preventDefault();
    const url = new URL(window.location.href);
    url.searchParams.delete('task');
    history.pushState(null, '', url);
    closeTask();
  }
  if (event.target.closest('#agent-detail-retry') && selected) selectTask(selected);
  const stream = event.target.closest('[data-agent-stream]');
  if (stream && detail?.logs) {
    document.querySelectorAll('[data-agent-stream]').forEach(button => button.setAttribute('aria-selected', String(button === stream)));
    $('.agent-log-output').setAttribute('aria-labelledby', stream.id);
    $('.agent-log-output').textContent = detail.logs[stream.dataset.agentStream] || (stream.dataset.agentStream === 'stderr' ? 'No errors recorded.' : 'No output recorded.');
  }
});
setInterval(() => {
  if (state.view === 'agents' && !document.hidden && (tasks.some(t => ACTIVE.has(t.status)) || ACTIVE.has(detail?.status))) renderAgentTasks({ quiet: true });
}, 15000);
