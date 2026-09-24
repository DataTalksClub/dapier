/* Credentials view: provider secrets used by workflow actions. */
import { $, $$, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp } from '../format.js';
import { refresh } from './overview.js';

function renderCredentials(credentials) {
  const labels = {
    slack: { name: 'Slack bot', secret: 'credential: slack' },
    mailchimp: { name: 'Mailchimp', secret: 'credential: mailchimp' },
  };
  $('#credential-list').innerHTML = credentials.map((credential) => {
    const item = labels[credential.provider];
    const status = credential.configured ? 'configured' : 'missing';
    return `<tr>
      <td class="cell-title"><span class="cell-name">${item.name}</span><span class="cell-sub">${item.secret}</span></td>
      <td class="mono muted-cell" data-label="Updated">${credential.updated_at ? formatTimestamp(credential.updated_at) : '—'}</td>
      <td data-label="Status">${statusLine(status)}</td>
      <td class="action-cell"><button class="button secondary credential-edit" data-provider="${credential.provider}">${credential.configured ? 'Replace' : 'Add'}</button></td>
    </tr>`;
  }).join('');
  $$('.credential-edit').forEach((button) => button.addEventListener('click', () => openCredential(button.dataset.provider)));
}

function openCredential(provider) {
  const form = $('#credential-form');
  form.reset();
  form.provider.value = provider;
  $('#credential-title').textContent = provider === 'slack' ? 'Slack bot token' : 'Mailchimp API key';
  $('#credential-field-label').firstChild.textContent = provider === 'slack' ? 'Bot token' : 'API key';
  $('#credential-error').textContent = '';
  $('#credential-dialog').showModal();
  form.value.focus();
}

$('#credential-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const provider = form.provider.value;
  const field = provider === 'slack' ? 'token' : 'api_key';
  $('#credential-error').textContent = '';
  try {
    await api(`/api/admin/credentials/${provider}`, { method: 'PUT', body: JSON.stringify({ [field]: form.value.value }) });
    form.value.value = '';
    $('#credential-dialog').close();
    notice('Credential saved');
    await refresh();
  } catch (error) { $('#credential-error').textContent = error.message; }
});

export { renderCredentials };
