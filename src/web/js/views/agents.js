/* Agent runs across every workflow trigger. */
import { $, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp, wrapTokens } from '../format.js';

/* Agent tasks: the host jobs the `agent` action enqueued, as `dapier worker`
   leaves them. Not part of the overview payload — the view fetches
   /api/admin/agent-tasks itself (like the Runs view) and Refresh re-reads it. */
let agentTasksSeq = 0;

export async function renderAgentTasks() {
  const empty = $('#agent-tasks-empty');
  const wrap = $('#agent-tasks-wrap');
  const body = $('#agent-tasks');
  if (!empty || !wrap || !body) return;
  const seq = ++agentTasksSeq;
  let tasks = [];
  try {
    const data = await api('/api/admin/agent-tasks?limit=50');
    if (seq !== agentTasksSeq) return; // a newer fetch superseded this one
    tasks = data.tasks || [];
  } catch (error) {
    if (seq !== agentTasksSeq) return;
    empty.hidden = false;
    empty.textContent = 'Agent tasks are unavailable.';
    wrap.hidden = true;
    return;
  }
  empty.hidden = tasks.length > 0;
  empty.textContent = 'No tasks yet. An agent action enqueues one when its workflow runs.';
  wrap.hidden = tasks.length === 0;
  body.innerHTML = tasks.map((task) => `<tr>
      <td class="cell-title mono"><span class="cell-name">${escapeHtml(wrapTokens(task.task_id || ''))}</span><span class="cell-sub">${escapeHtml(task.engine || '')}</span></td>
      <td class="mono muted-cell" data-label="Workflow">${escapeHtml(task.workflow || '—')}</td>
      <td data-label="Status">${statusLine(task.status)}</td>
      <td class="mono muted-cell" data-label="Result"><button class="button secondary agent-task-open" type="button" data-task="${escapeHtml(task.task_id)}">View logs</button><span class="cell-sub">${escapeHtml(task.summary || task.error || '—')}</span></td>
      <td class="mono muted-cell" data-label="Updated">${escapeHtml(formatTimestamp(task.finished_at || task.started_at || task.created_at) || '—')}</td>
    </tr>`).join('');
}

$('#agent-tasks-refresh')?.addEventListener('click', () => renderAgentTasks());

$('#agent-tasks-refresh').addEventListener('click', renderAgentTasks);
let taskDetailSeq = 0;
document.addEventListener('click', async (event) => {
  const button = event.target.closest('.agent-task-open');
  if (!button) return;
  const seq = ++taskDetailSeq;
  $('#agent-task-title').textContent = 'Agent run';
  $('#agent-task-detail').textContent = 'Loading…';
  $('#agent-task-dialog').showModal();
  try {
    const data = await api(`/api/admin/agent-tasks?task_id=${encodeURIComponent(button.dataset.task)}`);
    if (seq !== taskDetailSeq) return;
    const task = data.task;
    const logs = task.logs;
    $('#agent-task-detail').innerHTML = `
      <p class="mono">${escapeHtml(task.task_id)}</p>
      <p>${statusLine(task.status)} · ${escapeHtml(task.engine || 'agent')}${task.exit_code != null ? ` · Exit ${escapeHtml(String(task.exit_code))}` : ''}</p>
      <p>Workflow: <a href="/workflows/${encodeURIComponent(task.workflow)}">${escapeHtml(task.workflow)}</a></p>
      <h3>Result</h3><pre class="email-flow-prompt">${escapeHtml(task.summary || task.error || 'No result yet.')}</pre>
      <h3>Logs</h3>
      ${logs ? `${logs.truncated ? '<p class="sub">Showing the end of the output; earlier logs were truncated.</p>' : ''}
        <h4>Standard output</h4><pre class="email-flow-prompt">${escapeHtml(logs.stdout || 'No output.')}</pre>
        <h4>Standard error</h4><pre class="email-flow-prompt">${escapeHtml(logs.stderr || 'No errors.')}</pre>`
        : `<p class="sub">${['queued', 'running'].includes(task.status) ? 'Logs are available after the agent finishes. Refresh to check its progress.' : 'This worker did not record logs. The available result is shown above.'}</p>`}`;
  } catch (error) {
    if (seq === taskDetailSeq) $('#agent-task-detail').textContent = error.message;
    notice(error.message, true);
  }
});
