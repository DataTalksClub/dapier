/* OAuth clients view: shared per-provider client configuration. */
import { $, $$, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, wrapTokens } from '../format.js';
import { refresh } from './overview.js';

function renderOAuthClients(clients) {
  const names = { google: 'Google (also YouTube)', dropbox: 'Dropbox' };
  $('#oauth-client-list').innerHTML = clients.map((client) => `<tr>
      <td class="cell-title"><span class="cell-name">${names[client.provider] || escapeHtml(client.provider)}</span><span class="cell-sub">oauth client: ${escapeHtml(client.provider)}</span></td>
      <td class="mono muted-cell" data-label="Client ID">${client.client_id ? wrapTokens(client.client_id) : '—'}</td>
      <td class="mono muted-cell" data-label="Source">${escapeHtml(client.source)}</td>
      <td data-label="Status">${statusLine(client.configured ? 'configured' : 'missing')}</td>
      <td class="action-cell"><button class="button secondary oauth-client-edit" data-provider="${escapeHtml(client.provider)}" type="button">${client.configured ? 'Replace' : 'Set up'}</button></td>
    </tr>`).join('');
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
