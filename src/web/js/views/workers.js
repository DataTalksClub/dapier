/* Host workers: the `dapier worker` processes agent tasks run on. */
import { $ } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp } from '../format.js';

/* --- row shaping (pure; pinned by tests/test_console_workers.py) ---------- */

/* "3m ago" from an epoch-seconds (or ISO) timestamp; '' when unknown. */
function agoText(value, nowMs) {
  if (value == null || value === '') return '';
  const ms = /^\d+$/.test(String(value)) ? Number(value) * 1000 : Date.parse(value);
  if (Number.isNaN(ms)) return '';
  const seconds = Math.max(0, Math.round((nowMs - ms) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/* Worker ids are "<hostname>-<pid>-<8 hex>". The hostname names the
   machine; the hex suffix tells two processes on it apart. */
function workerIdentity(worker) {
  const id = String(worker.worker_id || '');
  const match = id.match(/^(.*)-(\d+)-([0-9a-f]{8})$/);
  const host = worker.hostname || (match ? match[1] : id) || 'unknown';
  /* Like `hostname -s`: cloud FQDNs (ip-…eu-west-1.compute.internal) read
     by their first label; the full id stays in the tooltip. */
  const name = /^\d+(\.\d+){3}$/.test(host) ? host : host.split('.')[0];
  const pid = worker.pid || (match ? Number(match[2]) : null);
  const suffix = match ? match[3] : '';
  return { name, sub: [pid ? `pid ${pid}` : '', suffix].filter(Boolean).join(' · ') };
}

/* Agent task ids read "agent:<workflow>:<execution>:run"; the workflow is
   the part an operator recognises. Anything else shows as-is. */
function taskName(taskId) {
  const parts = String(taskId || '').split(':');
  if (parts[0] === 'agent' && parts.length >= 3 && parts[1]) return parts[1];
  return String(taskId || '');
}

function workerTask(worker) {
  if (worker.current_task_id) {
    return { name: taskName(worker.current_task_id), sub: 'running now', id: worker.current_task_id };
  }
  if (worker.last_task_id) {
    return { name: taskName(worker.last_task_id), sub: `last · ${worker.last_status || 'unknown'}`, id: worker.last_task_id };
  }
  return { name: '', sub: 'idle', id: '' };
}

/* Workspace roots are long absolute paths; the tail names the checkout. */
function workspaceText(path) {
  const value = String(path || '');
  if (!value) return '—';
  const parts = value.split('/').filter(Boolean);
  return value.length > 28 && parts.length > 1 ? `…/${parts[parts.length - 1]}` : value;
}

function capabilityText(worker) {
  const list = Array.isArray(worker.capabilities) ? worker.capabilities.filter(Boolean) : [];
  return list.length ? list.join(', ') : '—';
}

/* --- end row shaping ------------------------------------------------------ */

/* Not part of the overview payload — the view fetches /api/admin/workers
   itself (like the Agents view). The top-bar Refresh re-reads it. A worker
   is active while its last check-in is inside the API's active window. */
let workersSeq = 0;

function clip(text, title, cls = '') {
  const tip = title ? ` title="${escapeHtml(title)}"` : '';
  return `<span class="worker-clip ${cls}"${tip}>${escapeHtml(text)}</span>`;
}

function workerRow(worker, nowMs) {
  const who = workerIdentity(worker);
  const task = workerTask(worker);
  const seen = agoText(worker.last_seen, nowMs);
  const started = agoText(worker.started_at, nowMs);
  const capabilities = capabilityText(worker);
  return `<tr>
      <td class="cell-title">${clip(who.name, worker.worker_id, 'cell-name')}${who.sub ? clip(who.sub, '', 'cell-sub') : ''}</td>
      <td data-label="Status"><span class="worker-status">${statusLine(worker.active ? 'active' : 'offline')}${seen ? `<span class="cell-sub" title="${escapeHtml(formatTimestamp(worker.last_seen) || '')}">seen ${escapeHtml(seen)}</span>` : ''}</span></td>
      <td data-label="Task"><span class="worker-stack">${task.name ? clip(task.name, task.id, 'worker-task') : ''}${clip(task.sub, '', 'cell-sub')}</span></td>
      <td data-label="Engine"><span class="worker-stack"><span class="dk-badge worker-engine">${escapeHtml(worker.engine || 'claude')}</span><span class="worker-capabilities"${capabilities === '—' ? ' title="No extra capabilities"' : ''}>${escapeHtml(capabilities)}</span></span></td>
      <td data-label="Workspace">${clip(workspaceText(worker.workspace_root), worker.workspace_root || '', 'mono worker-path')}</td>
      <td data-label="Started"><span class="worker-started" title="${escapeHtml(formatTimestamp(worker.started_at) || '')}">${escapeHtml(started || '—')}</span></td>
    </tr>`;
}

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
  const now = Date.now();
  body.innerHTML = workers.map((worker) => workerRow(worker, now)).join('');
}
