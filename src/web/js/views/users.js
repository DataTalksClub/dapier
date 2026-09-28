/* Users view: who may use the console, and at which role — over
   /api/admin/users. Renders state and calls endpoints; the role rules
   (last-admin protection, disabled handling) live server-side. */
import { $, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp } from '../format.js';

function renderUsers(users) {
  $('#user-empty').hidden = users.length > 0;
  $('.table-wrap', $('[data-page=users]')).hidden = users.length === 0;
  $('#user-table').innerHTML = users.map((user) => `<tr>
      <td class="cell-title"><span class="cell-name mono">${escapeHtml(user.subject)}</span><span class="cell-sub">updated ${formatTimestamp(user.updated_at) || '—'} by ${escapeHtml(user.updated_by || '—')}</span></td>
      <td class="mono muted-cell" data-label="Role">${escapeHtml(user.role)}</td>
      <td class="muted-cell" data-label="Display name">${escapeHtml(user.display_name || '—')}</td>
      <td data-label="Status">${user.disabled ? statusLine('disabled') : statusLine('active')}</td>
      <td class="action-cell">
        <button class="button secondary user-edit" data-subject="${escapeHtml(user.subject)}" type="button">Set role</button>
        <button class="button secondary user-remove" data-subject="${escapeHtml(user.subject)}" data-role="${escapeHtml(user.role)}" type="button">Remove</button>
      </td>
    </tr>`).join('');
}

export async function refreshUsers() {
  try {
    renderUsers((await api('/api/admin/users')).users || []);
  } catch (error) { notice(error.message, true); }
}

function openRoleDialog(user = {}) {
  const form = $('#user-role-form');
  form.reset();
  $('#user-role-error').textContent = '';
  $('#user-role-title').textContent = user.subject ? `Set role — ${user.subject}` : 'Set role';
  form.subject.value = user.subject || '';
  form.subject.readOnly = Boolean(user.subject);
  form.role.value = user.role || 'viewer';
  form.display_name.value = user.display_name || '';
  form.disabled.checked = Boolean(user.disabled);
  $('#user-role-dialog').showModal();
  form.subject.focus();
}

$('#new-user').addEventListener('click', () => openRoleDialog());

$('#user-table').addEventListener('click', (event) => {
  const edit = event.target.closest('.user-edit');
  if (edit) {
    const cells = edit.closest('tr').querySelectorAll('td');
    openRoleDialog({
      subject: edit.dataset.subject,
      role: ((cells[1] || {}).textContent || 'viewer').trim(),
      display_name: ((cells[2] || {}).textContent || '').trim(),
      disabled: ((cells[3] || {}).textContent || '').includes('disabled'),
    });
    return;
  }
  const remove = event.target.closest('.user-remove');
  if (!remove) return;
  const dialog = $('#user-confirm-dialog');
  $('#user-confirm-title').textContent = `Remove ${remove.dataset.subject}?`;
  $('#user-confirm-message').textContent = `The ${remove.dataset.role} role is deleted from the store; the operator allowlist decides this account's access again. Removing the last admin is refused by the API.`;
  dialog.returnValue = '';
  dialog.showModal();
  dialog.addEventListener('close', async () => {
    if (dialog.returnValue !== 'confirm') return;
    try {
      await api(`/api/admin/users/${encodeURIComponent(remove.dataset.subject)}`, { method: 'DELETE' });
      notice(`Removed ${remove.dataset.subject}.`);
      await refreshUsers();
    } catch (error) { notice(error.message, true); }
  }, { once: true });
});

$('#user-role-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type=submit]');
  if (submit.disabled) return;
  submit.disabled = true;
  submit.textContent = 'Saving…';
  $('#user-role-error').textContent = '';
  const subject = form.subject.value.trim();
  const body = { subject, role: form.role.value };
  if (form.display_name.value.trim()) body.display_name = form.display_name.value.trim();
  body.disabled = form.disabled.checked;
  try {
    await api('/api/admin/users', { method: 'POST', body: JSON.stringify(body) });
    $('#user-role-dialog').close();
    notice(`Saved ${subject}.`);
    await refreshUsers();
  } catch (error) { $('#user-role-error').textContent = error.message; }
  finally {
    submit.disabled = false;
    submit.textContent = 'Save role';
  }
});

/* Re-list when the view is re-entered, so the table stays fresh without
   fetching on every page load. */
document.addEventListener('click', (event) => {
  if (event.target.closest('.nav-item[data-view="users"]')) void refreshUsers();
});
