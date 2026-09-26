/* API tokens view: operator-issued bearer tokens for agents. */
import { $, $$, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, wrapTokens, formatTimestamp } from '../format.js';
import { refresh } from './overview.js';
import { state } from '../state.js';
import { openAccessGrants } from './connections.js';

let revealedToken = null;

function confirmTokenRevoke(token) {
  const dialog = $('#token-confirm-dialog');
  $('#token-confirm-title').textContent = `Revoke ${token.token_id}?`;
  $('#token-confirm-message').textContent = `The token for agent ${token.agent} will stop authenticating immediately. Its connection grants will remain until you remove them.`;
  dialog.returnValue = '';
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true });
    dialog.showModal();
  });
}

function renderGrantConnections() {
  const select = $('#token-grant-connection');
  const connections = ((state.data || {}).connections || []).filter((connection) => connection.provider !== 'zoom');
  select.innerHTML = connections.map((connection) => `<option value="${escapeHtml(connection.connection_id)}">${escapeHtml(connection.display_name || connection.connection_id)}</option>`).join('');
  select.closest('label').hidden = connections.length === 0;
  $('#token-grant').hidden = connections.length === 0;
  $('#token-reveal-dialog .dialog-body .sub').textContent = connections.length
    ? 'Copy it now — this value is never shown or stored again. Store it in the consumer’s secret store. You can grant connection access without closing this window.'
    : 'Copy it now — this value is never shown or stored again. Add a connection after safely storing the token, then grant that connection access.';
}

function renderTokens(tokens) {
  $('#token-empty').hidden = tokens.length > 0;
  $('.table-wrap', $('[data-page=tokens]')).hidden = tokens.length === 0;
  $('#token-table').innerHTML = tokens.map((token) => `<tr>
      <td class="cell-title"><span class="cell-name mono">${escapeHtml(token.token_id)}</span><span class="cell-sub">${wrapTokens(token.token_prefix || '')}…</span></td>
      <td class="mono muted-cell" data-label="Agent">${escapeHtml(token.agent)}</td>
      <td class="mono muted-cell" data-label="Created">${formatTimestamp(token.created_at) || '—'}</td>
      <td class="mono muted-cell" data-label="Last used">${formatTimestamp(token.last_used_at) || 'never'}</td>
      <td data-label="Status">${token.revoked_at
        ? `<span class="status off"><span class="status-dot" aria-hidden="true"></span>revoked ${formatTimestamp(token.revoked_at) || ''}</span>`
        : statusLine('active')}</td>
      <td class="action-cell">${token.revoked_at ? '' : `<button class="button secondary token-revoke" data-token="${escapeHtml(token.token_id)}" type="button">Revoke</button>`}</td>
    </tr>`).join('');
  $$('.token-revoke').forEach((button) => button.addEventListener('click', async () => {
    const token = tokens.find((item) => item.token_id === button.dataset.token);
    if (!token || !await confirmTokenRevoke(token)) return;
    button.disabled = true;
    button.textContent = 'Revoking…';
    try {
      await api(`/api/admin/tokens?token_id=${encodeURIComponent(button.dataset.token)}`, { method: 'DELETE' });
      notice(`Token ${button.dataset.token} revoked — its grants remain but no longer authenticate`);
      await refresh();
    } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; button.textContent = 'Revoke'; }
  }));
}

$('#token-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  submit.textContent = 'Creating…';
  $('#token-error').textContent = '';
  const agent = form.agent.value.trim();
  try {
    const created = await api('/api/admin/tokens', {
      method: 'PUT',
      body: JSON.stringify({ token_id: form.token_id.value.trim(), agent }),
    });
    form.reset();
    $('#token-dialog').close();
    revealedToken = { token_id: created.token_id, agent: created.agent || agent };
    renderGrantConnections();
    $('#token-reveal-title').textContent = `Token ${created.token_id} created`;
    $('#token-reveal-value').textContent = created.token || '';
    $('#token-reveal-dialog').showModal();
    await refresh();
  } catch (error) { $('#token-error').textContent = error.message; }
  finally { submit.disabled = false; submit.textContent = 'Create token'; }
});

$('#token-grant').addEventListener('click', async () => {
  if (!revealedToken) return;
  const connectionId = $('#token-grant-connection').value;
  if (!connectionId) return;
  const button = $('#token-grant');
  button.disabled = true;
  button.textContent = 'Opening grants…';
  try {
    await openAccessGrants(connectionId, { subject: `token:${revealedToken.token_id}`, agent: revealedToken.agent });
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; button.textContent = 'Grant connection access'; }
});

$('#token-reveal-dialog').addEventListener('close', () => {
  $('#token-reveal-value').textContent = '';
  revealedToken = null;
});

$('#token-copy').addEventListener('click', async () => {
  const value = $('#token-reveal-value').textContent;
  try {
    await navigator.clipboard.writeText(value);
    $('#token-copy').textContent = 'Copied';
    setTimeout(() => { $('#token-copy').textContent = 'Copy'; }, 2000);
  } catch (_) {
    // Clipboard refused (insecure context): fall back to manual selection.
    const range = document.createRange();
    range.selectNodeContents($('#token-reveal-value'));
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }
});

$('#new-token').addEventListener('click', () => {
  const form = $('#token-form');
  form.reset();
  $('#token-error').textContent = '';
  $('#token-dialog').showModal();
  form.token_id.focus();
});

export { renderTokens };
