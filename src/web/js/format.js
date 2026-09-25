/* Formatting helpers: escaping, status pills, one timestamp family. */
export const pad2 = (value) => String(value).padStart(2, '0');

export function escapeHtml(value) {
  const element = document.createElement('span');
  element.textContent = value == null ? '' : String(value);
  return element.innerHTML;
}

export function statusLine(status, labels) {
  const value = String(status || 'unknown');
  const kind = ['completed', 'connected', 'configured', 'enabled', 'active'].includes(value) ? 'ok'
    : ['processing', 'ready'].includes(value) ? 'run'
    : ['failed', 'error', 'missing', 'expired'].includes(value) ? 'err'
    : 'off';
  const text = (labels || {})[value] || value;
  return `<span class="status ${kind}"><span class="status-dot" aria-hidden="true"></span>${escapeHtml(text)}</span>`;
}

/* Escape, then mark separator characters as safe wrap points so long machine
   values (execution IDs, scope URLs) break at ":", ".", "/", "_" — never mid-token. */
export function wrapTokens(value) {
  return escapeHtml(value).replace(/([:._/])/g, '$1<wbr>');
}

export function triggerLabel(workflow) {
  const trigger = workflow.trigger;
  const base = `${trigger.connector} · ${trigger.event}`;
  const extra = (workflow.triggerCount || 1) - 1;
  return extra > 0 ? `${base} +${extra}` : base;
}

/* One timestamp family everywhere: YYYY-MM-DD HH:MM (24h, local). */
function toDate(value) {
  return /^\d+$/.test(String(value)) ? new Date(Number(value) * 1000) : new Date(value);
}

export function formatTimestamp(value) {
  if (value == null || value === '') return null;
  const date = toDate(value);
  return Number.isNaN(date.getTime()) ? String(value)
    : `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

export function formatDay(value) {
  if (value == null || value === '') return null;
  const date = toDate(value);
  return Number.isNaN(date.getTime()) ? String(value)
    : `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

/* Step timing: sub-second stays in ms, everything else in seconds. */
export function formatDuration(ms) {
  if (ms == null || ms === '') return null;
  const value = Number(ms);
  if (Number.isNaN(value)) return String(ms);
  return value < 1000 ? `${Math.round(value)}ms` : `${(value / 1000).toFixed(value < 10000 ? 2 : 1)}s`;
}

/* Machine data (step input/output) as one escaped JSON block. */
export function jsonBlock(value) {
  if (value == null || value === '') return '';
  let text;
  try {
    text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  } catch (_) {
    text = String(value);
  }
  return `<pre class="json-block">${escapeHtml(text)}</pre>`;
}

export function emptyRow(columns) {
  return `<tr><td colspan="${columns}" style="color:var(--muted)">No activity</td></tr>`;
}

export function configRows(config) {
  const entries = Object.entries(config).filter(([key]) => key !== 'id' && key !== 'type');
  if (!entries.length) return '<p class="detail-muted">No parameters.</p>';
  return `<dl class="detail-list">${entries.map(([key, value]) => {
    const text = typeof value === 'object' && value !== null ? JSON.stringify(value, null, 2) : String(value);
    return `<div><dt>${escapeHtml(key)}</dt><dd><pre>${escapeHtml(text)}</pre></dd></div>`;
  }).join('')}</dl>`;
}

export function detailRows(rows) {
  const entries = rows.filter(([, value]) => value !== undefined && value !== null && value !== '');
  if (!entries.length) return '<p class="detail-muted">No details recorded.</p>';
  return `<dl class="detail-list">${entries.map(([label, value]) =>
    `<div><dt>${escapeHtml(label)}</dt><dd><pre>${escapeHtml(String(value))}</pre></dd></div>`).join('')}</dl>`;
}
