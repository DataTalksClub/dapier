/* Credentials view: provider secrets used by workflow actions. */
import { $, $$, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp } from '../format.js';
import { refresh } from './overview.js';

/* The fields each provider stores; the API validates the values. */
const PROVIDERS = {
  slack: { name: 'Slack bot', fields: [{ key: 'token', label: 'Bot token' }] },
  mailchimp: { name: 'Mailchimp', fields: [{ key: 'api_key', label: 'API key' }] },
  aws: { name: 'AWS keys', fields: [
    { key: 'access_key_id', label: 'Access key ID' },
    { key: 'secret_access_key', label: 'Secret access key' },
  ] },
};

function renderCredentials(credentials) {
  $('#credential-list').innerHTML = credentials.map((credential) => {
    const item = PROVIDERS[credential.provider] || { name: credential.provider };
    const status = credential.configured ? 'configured' : 'missing';
    return `<tr>
      <td class="cell-title"><span class="cell-name">${escapeHtml(item.name)}</span><span class="cell-sub">credential: ${escapeHtml(credential.provider)}</span></td>
      <td class="mono muted-cell" data-label="Updated">${credential.updated_at ? formatTimestamp(credential.updated_at) : '—'}</td>
      <td data-label="Status">${statusLine(status)}</td>
      <td class="action-cell"><button class="button secondary credential-edit" data-provider="${escapeHtml(credential.provider)}">${credential.configured ? 'Replace' : 'Add'}</button></td>
    </tr>`;
  }).join('');
  $$('.credential-edit').forEach((button) => button.addEventListener('click', () => openCredential(button.dataset.provider)));
}

function openCredential(provider) {
  const form = $('#credential-form');
  form.reset();
  form.provider.value = provider;
  const spec = PROVIDERS[provider] || { name: provider, fields: [{ key: 'value', label: 'Value' }] };
  $('#credential-title').textContent = `${spec.name} credential`;
  $('#credential-fields').innerHTML = spec.fields.map((field) => `
    <label>${escapeHtml(field.label)}<input name="${escapeHtml(field.key)}" class="secret-input" type="password" autocomplete="new-password" required></label>
  `).join('');
  $('#credential-error').textContent = '';
  $('#credential-dialog').showModal();
  const first = $('#credential-fields input');
  if (first) first.focus();
}

$('#credential-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  submit.textContent = 'Saving…';
  const provider = form.provider.value;
  const body = {};
  $$('#credential-fields input').forEach((input) => { body[input.name] = input.value; });
  $('#credential-error').textContent = '';
  try {
    await api(`/api/admin/credentials/${provider}`, { method: 'PUT', body: JSON.stringify(body) });
    $$('#credential-fields input').forEach((input) => { input.value = ''; });
    $('#credential-dialog').close();
    notice('Credential saved');
    await refresh();
  } catch (error) { $('#credential-error').textContent = error.message; }
  finally { submit.disabled = false; submit.textContent = 'Save credential'; }
});

export { renderCredentials };
