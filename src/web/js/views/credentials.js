/* Credentials view: provider secrets used by workflow actions. */
import { $, $$, notice, rowActions } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp } from '../format.js';
import { refresh } from './overview.js';

/* The fields each provider stores; the API validates the values. */
const PROVIDERS = {
  slack: { name: 'Slack bot', fields: [{ key: 'token', label: 'Bot token' }] },
  mailchimp: { name: 'Mailchimp', fields: [{ key: 'api_key', label: 'API key' }] },
  aws: { name: 'AWS', fields: [
    { key: 'access_key_id', label: 'Access key ID' },
    { key: 'secret_access_key', label: 'Secret access key' },
  ] },
};

/* Credential-backed providers with a health check (pseudo connections). */
const TESTABLE = new Set(['mailchimp', 'aws']);

function providerName(provider) {
  if (PROVIDERS[provider]) return PROVIDERS[provider].name;
  const known = { zoom: 'Zoom', telegram: 'Telegram bot', github: 'GitHub', openai: 'OpenAI' };
  const value = String(provider || '');
  return known[value] || value.charAt(0).toUpperCase() + value.slice(1);
}

function renderCredentials(credentials) {
  $('#credential-list').innerHTML = credentials.map((credential) => {
    const name = providerName(credential.provider);
    /* Service credentials are optional: an unset one is neutral "Not set
       up", never a danger state. */
    const status = credential.configured ? 'configured' : 'missing';
    const provider = escapeHtml(credential.provider);
    const edit = `<button class="dk-button dk-button--secondary dk-button--sm credential-edit" data-provider="${provider}" type="button">${credential.configured ? 'Replace' : 'Add'}</button>`;
    /* Test stays reachable whether or not a value is stored (it reports
       what is missing); it sits behind More beside the one visible action. */
    const test = TESTABLE.has(credential.provider)
      ? `<button class="row-menu-item credential-test" data-provider="${provider}" type="button">Test connection</button>`
      : '';
    return `<tr>
      <td class="cell-title"><span class="cell-name">${escapeHtml(name)}</span></td>
      <td class="mono muted-cell nowrap" data-label="Updated"${credential.updated_at ? '' : ' data-empty'}>${credential.updated_at ? formatTimestamp(credential.updated_at) : '—'}</td>
      <td data-empty></td>
      <td data-label="Status">${statusLine(status)}</td>
      <td class="action-cell">${rowActions(edit, [test], { id: `credential-menu-${provider}`, label: escapeHtml(name) })}</td>
    </tr>`;
  }).join('');
  $$('.credential-edit').forEach((button) => button.addEventListener('click', () => openCredential(button.dataset.provider)));
  $$('.credential-test').forEach((button) => button.addEventListener('click', () => testCredential(button)));
}

async function testCredential(button) {
  const provider = button.dataset.provider;
  button.disabled = true;
  try {
    const verdict = await api(`/api/admin/connections/${provider}/test`, { method: 'POST', body: '{}' });
    notice(`${verdict.ok ? 'OK' : 'Failed'} — ${verdict.detail || (verdict.ok ? 'connection OK' : 'check failed')}`, !verdict.ok);
  } catch (error) { notice(`Test failed: ${error.message}`); }
  finally { button.disabled = false; }
}

function openCredential(provider) {
  const form = $('#credential-form');
  form.reset();
  form.provider.value = provider;
  const spec = PROVIDERS[provider] || { name: provider, fields: [{ key: 'value', label: 'Value' }] };
  $('#credential-title').textContent = `${spec.name} credential`;
  const fields = $('#credential-fields');
  const renderFields = (mode) => {
    const selected = provider === 'aws' && mode === 'role' ? [
      { key: 'role_arn', label: 'IAM role ARN', public: true },
      { key: 'external_id', label: 'External ID (optional)', optional: true },
      { key: 'region', label: 'Region (optional)', public: true, optional: true },
      { key: 'buckets', label: 'Bucket names, comma-separated (optional)', public: true, optional: true },
    ] : spec.fields;
    const target = provider === 'aws' ? $('#aws-credential-fields') : fields;
    target.innerHTML = selected.map((field) => `
      <label>${escapeHtml(field.label)}<input name="${escapeHtml(field.key)}" class="${field.public ? '' : 'secret-input'}" type="${field.public ? 'text' : 'password'}" autocomplete="new-password" ${field.optional ? '' : 'required'}></label>
    `).join('');
  };
  if (provider === 'aws') {
    fields.innerHTML = '<label>Authentication<select id="aws-credential-mode"><option value="role">Assume IAM role</option><option value="keys">Access keys</option></select></label><div id="aws-credential-fields"></div>';
    $('#aws-credential-mode').addEventListener('change', (event) => renderFields(event.target.value));
  }
  renderFields('role');
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
  $$('#credential-fields input').forEach((input) => {
    if (input.name === 'buckets') {
      if (input.value.trim()) body.buckets = input.value.split(',').map((name) => name.trim()).filter(Boolean);
    } else if (input.value.trim()) body[input.name] = input.value;
  });
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
