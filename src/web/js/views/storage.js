/* Data store view: each workflow's key-value state — the store the
   storage_set/get/find/delete steps run on — over /api/admin/storage. */
import { $, $$, notice } from '../ui.js';
import { api } from '../api.js';
import { state } from '../state.js';
import { escapeHtml, formatTimestamp } from '../format.js';

let workflow = '';

/* The workflow picker mirrors the Run history filter: options come from the
   overview payload the console already loads, so choosing a workflow never
   means typing an id from memory. */
function fillWorkflowSelect() {
  const select = $('#storage-browse').workflow;
  const ids = [...new Set([
    ...(state.data?.workflows || []).map((entry) => entry.id),
    workflow,
  ].filter(Boolean))].sort();
  select.innerHTML = '<option value="">Choose a workflow…</option>' +
    ids.map((id) => `<option value="${escapeHtml(id)}"${id === workflow ? ' selected' : ''}>${escapeHtml(id)}</option>`).join('');
}

function showIdle() {
  $('#storage-idle').hidden = false;
  $('#storage-empty').hidden = true;
  $('#storage-table-wrap').hidden = true;
}

/* The write form names the workflow it will hit, so a picker change can't
   silently redirect a store into another workflow's keys. */
function updatePutTarget() {
  $('#storage-put-target').hidden = !workflow;
  $('#storage-put-workflow').textContent = workflow;
}

function renderItems(data) {
  const items = data.items || [];
  $('#storage-idle').hidden = true;
  $('#storage-empty').hidden = items.length > 0;
  $('#storage-table-wrap').hidden = items.length === 0;
  $('#storage-empty-title').textContent = `No stored keys for ${workflow}`;
  $('#storage-table').innerHTML = items.map((item) => `<tr>
      <td class="cell-title"><span class="cell-name mono clip" title="${escapeHtml(item.key)}">${escapeHtml(item.key)}</span></td>
      <td class="mono muted-cell storage-value-cell" data-label="Value"><span class="storage-value-text">${escapeHtml(item.value)}</span></td>
      <td class="mono muted-cell nowrap" data-label="Updated">${formatTimestamp(item.updated_at) || '—'}</td>
      <td class="mono muted-cell nowrap" data-label="Expires"${item.expires ? '' : ' data-empty'}>${item.expires
        ? formatTimestamp(new Date(item.expires * 1000).toISOString()) || item.expires : 'Never'}</td>
      <td class="action-cell"><button class="dk-button dk-button--danger dk-button--sm storage-delete" data-key="${escapeHtml(item.key)}" type="button">Delete</button></td>
    </tr>`).join('');
  /* Only overflowing values get the expand affordance; short ones stay quiet. */
  $$('#storage-table .storage-value-text').forEach((el) => {
    if (el.scrollWidth > el.clientWidth || el.scrollHeight > el.clientHeight) el.title = 'Click to expand';
  });
}

async function listKeys() {
  const prefix = $('#storage-browse').prefix.value.trim();
  const query = prefix ? `?prefix=${encodeURIComponent(prefix)}` : '';
  try {
    renderItems(await api(`/api/admin/storage/${encodeURIComponent(workflow)}${query}`));
  } catch (error) { notice(error.message, true); }
}

/* Choosing a workflow lists its keys right away; the form submit remains for
   applying a prefix filter and refreshing. */
$('#storage-browse').workflow.addEventListener('change', (event) => {
  workflow = event.currentTarget.value;
  updatePutTarget();
  if (workflow) void listKeys(); else showIdle();
});

$('#storage-browse').addEventListener('submit', (event) => {
  event.preventDefault();
  workflow = event.currentTarget.workflow.value;
  updatePutTarget();
  if (workflow) void listKeys();
});

const putForm = $('#storage-put');
const valueField = putForm.value;

/* The value box starts at input height so the form reads as one line, and
   grows with its content (capped at the CSS max-height) instead of sitting
   as a tall empty hole. */
function autosizeValue() {
  valueField.style.height = 'auto';
  valueField.style.height = `${Math.min(valueField.scrollHeight, 224)}px`;
}
valueField.addEventListener('input', autosizeValue);

putForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!workflow) return notice('Choose a workflow above first.', true);
  const submit = putForm.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  submit.textContent = 'Storing…';
  const body = { key: putForm.key.value.trim(), value: valueField.value };
  if (putForm.ttl_seconds.value) body.ttl_seconds = Number(putForm.ttl_seconds.value);
  try {
    await api(`/api/admin/storage/${encodeURIComponent(workflow)}`, { method: 'POST', body: JSON.stringify(body) });
    notice(`Stored ${body.key} for ${workflow}.`);
    putForm.key.value = '';
    valueField.value = '';
    putForm.ttl_seconds.value = '';
    autosizeValue();
    await listKeys();
  } catch (error) { notice(error.message, true); }
  finally { submit.disabled = false; submit.textContent = 'Store'; }
});

$('#storage-table').addEventListener('click', async (event) => {
  const value = event.target.closest('.storage-value-text');
  if (value) {
    const expanded = value.closest('.storage-value-cell').classList.toggle('expanded');
    value.title = expanded ? 'Click to collapse' : 'Click to expand';
    return;
  }
  const button = event.target.closest('.storage-delete');
  if (!button || !workflow) return;
  button.disabled = true;
  try {
    await api(`/api/admin/storage/${encodeURIComponent(workflow)}?key=${encodeURIComponent(button.dataset.key)}`, { method: 'DELETE' });
    notice(`Deleted ${button.dataset.key}.`);
    await listKeys();
  } catch (error) {
    notice(error.message, true);
    button.disabled = false;
  }
});

/* Called by the overview render (render()), so the picker fills as soon as
   the workflow list lands and refills on every console refresh. */
export function renderStorage() { fillWorkflowSelect(); }

/* Re-fill the picker and re-list when the view is re-entered, so both stay
   fresh without fetching on every page load. */
document.addEventListener('click', (event) => {
  if (event.target.closest('[data-view="storage"]')) {
    fillWorkflowSelect();
    if (workflow) void listKeys();
  }
});
