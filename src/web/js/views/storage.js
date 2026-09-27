/* Data store view: each workflow's key-value state — the store the
   storage_set/get/find/delete steps run on — over /api/admin/storage. */
import { $, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, formatTimestamp } from '../format.js';

let workflow = '';

function renderItems(data) {
  const items = data.items || [];
  $('#storage-empty').hidden = items.length > 0;
  $('#storage-table-wrap').hidden = items.length === 0;
  $('#storage-table').innerHTML = items.map((item) => `<tr>
      <td class="cell-title"><span class="cell-name mono">${escapeHtml(item.key)}</span></td>
      <td class="mono muted-cell" data-label="Value">${escapeHtml(item.value)}</td>
      <td class="mono muted-cell" data-label="Updated">${formatTimestamp(item.updated_at) || '—'}</td>
      <td class="mono muted-cell" data-label="Expires">${item.expires
        ? formatTimestamp(new Date(item.expires * 1000).toISOString()) || item.expires : '—'}</td>
      <td class="action-cell"><button class="button secondary storage-delete" data-key="${escapeHtml(item.key)}" type="button">Delete</button></td>
    </tr>`).join('');
}

async function listKeys() {
  const prefix = $('#storage-browse').prefix.value.trim();
  const query = prefix ? `?prefix=${encodeURIComponent(prefix)}` : '';
  try {
    renderItems(await api(`/api/admin/storage/${encodeURIComponent(workflow)}${query}`));
  } catch (error) { notice(error.message, true); }
}

$('#storage-browse').addEventListener('submit', (event) => {
  event.preventDefault();
  workflow = event.currentTarget.workflow.value.trim();
  if (workflow) void listKeys();
});

$('#storage-put').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  if (!workflow) return notice('Enter a workflow ID above first.', true);
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  submit.textContent = 'Storing…';
  const body = { key: form.key.value.trim(), value: form.value.value };
  if (form.ttl_seconds.value) body.ttl_seconds = Number(form.ttl_seconds.value);
  try {
    await api(`/api/admin/storage/${encodeURIComponent(workflow)}`, { method: 'POST', body: JSON.stringify(body) });
    notice(`Stored ${body.key} for ${workflow}.`);
    form.key.value = '';
    form.value.value = '';
    form.ttl_seconds.value = '';
    await listKeys();
  } catch (error) { notice(error.message, true); }
  finally { submit.disabled = false; submit.textContent = 'Store'; }
});

$('#storage-table').addEventListener('click', async (event) => {
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

/* Re-list when the view is re-entered, so the table stays fresh without
   fetching on every page load. */
document.addEventListener('click', (event) => {
  if (event.target.closest('.nav-item[data-view="storage"]') && workflow) void listKeys();
});
