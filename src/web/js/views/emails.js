/* Emails is an inventory of workflow entry points. Flows are edited only in
   the workflow designer; the legacy action editor has been retired. */
import { $, $$, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp } from '../format.js';
import { refresh } from './overview.js';
import { openDesigner } from './designer.js';

function handlerDetails(handler) {
  const name = escapeHtml(handler.workflow);
  const actions = (handler.action_types || []).map(escapeHtml).join(' → ') || 'No actions';
  const pending = handler.has_draft && handler.status !== 'draft' ? ' · unpublished changes' : '';
  const manage = handler.legacy
    ? `<button class="dk-button dk-button--secondary email-migrate" data-name="${escapeHtml(handler.legacy_name)}" type="button">Convert to workflow</button>`
    : `<button class="dk-button dk-button--secondary email-workflow" data-workflow="${name}" type="button">Edit workflow</button>`;
  return { name, actions, pending, manage };
}

function row(title, handler) {
  const { name, actions, pending, manage } = handlerDetails(handler);
  return `<tr>
    <td class="cell-title"><span class="cell-name mono">${escapeHtml(title)}</span><span class="cell-sub">${escapeHtml(handler.description || '')}</span></td>
    <td class="cell-title" data-label="Workflow"><span class="cell-name">${name}</span><span class="cell-sub">${actions}</span></td>
    <td data-label="Status">${statusLine(handler.status)}<span class="cell-sub">${pending}</span></td>
    <td class="mono muted-cell" data-label="Updated">${formatTimestamp(handler.updated_at) || '—'}</td>
    <td class="action-cell" data-label="Manage">${manage}</td>
  </tr>`;
}

export function renderEmails(data) {
  const config = data || {};
  $('#email-domain-hint').textContent = config.domain || '';
  $('#email-load-error').textContent = config.error || '';
  const addresses = config.addresses || [];
  $('#email-empty').hidden = addresses.length > 0 || Boolean(config.error);
  $('#email-addresses').hidden = addresses.length === 0;
  // One address row, with every workflow handler and its actions visible.
  $('#email-table').innerHTML = addresses.map((address) => {
    const handlers = address.handlers || [];
    return `<tr>
      <td class="cell-title"><span class="cell-name mono">${escapeHtml(address.address)}</span>${handlers.length > 1 ? '<span class="cell-sub">Multiple workflows receive this address</span>' : ''}</td>
      <td data-label="Workflow">${handlers.map((h) => { const d = handlerDetails(h); return `<div class="cell-title"><span class="cell-name">${d.name}</span><span class="cell-sub">${d.actions}</span></div>`; }).join('')}</td>
      <td data-label="Status">${handlers.map((h) => `<div class="cell-title">${statusLine(h.status)}${h.has_draft && h.status !== 'draft' ? '<span class="cell-sub">Unpublished changes</span>' : ''}</div>`).join('')}</td>
      <td class="mono muted-cell" data-label="Updated">${handlers.map((h) => `<div>${formatTimestamp(h.updated_at) || '—'}</div>`).join('')}</td>
      <td class="action-cell" data-label="Manage">${handlers.map((h) => handlerDetails(h).manage).join(' ')}</td>
    </tr>`;
  }).join('');
  const subscriptions = config.subscriptions || [];
  $('#email-subscriptions').hidden = subscriptions.length === 0;
  $('#email-subscription-table').innerHTML = subscriptions.map((h) => row(
    JSON.stringify(h.filters.route || 'All incoming addresses'), h)).join('');
  const watchers = config.watchers || [];
  $('#email-watchers').hidden = watchers.length === 0;
  $('#email-watcher-table').innerHTML = watchers.map((h) => row(h.event, h)).join('');
  $$('.email-workflow').forEach((button) => button.addEventListener('click', () => openDesigner(button.dataset.workflow)));
  $$('.email-migrate').forEach((button) => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const result = await api('/api/admin/email-triggers/migrate', { method: 'POST', body: JSON.stringify({ name: button.dataset.name }) });
      notice('Converted to a workflow. The same address and actions remain live.');
      await refresh();
      await openDesigner(result.workflow);
    } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  }));
}

export function renderEmailFrom(addresses) {
  const list = $('#email-from-list');
  const items = addresses || [];
  list.innerHTML = items.map((address) => `<li class="sender-chip">
    <span class="mono">${escapeHtml(address)}</span>
    <button class="sender-remove" type="button" data-address="${escapeHtml(address)}" aria-label="Remove ${escapeHtml(address)}">&times;</button>
  </li>`).join('') || '<li class="sender-empty">No senders — every incoming message is ignored until one is added above.</li>';
  $$('.sender-remove').forEach((button) => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await api(`/api/admin/email-from?address=${encodeURIComponent(button.dataset.address)}`, { method: 'DELETE' });
      await refresh();
    } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  }));
}

$('#email-from-add').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const address = form.address.value.trim();
  if (!address) return;
  try {
    await api('/api/admin/email-from', { method: 'POST', body: JSON.stringify({ address }) });
    form.reset();
    await refresh();
  } catch (error) { notice(error.message, true); }
});

$('#new-email').addEventListener('click', () => openDesigner(null));
