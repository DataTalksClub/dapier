/* Agent mail: one address that starts an Aplexer session, plus the shared sender list. */
import { $, $$, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp } from '../format.js';
import { refresh } from './overview.js';

const OPERATORS = ['equals', 'contains', 'prefix', 'suffix', 'not_equals', 'does_not_contain'];

function mailboxOf(data) {
  return (data && data.mailbox) || null;
}

function bindListButtons() {
  $$('.agent-from-remove').forEach((button) => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await api(`/api/admin/agent-mail/from?address=${encodeURIComponent(button.dataset.address)}`, { method: 'DELETE' });
      await refresh();
    } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  }));
  $$('.agent-rule-delete').forEach((button) => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await api(`/api/admin/agent-mailboxes/agent/rules?id=${encodeURIComponent(button.dataset.id)}`, { method: 'DELETE' });
      await refresh();
    } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  }));
}

export function renderAgentMail(data) {
  const mailbox = mailboxOf(data);
  const addresses = (data && data.addresses) || [];
  const tasks = (data && data.tasks) || [];
  const empty = $('#agent-mail-empty');
  const body = $('#agent-mail-body');
  if (!empty || !body) return;
  empty.hidden = Boolean(mailbox);
  body.hidden = !mailbox;
  if (!mailbox) return;
  const form = $('#agent-mail-settings');
  form.engine.value = mailbox.engine || '';
  form.workspace.value = mailbox.workspace || '';
  form.instructions.value = mailbox.instructions || '';
  form.enabled.checked = Boolean(mailbox.enabled);
  $('#agent-mail-address').textContent = mailbox.address || '';
  $('#agent-mail-from-list').innerHTML = addresses.map((address) => `<li>
      <span class="mono">${escapeHtml(address)}</span>
      <button class="button secondary agent-from-remove" type="button" data-address="${escapeHtml(address)}">Remove</button>
    </li>`).join('') || '<li class="muted-cell">No senders. Every message is ignored.</li>';
  const rules = mailbox.rules || [];
  $('#agent-mail-rules').innerHTML = rules.map((rule) => `<li>
      <span class="mono">${escapeHtml(rule.field)} ${escapeHtml(rule.operator)} ${escapeHtml(String(rule.value))}</span>
      <button class="button secondary agent-rule-delete" type="button" data-id="${escapeHtml(rule.id)}">Delete</button>
    </li>`).join('') || '<li class="muted-cell">No extra rules.</li>';
  $('#agent-mail-tasks').innerHTML = tasks.map((task) => `<tr>
      <td data-label="Status">${statusLine(task.status || 'starting')}</td>
      <td class="mono" data-label="From">${escapeHtml(task.from || '')}</td>
      <td data-label="Subject">${escapeHtml(task.subject || '')}</td>
      <td class="mono muted-cell" data-label="Tag">${escapeHtml(task.tag || '—')}</td>
      <td class="mono muted-cell" data-label="When">${formatTimestamp(task.created_at) || '—'}</td>
    </tr>`).join('');
  $('#agent-mail-tasks-empty').hidden = tasks.length > 0;
  bindListButtons();
}

$('#agent-mail-settings').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  try {
    await api('/api/admin/agent-mailboxes', {
      method: 'PUT',
      body: JSON.stringify({
        engine: form.engine.value.trim(),
        workspace: form.workspace.value.trim(),
        instructions: form.instructions.value,
        enabled: form.enabled.checked,
      }),
    });
    notice('Agent mailbox saved');
    await refresh();
  } catch (error) { notice(error.message, true); }
  finally { submit.disabled = false; }
});

$('#agent-mail-from-add').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const address = form.address.value.trim();
  if (!address) return;
  try {
    await api('/api/admin/agent-mail/from', { method: 'POST', body: JSON.stringify({ address }) });
    form.reset();
    await refresh();
  } catch (error) { notice(error.message, true); }
});

$('#agent-mail-rule-add').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api('/api/admin/agent-mailboxes/agent/rules', {
      method: 'POST',
      body: JSON.stringify({
        field: form.field.value,
        operator: form.operator.value,
        value: form.value.value,
      }),
    });
    form.value.value = '';
    await refresh();
  } catch (error) { notice(error.message, true); }
});

export const AGENT_MAIL_OPERATORS = OPERATORS;
