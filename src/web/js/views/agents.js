/* A run inbox: scan activity, select a task, read its outcome. */
import { $, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp } from '../format.js';
import { state } from '../state.js';

const ACTIVE = new Set(['queued', 'running', 'claimed', 'starting', 'started']);
const FAILED = new Set(['failed', 'timed_out', 'interrupted']);
const LABELS = { succeeded: 'Completed', running: 'Running', queued: 'Queued', claimed: 'Starting', starting: 'Starting', started: 'Running', failed: 'Failed', timed_out: 'Timed out', interrupted: 'Interrupted' };
let tasks = [], selected = null, detail = null, filter = 'all', search = '', fetched = false;
let listSeq = 0, detailSeq = 0;

function title(task) {
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
    (!search || [title(task), task.workflow, task.engine, task.summary, task.task_id].join(' ').toLowerCase().includes(search)));
}
function renderList() {
  const rows = shown();
  const counts = { all: tasks.length, active: tasks.filter(t => ACTIVE.has(t.status)).length, attention: tasks.filter(t => FAILED.has(t.status)).length, completed: tasks.filter(t => t.status === 'succeeded').length };
  document.querySelectorAll('[data-agent-filter]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.agentFilter === filter));
    button.querySelector('span').textContent = counts[button.dataset.agentFilter];
  });
  $('#agent-runs-note').textContent = fetched ? `${rows.length} ${rows.length === 1 ? 'run' : 'runs'}${tasks.length === 200 ? ' · newest 200 loaded' : ''}` : 'Loading runs…';
  $('#agent-runs-list').innerHTML = rows.length ? rows.map(task => `<button type="button" class="agent-run-item ${task.task_id === selected ? 'selected' : ''}" data-agent-task="${escapeHtml(task.task_id).replace(/"/g, '&quot;')}" aria-current="${task.task_id === selected ? 'true' : 'false'}">
    <span class="agent-run-line"><strong>${escapeHtml(title(task))}</strong>${badge(task)}</span>
    <span class="agent-run-meta"><span>${escapeHtml(task.workflow || 'Agent run')}</span><time>${escapeHtml(date(task))}</time></span>
  </button>`).join('') : `<div class="agent-list-empty"><h3>${tasks.length ? 'No matching runs' : 'No agent runs yet'}</h3><p>${tasks.length ? 'Try another status or search.' : 'Runs appear here when a workflow starts an agent.'}</p></div>`;
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
function readableResult(text) {
  return String(text).split(/(https?:\/\/[^\s<>"']+)/g).map(part => {
    if (!/^https?:\/\//.test(part)) return escapeHtml(part);
    const link = part.replace(/[.,;]+$/, '');
    return `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(link)}</a>${escapeHtml(part.slice(link.length))}`;
  }).join('');
}

function renderDetail() {
  const panel = $('#agent-run-detail');
  $('.agent-workspace').classList.toggle('has-selection', Boolean(selected));
  if (!selected) {
    panel.innerHTML = '<div class="agent-detail-empty"><h3>Select a run</h3><p>Read its result and check what happened.</p></div>';
    return;
  }
  const task = detail;
  if (!task) { panel.innerHTML = '<p class="agent-detail-loading" role="status">Loading run…</p>'; return; }
  const active = ACTIVE.has(task.status), failed = FAILED.has(task.status), result = resultText(task);
  const logs = task.logs;
  panel.innerHTML = `<div class="agent-detail-head">
      <button class="agent-back button secondary" type="button">Back to runs</button>
      <div class="agent-detail-status">${badge(task)}<span>${escapeHtml(task.engine || 'Agent')} · ${escapeHtml(duration(task))}</span></div>
      <h2 tabindex="-1">${escapeHtml(title(task))}</h2>
      <a class="agent-workflow-link" href="/workflows/${encodeURIComponent(task.workflow || '')}">${escapeHtml(task.workflow || 'Workflow')}</a>
      <p class="agent-detail-date">${task.started_at ? 'Started' : task.status === 'queued' ? 'Queued' : 'Created'} ${escapeHtml(formatTimestamp(task.started_at || task.created_at) || '—')}${task.finished_at ? ` · Finished ${escapeHtml(formatTimestamp(task.finished_at))}` : ''}</p>
    </div>
    <section class="agent-result" aria-labelledby="agent-result-heading">
      <h3 id="agent-result-heading">${failed ? 'What went wrong' : active ? 'Progress' : 'Result'}</h3>
      ${result ? `<div class="agent-result-text">${readableResult(result)}</div>` : `<div class="agent-progress"><p>${task.status === 'queued' ? 'Waiting for a worker.' : active ? 'The agent is working on this task.' : 'This run finished without a recorded result.'}</p>${active ? '<p class="sub">This screen checks for updates every 15 seconds. Output is available when the run finishes.</p>' : ''}</div>`}
    </section>
    <details class="agent-log-section"><summary>Logs ${logs?.stderr ? '<span class="agent-log-hint">Error output available</span>' : ''}</summary>
      ${logs ? `${logs.truncated ? '<p class="sub">Only the end of the output was retained.</p>' : ''}
        <div class="agent-log-tabs" role="group" aria-label="Log stream"><button type="button" data-agent-stream="stdout" aria-pressed="true">Output</button><button type="button" data-agent-stream="stderr" aria-pressed="false">Errors</button></div>
        <pre class="agent-log-output" tabindex="0">${escapeHtml(logs.stdout || 'No output recorded.')}</pre>`
        : `<p class="sub">${active ? 'Logs will be available after this run finishes.' : 'Detailed logs were not recorded for this run. Its available result is shown above.'}</p>`}
    </details>
    <details class="agent-technical"><summary>Run details</summary><dl>
      <dt>Task ID</dt><dd class="mono">${escapeHtml(task.task_id)}</dd>
      <dt>Workspace</dt><dd>${escapeHtml(task.workspace || 'Worker default')}</dd>
      ${task.exit_code != null ? `<dt>Exit code</dt><dd>${escapeHtml(String(task.exit_code))}</dd>` : ''}
      ${task.notified_at ? `<dt>Completion email sent</dt><dd>${escapeHtml(formatTimestamp(task.notified_at))}</dd>` : ''}
    </dl></details>`;
}
async function selectTask(id, { updateUrl = true } = {}) {
  selected = id; detail = null;
  const seq = ++detailSeq;
  renderList(); renderDetail();
  if (updateUrl) setTaskUrl(id);
  try {
    const data = await api(`/api/admin/agent-tasks?task_id=${encodeURIComponent(id)}`);
    if (seq !== detailSeq || selected !== id) return;
    detail = data.task; renderDetail();
  } catch (error) {
    if (seq !== detailSeq) return;
    $('#agent-run-detail').innerHTML = `<div class="agent-detail-empty"><button class="agent-back button secondary" type="button">Back to runs</button><h3>Could not load this run</h3><p>${escapeHtml(error.message)}</p><button class="button secondary" id="agent-detail-retry" type="button">Try again</button></div>`;
  }
}
export async function renderAgentTasks({ quiet = false } = {}) {
  const seq = ++listSeq;
  const refresh = $('#agent-tasks-refresh');
  if (!quiet) refresh.disabled = true;
  try {
    const data = await api('/api/admin/agent-tasks?limit=200');
    if (seq !== listSeq) return;
    tasks = data.tasks || []; fetched = true; renderList();
    $('#agent-refresh-note').textContent = 'Updated just now';
    if (state.view !== 'agents') return;
    const fromUrl = state.view === 'agents' ? new URLSearchParams(window.location.search).get('task') : null;
    const id = fromUrl || selected || (window.matchMedia('(min-width: 1001px)').matches ? (tasks.find(t => ACTIVE.has(t.status)) || tasks[0])?.task_id : null);
    if (id && (!quiet || id !== selected || !detail || ACTIVE.has(detail.status))) await selectTask(id, { updateUrl: state.view === 'agents' });
    else renderDetail();
  } catch (error) {
    if (seq !== listSeq) return;
    $('#agent-refresh-note').textContent = 'Could not refresh · try again';
    if (!fetched) $('#agent-runs-list').innerHTML = '<div class="agent-list-empty"><h3>Runs are unavailable</h3><p>Use Refresh to try again.</p></div>';
    if (!quiet) notice(error.message, true);
  } finally { if (seq === listSeq) refresh.disabled = false; }
}
$('#agent-tasks-refresh').addEventListener('click', () => renderAgentTasks());
$('#agent-runs-search').addEventListener('input', event => { search = event.target.value.trim().toLowerCase(); renderList(); });
document.addEventListener('click', event => {
  const filterButton = event.target.closest('[data-agent-filter]');
  if (filterButton) { filter = filterButton.dataset.agentFilter; renderList(); }
  const row = event.target.closest('[data-agent-task]');
  if (row) selectTask(row.dataset.agentTask).then(() => {
    if (selected === row.dataset.agentTask && window.matchMedia('(max-width: 1000px)').matches) $('#agent-run-detail h2')?.focus();
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
