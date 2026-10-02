/* Bookkeeping view: parsed invoices awaiting confirmation. The queue is
   the human gate of the invoice pipeline — the workflow stages pending
   entries; nothing reaches the ledger until one is confirmed here (or
   through `dapier bookkeeping confirm`). */
import { state } from '../state.js';
import { $, $$, icons, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, formatTimestamp, dataBlock } from '../format.js';

let entries = [];
let counts = null;
let filter = 'pending';
let openId = null;

/* The fields a reviewer may correct, rendered as inputs in the detail
   dialog; everything else is provenance and stays read-only. */
const EDITABLE = [
  ['provider', 'Provider'],
  ['what', 'What'],
  ['invoice_number', 'Invoice number'],
  ['date_issued', 'Date issued'],
  ['date_paid', 'Date paid'],
  ['amount', 'Amount'],
  ['currency', 'Currency'],
  ['vat', 'VAT'],
  ['period', 'Period'],
  ['period_month', 'Period month'],
  ['account', 'Account'],
];

function sourceBadge(entry) {
  const source = entry.source || '?';
  const title = source === 'ai'
    ? 'AI-extracted — double-check every field'
    : 'Parsed by a deterministic vendor template';
  return `<span class="mono" title="${title}">${escapeHtml(source)}</span>`;
}

function entryRow(entry) {
  const fields = entry.entry || {};
  return `<tr class="bookkeeping-open" data-entry="${escapeHtml(entry.entry_id)}" role="button" tabindex="0">
    <td class="mono muted-cell" data-label="Created">${escapeHtml(formatTimestamp(entry.created_at) || '—')}</td>
    <td data-label="Status">${escapeHtml(entry.status || '—')}</td>
    <td data-label="Source">${sourceBadge(entry)}</td>
    <td data-label="Provider">${escapeHtml(fields.provider || '—')}</td>
    <td class="mono" data-label="Invoice">${escapeHtml(fields.invoice_number || '—')}</td>
    <td data-label="What">${escapeHtml(fields.what || '—')}</td>
    <td class="mono" data-label="Amount">${escapeHtml(String(fields.amount ?? '—'))} ${escapeHtml(fields.currency || '')}</td>
  </tr>`;
}

function renderRows() {
  $('#bookkeeping-counts').textContent = counts
    ? `pending ${counts.pending ?? '—'} · confirmed ${counts.confirmed ?? '—'} · rejected ${counts.rejected ?? '—'}` : '';
  $('#bookkeeping-table').innerHTML = entries.map(entryRow).join('');
  $('#bookkeeping-empty').hidden = entries.length > 0;
  $('#bookkeeping-table-wrap').hidden = entries.length === 0;
  $$('.bookkeeping-filter').forEach((button) => {
    button.classList.toggle('active', button.dataset.status === filter);
  });
  icons();
}

export async function fetchBookkeeping() {
  try {
    const params = new URLSearchParams({ limit: '200' });
    if (filter && filter !== 'all') params.set('status', filter);
    const data = await api(`/api/admin/bookkeeping?${params}`);
    entries = data.entries || [];
    counts = data.counts || null;
  } catch (error) {
    notice(error.message, true);
  }
  renderRows();
}

export function renderBookkeeping() {
  if (state.view === 'bookkeeping') void fetchBookkeeping();
  else renderRows();
}

function fieldInput(name, label, value) {
  return `<label class="detail-field"><span>${escapeHtml(label)}</span>
    <input class="bookkeeping-field mono-input" data-field="${escapeHtml(name)}" value="${escapeHtml(value ?? '')}"></label>`;
}

async function openEntry(entryId) {
  openId = entryId;
  try {
    const entry = await api(`/api/admin/bookkeeping/${encodeURIComponent(entryId)}`);
    const fields = entry.entry || {};
    const pending = entry.status === 'pending';
    $('#bookkeeping-entry-title').textContent =
      `${fields.provider || 'Invoice'} ${fields.invoice_number || ''}`.trim();
    const inputs = EDITABLE.map(([name, label]) => pending
      ? fieldInput(name, label, fields[name])
      : `<div class="detail-field"><span>${escapeHtml(label)}</span><p class="mono">${escapeHtml(String(fields[name] ?? '—'))}</p></div>`).join('');
    const provenance = {
      status: entry.status,
      source: entry.source,
      workflow: entry.workflow_id,
      created: entry.created_at,
      confirmed: entry.confirmed_at,
      confirmed_by: entry.confirmed_by,
      rejected: entry.rejected_at,
      note: entry.reject_note,
    };
    $('#bookkeeping-entry-detail').innerHTML = `
      <div class="detail-grid">${inputs}</div>
      <h3 class="sub-head">Provenance</h3>
      ${dataBlock(provenance)}
      ${entry.message ? `<h3 class="sub-head">Arrived with</h3>${dataBlock(entry.message)}` : ''}
      ${entry.pdf ? `<h3 class="sub-head">PDF</h3>${dataBlock(entry.pdf)}` : ''}`;
    $('#bookkeeping-confirm-button').hidden = !pending;
    $('#bookkeeping-reject-button').hidden = !pending;
    $('#bookkeeping-entry-dialog').showModal();
  } catch (error) {
    notice(error.message, true);
  }
}

function collectEdits() {
  const edits = {};
  $$('#bookkeeping-entry-detail .bookkeeping-field').forEach((input) => {
    const raw = input.value.trim();
    const key = input.dataset.field;
    const numeric = ['amount', 'vat'].includes(key);
    if (raw === '') { edits[key] = null; return; }
    edits[key] = numeric ? Number(raw) : raw;
  });
  return edits;
}

document.addEventListener('click', async (event) => {
  const confirm = event.target.closest('#bookkeeping-confirm-button');
  if (confirm && openId) {
    confirm.disabled = true;
    try {
      await api(`/api/admin/bookkeeping/${encodeURIComponent(openId)}/confirm`, {
        method: 'POST',
        body: JSON.stringify({ edits: collectEdits() }),
      });
      notice('Entry confirmed — it can reach the ledger now.');
      $('#bookkeeping-entry-dialog').close();
      await fetchBookkeeping();
    } catch (error) {
      notice(error.message, true);
    } finally {
      confirm.disabled = false;
    }
    return;
  }
  const reject = event.target.closest('#bookkeeping-reject-button');
  if (reject && openId) {
    reject.disabled = true;
    try {
      await api(`/api/admin/bookkeeping/${encodeURIComponent(openId)}/reject`, {
        method: 'POST',
        body: '{}',
      });
      notice('Entry rejected. Forwarding the invoice again stages a fresh one.');
      $('#bookkeeping-entry-dialog').close();
      await fetchBookkeeping();
    } catch (error) {
      notice(error.message, true);
    } finally {
      reject.disabled = false;
    }
    return;
  }
  const row = event.target.closest('.bookkeeping-open');
  if (row && row.dataset.entry) openEntry(row.dataset.entry);
  const filterButton = event.target.closest('.bookkeeping-filter');
  if (filterButton && filterButton.dataset.status !== filter) {
    filter = filterButton.dataset.status;
    void fetchBookkeeping();
  }
});

/* Entering the view fetches fresh queue state. */
document.addEventListener('click', (event) => {
  if (event.target.closest('.nav-item[data-view="bookkeeping"]')) void fetchBookkeeping();
});
window.addEventListener('popstate', () => {
  if (state.view === 'bookkeeping') void fetchBookkeeping();
});
