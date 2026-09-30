/* Host workers: the `dapier worker` processes agent tasks run on. */
import { $ } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp, wrapTokens } from '../format.js';

/* Not part of the overview payload — the view fetches /api/admin/workers
   itself (like the Agents view) and Refresh re-reads it. A worker is active
   while its last check-in (every claim poll, every task heartbeat) is inside
   the API's active window; the API makes that call so both surfaces agree. */
let workersSeq = 0;

export async function renderWorkers() {
  const empty = $('#workers-empty');
  const wrap = $('#workers-wrap');
  const body = $('#workers');
  if (!empty || !wrap || !body) return;
  const seq = ++workersSeq;
  let data;
  try {
    data = await api('/api/admin/workers');
    if (seq !== workersSeq) return; // a newer fetch superseded this one
  } catch (error) {
    if (seq !== workersSeq) return;
    empty.hidden = false;
    empty.textContent = 'Workers are unavailable.';
    wrap.hidden = true;
    return;
  }
  const workers = data.workers || [];
  empty.hidden = workers.length > 0;
  empty.innerHTML = 'No worker has checked in. Start one with <code>dapier worker</code> — agent tasks stay queued until a worker picks them up.';
  wrap.hidden = workers.length === 0;
  body.innerHTML = workers.map((worker) => {
    const tasks = worker.current_task_id
      ? `<span class="cell-name">${wrapTokens(worker.current_task_id)}</span><span class="cell-sub">running now</span>`
      : (worker.last_task_id
        ? `<span class="cell-sub">last: ${wrapTokens(worker.last_task_id)} · ${escapeHtml(worker.last_status || 'unknown')}</span>`
        : '<span class="cell-sub">idle</span>');
    return `<tr>
      <td class="cell-title mono"><span class="cell-name">${wrapTokens(worker.worker_id || '')}</span><span class="cell-sub">${escapeHtml([worker.hostname, worker.pid ? `pid ${worker.pid}` : ''].filter(Boolean).join(' · '))}</span></td>
      <td data-label="Status">${statusLine(worker.active ? 'active' : 'offline')}<span class="cell-sub">seen ${escapeHtml(formatTimestamp(worker.last_seen)) || '—'}</span></td>
      <td class="mono muted-cell" data-label="Tasks">${tasks}</td>
      <td class="mono muted-cell" data-label="Workspace">${escapeHtml(worker.workspace_root || '—')}</td>
      <td class="mono muted-cell" data-label="Started">${escapeHtml(formatTimestamp(worker.started_at)) || '—'}</td>
    </tr>`;
  }).join('');
}

$('#workers-refresh').addEventListener('click', renderWorkers);
