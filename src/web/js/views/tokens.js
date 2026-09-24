/* API tokens view: operator-issued bearer tokens for agents. */
import { $, $$, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, wrapTokens, formatTimestamp } from '../format.js';
import { refresh } from './overview.js';

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
    try {
      await api(`/api/admin/tokens?token_id=${encodeURIComponent(button.dataset.token)}`, { method: 'DELETE' });
      notice(`Token ${button.dataset.token} revoked — its grants remain but no longer authenticate`);
      await refresh();
    } catch (error) { notice(error.message, true); }
  }));
}

$('#token-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  $('#token-error').textContent = '';
  try {
    const created = await api('/api/admin/tokens', {
      method: 'PUT',
      body: JSON.stringify({ token_id: form.token_id.value.trim(), agent: form.agent.value.trim() }),
    });
    form.reset();
    $('#token-dialog').close();
    $('#token-reveal-title').textContent = `Token ${created.token_id} created`;
    $('#token-reveal-value').textContent = created.token || '';
    $('#token-reveal-dialog').showModal();
    await refresh();
  } catch (error) { $('#token-error').textContent = error.message; }
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
