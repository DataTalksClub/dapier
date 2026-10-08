/* Audit view: the operator-action trail with server-side search,
   filters, paging, and CSV export. */
import { $, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, formatTimestamp, sentenceCase } from '../format.js';

const auditPage = { events: null, nextToken: null, filters: '' };
let fetchSeq = 0;

function selectedFilters() {
  return {
    search: $('#audit-log-search').value.trim(),
    action: $('#audit-log-action-filter').value,
    outcome: $('#audit-log-outcome-filter').value,
    date: $('#audit-log-date-filter').value,
  };
}

/* The date select as the API's `since`, like the runs view. */
function sinceFor(date) {
  const spans = { day: 86400000, week: 604800000 };
  return spans[date] ? new Date(Date.now() - spans[date]).toISOString() : '';
}

function queryParams({ search, action, outcome, date }, { limit, next } = {}) {
  const since = sinceFor(date);
  const params = new URLSearchParams({ limit: String(limit || 25) });
  if (search) params.set('q', search);
  if (action) params.set('action', action);
  if (outcome) params.set('outcome', outcome);
  if (since) params.set('since', since);
  if (next) params.set('next', next);
  return params;
}

async function fetchAuditPage({ append = false } = {}) {
  const filters = selectedFilters();
  const params = queryParams(filters, { next: append ? auditPage.nextToken : null });
  const seq = ++fetchSeq;
  try {
    const data = await api(`/api/admin/audit?${params}`);
    if (seq !== fetchSeq) return; // a newer fetch superseded this one
    const fresh = data.events || [];
    auditPage.nextToken = (data.paging || {}).next || null;
    auditPage.events = append && auditPage.events ? [...auditPage.events, ...fresh] : fresh;
    auditPage.filters = params.toString();
  } catch (error) {
    if (seq === fetchSeq && !append) { auditPage.events = []; auditPage.nextToken = null; }
    notice(error.message, true);
  }
  renderAudit();
}

/* "connections.test" / "hook-trigger" read as words; the machine value
   stays in the title for copying and in the filter. */
function actionLabel(action) {
  return sentenceCase(String(action || '?').replace(/[.\-_]+/g, ' '));
}

/* One status language: ok is a quiet success dot; anything denied or
   failed is the danger dot with its reason in words. */
function outcomeCell(outcome) {
  const value = String(outcome || '');
  if (!value) return '<span class="muted-cell">—</span>';
  const problem = /error|denied|fail/.test(value);
  const [head, ...rest] = value.split('-');
  const text = value === 'ok' ? 'OK'
    : rest.length ? `${sentenceCase(head)}: ${rest.join(' ')}` : sentenceCase(value);
  return `<span class="status ${problem ? 'err' : 'ok'}" title="${escapeHtml(value)}"><span class="status-dot" aria-hidden="true"></span><span class="status-text">${escapeHtml(text)}</span></span>`;
}

function clipCell(value, { mono = true } = {}) {
  if (!value) return { html: '<span class="muted-cell">—</span>', empty: true };
  return { html: `<span class="clip${mono ? ' mono' : ''}" title="${escapeHtml(value)}">${escapeHtml(value)}</span>`, empty: false };
}

function eventRow(item) {
  const resource = item.connection_id && item.connection_id !== 'unknown' ? item.connection_id : '';
  const cells = [
    ['When', { html: `<span class="mono nowrap">${escapeHtml(formatTimestamp(item.timestamp) || '—')}</span>`, empty: false }, 'audit-when'],
    ['Action', { html: `<span class="cell-name clip" title="${escapeHtml(item.action || '')}">${escapeHtml(actionLabel(item.action))}</span>`, empty: false }, 'cell-title audit-action'],
    ['Resource', clipCell(resource), 'audit-resource'],
    ['Actor', clipCell(item.actor_subject), 'audit-actor'],
    ['Agent', clipCell(item.agent), 'audit-agent'],
    /* A problem row's error rides under its outcome as a muted second
       line (the phone card shows it as its own full-width line below). */
    ['Outcome', { html: outcomeCell(item.outcome) + (item.error
      ? `<span class="cell-note audit-outcome-error" title="${escapeHtml(item.error)}">${escapeHtml(item.error)}</span>` : ''), empty: !item.outcome }, 'audit-outcome'],
    ['Error', item.error
      ? { html: `<span class="clamp-2" title="${escapeHtml(item.error)}">${escapeHtml(item.error)}</span>`, empty: false }
      : { html: '<span class="muted-cell">—</span>', empty: true }, 'audit-error'],
  ];
  return `<tr>${cells.map(([label, cell, cls]) =>
    `<td class="${cls}" data-label="${label}"${cell.empty ? ' data-empty' : ''}>${cell.html}</td>`).join('')}</tr>`;
}

/* Keep the chosen option visible across paged fetches, like the runs view. */
function fillSelect(select, values) {
  const selected = select.value;
  const options = new Set(values.filter(Boolean));
  if (selected) options.add(selected);
  select.innerHTML = `<option value="">${select.dataset.blank || 'All'}</option>` +
    [...options].sort().map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join('');
  select.value = selected;
}

export function renderAudit() {
  const events = auditPage.events || [];
  fillSelect($('#audit-log-action-filter'), events.map((item) => item.action));
  fillSelect($('#audit-log-outcome-filter'), events.map((item) => item.outcome));
  const note = $('#audit-log-note');
  if (auditPage.events === null) {
    note.textContent = 'Loading events…';
  } else {
    note.textContent = `Showing ${events.length} event${events.length === 1 ? '' : 's'}${auditPage.nextToken ? ', more to load' : ''}`;
  }
  note.title = 'Records are kept for 90 days.';
  $('#audit-log-table').innerHTML = events.map(eventRow).join('') ||
    '<tr class="register-empty"><td colspan="6"><strong>No audit events match these filters</strong></td></tr>';
  $('#audit-log-load-more').hidden = !auditPage.nextToken;
}

/* Entering the view always re-fetches: the trail only grows, and a stale
   page from a previous visit would page from the wrong token. */
export function refreshAudit() {
  auditPage.events = null;
  auditPage.nextToken = null;
  return fetchAuditPage();
}

$('#audit-log-load-more').addEventListener('click', () => fetchAuditPage({ append: true }));
$('#audit-log-action-filter').addEventListener('change', () => fetchAuditPage());
$('#audit-log-outcome-filter').addEventListener('change', () => fetchAuditPage());
$('#audit-log-date-filter').addEventListener('change', () => fetchAuditPage());
$('#audit-log-search').addEventListener('search', () => fetchAuditPage());

$('#audit-log-export').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const params = queryParams(selectedFilters(), { limit: 1000 });
    const data = await api(`/api/admin/audit/export?${params}`);
    const blob = new Blob([data.csv || ''], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = data.filename || 'dapier-audit.csv';
    link.click();
    URL.revokeObjectURL(url);
    notice(`Exported ${data.count || 0} audit events${data.truncated ? ' (capped — narrow the filters for the rest)' : ''}`);
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
});
