/* OAuth clients view: shared per-provider client configuration. */
import { $, $$, notice, rowActions } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine } from '../format.js';
import { refresh } from './overview.js';

function renderOAuthClients(clients) {
  const names = { google: 'Google (also YouTube)', dropbox: 'Dropbox' };
  const list = clients || [];
  const empty = $('#oauth-client-empty');
  const wrap = $('#oauth-client-wrap');
  if (empty) empty.hidden = list.length > 0;
  if (wrap) wrap.hidden = list.length === 0;
  const sources = { config: 'Saved in Dapier', deploy: 'Deployment setting', none: '' };
  $('#oauth-client-list').innerHTML = list.map((client) => {
    const name = names[client.provider] || escapeHtml(client.provider.charAt(0).toUpperCase() + client.provider.slice(1));
    const source = String(client.source || '');
    return `<tr>
      <td class="cell-title"><span class="cell-name">${name}</span></td>
      <td class="muted-cell" data-label="Client ID"${client.client_id ? '' : ' data-empty'}>${client.client_id
        ? `<span class="mono clip" title="${escapeHtml(client.client_id)}">${escapeHtml(client.client_id)}</span>` : '—'}</td>
      <td class="muted-cell" data-label="Source"${sources[source] === '' || !source ? ' data-empty' : ''} title="${escapeHtml(source)}">${escapeHtml((source in sources ? sources[source] : source) || '—')}</td>
      <td data-label="Status">${statusLine(client.configured ? 'configured' : 'missing')}</td>
      <td class="action-cell">${rowActions(`<button class="dk-button dk-button--secondary dk-button--sm oauth-client-edit" data-provider="${escapeHtml(client.provider)}" type="button">${client.configured ? 'Replace' : 'Set up'}</button>`, [], { id: '', label: name })}</td>
    </tr>`;
  }).join('');
  $$('.oauth-client-edit').forEach((button) => button.addEventListener('click', () => openOAuthClient(button.dataset.provider)));
}

function openOAuthClient(provider) {
  const form = $('#oauth-client-form');
  form.reset();
  form.dataset.provider = provider;
  $('#oauth-client-title').textContent = provider === 'google' ? 'Google OAuth client' : 'Dropbox OAuth client';
  $('#oauth-client-error').textContent = '';
  $('#oauth-client-dialog').showModal();
  form.client_id.focus();
}

$('#oauth-client-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  submit.textContent = 'Saving…';
  $('#oauth-client-error').textContent = '';
  try {
    await api(`/api/admin/oauth-clients/${form.dataset.provider}`, {
      method: 'PUT',
      body: JSON.stringify({ client_id: form.client_id.value, client_secret: form.client_secret.value }),
    });
    form.client_secret.value = '';
    $('#oauth-client-dialog').close();
    notice('OAuth client saved — Connect buttons are live');
    await refresh();
  } catch (error) { $('#oauth-client-error').textContent = error.message; }
  finally { submit.disabled = false; submit.textContent = 'Save client'; }
});

export { renderOAuthClients };
