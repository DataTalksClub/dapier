/* Formatting helpers: escaping, status pills, one timestamp family. */
export const pad2 = (value) => String(value).padStart(2, '0');

export function escapeHtml(value) {
  const element = document.createElement('span');
  element.textContent = value == null ? '' : String(value);
  return element.innerHTML;
}

export function statusLine(status, labels) {
  const value = String(status || 'unknown');
  const kind = ['completed', 'succeeded', 'reused', 'connected', 'configured', 'enabled', 'active'].includes(value) ? 'ok'
    : ['processing', 'running', 'ready'].includes(value) ? 'run'
    : ['failed', 'timed_out', 'interrupted', 'error', 'missing', 'expired', 'auto-paused'].includes(value) ? 'err'
    : 'off';
  const text = (labels || {})[value] || value;
  return `<span class="status ${kind}"><span class="status-dot" aria-hidden="true"></span>${escapeHtml(text)}</span>`;
}

/* A failure that no longer needs action. The run keeps its status pill
   (a fixed run still reads "failed" in history); this rides next to it so
   the actionable failures stand out from the retired ones. The tooltip
   says why it stopped being a problem. */
export function resolvedLine(run) {
  if (!run || !run.resolved) return '';
  const why = run.resolved_reason === 'acknowledged'
    ? 'Marked fixed by an operator — it stays in history but is no longer a problem.'
    : 'Recovered: a later run of this workflow completed, so this failure no longer needs action.';
  const at = run.resolved_at ? ` (${formatTimestamp(run.resolved_at)})` : '';
  return `<span class="status ok" title="${escapeHtml(why)}"><span class="status-dot" aria-hidden="true"></span>fixed${escapeHtml(at)}</span>`;
}

/* Escape, then mark separator characters as safe wrap points so long machine
   values (execution IDs, scope URLs) break at ":", ".", "/", "_" — never mid-token. */
export function wrapTokens(value) {
  return escapeHtml(value).replace(/([:._/])/g, '$1<wbr>');
}

export function triggerLabel(workflow) {
  const trigger = workflow.trigger;
  if (!trigger || !trigger.connector) return 'No trigger yet';
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

/* Machine data (step input/output) as one escaped JSON block, keys picked
   out so the eye can follow structure. */
export function jsonBlock(value) {
  if (value == null || value === '') return '';
  let text;
  try {
    text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  } catch (_) {
    text = String(value);
  }
  const html = escapeHtml(text).replace(/("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b/g, (match, str, colon, literal) => {
    if (str) return colon ? `<span class="j-key">${str}</span>${colon}` : `<span class="j-str">${str}</span>`;
    return `<span class="j-lit">${literal}</span>`;
  });
  return `<pre class="json-block">${html}</pre>`;
}

/* Human layer over machine data: the informative fields flattened to dotted
   paths (nested objects become one row each), long strings collapsed to one
   line and truncated — the untouched JSON stays one click away below. */
const DIGEST_ROWS = 10;
const DIGEST_DEPTH = 3;
const DIGEST_TEXT = 160;

/* Fields that only add noise at digest level: nulls, empty strings, empty
   objects. Zero and false stay — they usually are the answer. */
function emptyish(value) {
  return value === null || value === undefined || value === '' ||
    (typeof value === 'object' && !Array.isArray(value) && !Object.keys(value).length);
}

/* Most informative first: real text and meaningful flags outrank numbers,
   id-shaped keys (`*_id`, `id`) sink — they are machine bookkeeping. Depth
   nudges toward near-root fields; equal scores keep their natural order. */
const ID_KEY = /(^|_)(id|ids)$|_id$/;

function leafScore(path, value, text) {
  let score;
  if (typeof value === 'boolean') score = value ? 26 : 18;
  else if (typeof value === 'number') score = 12;
  else score = 30 + Math.min(20, text.length / 12);
  if (typeof value !== 'object' && ID_KEY.test(path.split('.').pop())) score -= 25;
  return score - path.split('.').length * 2;
}

function digestRows(value) {
  const rows = [];
  const walk = (node, path, depth) => {
    if (node === null || typeof node !== 'object') {
      const text = typeof node === 'string'
        ? node.replace(/\s+/g, ' ').trim().slice(0, DIGEST_TEXT) + (node.length > DIGEST_TEXT ? '…' : '')
        : String(node);
      rows.push([path, text, leafScore(path, node, text)]);
      return;
    }
    const entries = Array.isArray(node)
      ? node.map((item, index) => [String(index), item])
      : Object.entries(node);
    if (depth >= DIGEST_DEPTH || !entries.some(([, item]) => !emptyish(item))) {
      const text = Array.isArray(node) ? `[${entries.length} items]` : `{${entries.length} keys}`;
      rows.push([path, text, 16]);
      return;
    }
    for (const [key, item] of entries) {
      if (!emptyish(item)) walk(item, path ? `${path}.${key}` : key, depth + 1);
    }
  };
  walk(value, '', 0);
  return rows.sort((a, b) => b[2] - a[2]).map(([key, text]) => [key, text]);
}

/* Render one data payload: digest first, raw JSON in a collapsed <details>
   (open for failed steps, where the detail is the point). '' when there is
   nothing to show, so callers can fall back to their Empty note. */
export function dataBlock(value, { openRaw = false } = {}) {
  if (value == null || value === '') return '';
  if (typeof value === 'object' && !Array.isArray(value) && !Object.keys(value).length) return '';
  const rows = digestRows(value);
  const digest = rows.slice(0, DIGEST_ROWS).map(([key, text]) => key
    ? `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(text)}</dd></div>`
    : `<div class="json-digest-solo"><dd>${escapeHtml(text)}</dd></div>`).join('') +
    (rows.length > DIGEST_ROWS
      ? `<div class="json-digest-rest"><dd>+ ${rows.length - DIGEST_ROWS} more fields — see raw JSON</dd></div>`
      : '');
  return `${digest ? `<dl class="json-digest">${digest}</dl>` : ''}
    <details class="json-raw"${openRaw ? ' open' : ''}><summary>Raw JSON</summary>${jsonBlock(value)}</details>`;
}

export function emptyRow(columns) {
  return `<tr><td colspan="${columns}" style="color:var(--dk-text-muted)">No activity</td></tr>`;
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
