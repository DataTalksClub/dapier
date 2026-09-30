/* Audit view: the operator-action trail with server-side search,
   filters, paging, and CSV export. */
import { $, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, formatTimestamp } from '../format.js';

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

function eventRow(item) {
  const isProblem = /error|denied|fail/.test(item.outcome || '');
  return `<tr>
    <td class="mono muted-cell" data-label="When">${escapeHtml(formatTimestamp(item.timestamp) || '—')}</td>
    <td class="mono" data-label="Action"><span class="cell-name">${escapeHtml(item.action || '?')}</span></td>
    <td class="mono muted-cell" data-label="Resource">${escapeHtml(item.connection_id || '—')}</td>
    <td class="mono muted-cell" data-label="Actor">${escapeHtml(item.actor_subject || '—')}</td>
    <td class="mono muted-cell" data-label="Agent">${escapeHtml(item.agent || '—')}</td>
    <td class="mono ${isProblem ? 'audit-problem' : 'muted-cell'}" data-label="Outcome">${escapeHtml(item.outcome || '—')}</td>
    <td class="muted-cell" data-label="Error" title="${escapeHtml(item.error || '')}">${escapeHtml(item.error || '—')}</td>
  </tr>`;
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
    note.textContent = 'Showing up to 25 events.';
  } else {
    note.textContent = `Showing ${events.length} events${auditPage.nextToken ? ' · more history available' : ''}`;
  }
  $('#audit-log-table').innerHTML = events.map(eventRow).join('') ||
    '<tr><td colspan="7" class="muted-cell">No audit events match these filters</td></tr>';
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
