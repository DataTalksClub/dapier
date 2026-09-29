/* Emails view: app-managed email triggers — reserve an address, bind actions. */
import { $, $$, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp, wrapTokens } from '../format.js';
import { refresh } from './overview.js';

const ACTION_TEMPLATE = JSON.stringify([
  { type: 'dropbox_upload', connection_id: 'dropbox', source: 'attachment', folder: 'email-attachments' },
], null, 2);

/* The payload the row buttons act on; refreshed with every render. */
let current = { domain: '', triggers: [] };
let editing = null; // trigger being edited, or null when creating

function addressOf(trigger) {
  if (trigger.event && trigger.event !== 'message.received') return `${trigger.name} · ${trigger.event}`;
  return trigger.address || `${trigger.name}@${current.domain}`;
}

function actionSummary(trigger) {
  if (trigger.flow) return `flow: ${trigger.flow}`;
  return (trigger.actions || []).map((action) => action.type).join(' → ') || '—';
}

function openDialog(trigger) {
  editing = trigger || null;
  const form = $('#email-form');
  form.reset();
  $('#email-error').textContent = '';
  $('#email-dialog-title').textContent = trigger ? `Edit ${addressOf(trigger)}` : 'New email';
  form.event.value = (trigger && trigger.event) || 'message.received';
  syncEventFields(trigger);
  form.name.value = trigger ? trigger.name : '';
  form.name.disabled = Boolean(trigger); // renaming would orphan the address
  form.description.value = trigger ? (trigger.description || '') : '';
  form.enabled.checked = trigger ? Boolean(trigger.enabled) : true;
  form.actions.value = trigger && trigger.actions
    ? JSON.stringify(trigger.actions, null, 2)
    : ACTION_TEMPLATE;
  form.actions.disabled = Boolean(trigger && trigger.flow);
  $('#email-dialog').showModal();
  if (!trigger) form.name.focus();
}

/* Address triggers reserve <name>@domain; SES feedback watchers match
   domain-wide and scope through a filters object instead. */
function syncEventFields(trigger) {
  const form = $('#email-form');
  const watcher = form.event.value !== 'message.received';
  $('#email-address-field').hidden = watcher;
  $('#email-filters-field').hidden = !watcher;
  // A hidden required input would block submit with an unfocusable error.
  form.name.required = !watcher;
  form.filters.value = watcher && trigger && trigger.filters && Object.keys(trigger.filters).length
    ? JSON.stringify(trigger.filters, null, 2)
    : '';
}

$('#email-form').elements.event.addEventListener('change', () => syncEventFields(editing));

function bindRowButtons() {
  $$('.email-edit').forEach((button) => button.addEventListener('click', () => {
    const trigger = current.triggers.find((item) => item.name === button.dataset.name);
    if (trigger) openDialog(trigger);
  }));
  $$('.email-toggle').forEach((button) => button.addEventListener('click', async () => {
    const trigger = current.triggers.find((item) => item.name === button.dataset.name);
    if (!trigger || button.disabled) return;
    button.disabled = true;
    try {
      const body = {
        name: trigger.name,
        description: trigger.description || '',
        enabled: !trigger.enabled,
      };
      if (trigger.event && trigger.event !== 'message.received') {
        body.event = trigger.event; // api_save replaces the item: carry watcher fields
        body.filters = trigger.filters || {};
      }
      if (trigger.flow) body.flow = trigger.flow;
      else body.actions = trigger.actions || [];
      await api('/api/admin/email-triggers', { method: 'PUT', body: JSON.stringify(body) });
      await refresh();
    } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  }));
  $$('.email-delete').forEach((button) => button.addEventListener('click', async () => {
    const trigger = current.triggers.find((item) => item.name === button.dataset.name);
    if (!trigger) return;
    const dialog = $('#email-confirm-dialog');
    const watcher = Boolean(trigger.event && trigger.event !== 'message.received');
    $('#email-confirm-title').textContent = `Delete ${addressOf(trigger)}?`;
    $('#email-confirm-message').textContent = watcher
      ? 'This watcher stops matching SES feedback and its actions stop running.'
      : 'Mail to this address will no longer run any actions. The address becomes free to reserve again.';
    dialog.returnValue = '';
    dialog.showModal();
    const confirmed = await new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true }));
    if (!confirmed) return;
    button.disabled = true;
    try {
      await api(`/api/admin/email-triggers?name=${encodeURIComponent(trigger.name)}`, { method: 'DELETE' });
      notice(`Email ${addressOf(trigger)} deleted`);
      await refresh();
    } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  }));
}

export function renderEmailFrom(addresses) {
  const list = $('#email-from-list');
  if (!list) return;
  const items = addresses || [];
  list.innerHTML = items.map((address) => `<li>
      <span class="mono">${escapeHtml(address)}</span>
      <button class="button secondary email-from-remove" type="button" data-address="${escapeHtml(address)}">Remove</button>
    </li>`).join('') || '<li class="muted-cell">No senders. Every message is ignored.</li>';
  $$('.email-from-remove').forEach((button) => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await api(`/api/admin/email-from?address=${encodeURIComponent(button.dataset.address)}`, { method: 'DELETE' });
      await refresh();
    } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  }));
}

$('#email-from-add')?.addEventListener('submit', async (event) => {
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

/* Agent tasks: the host jobs the `agent` action enqueued, as `dapier worker`
   leaves them. Not part of the overview payload — the view fetches
   /api/admin/agent-tasks itself (like the Runs view) and Refresh re-reads it. */
let agentTasksSeq = 0;

export async function renderAgentTasks() {
  const empty = $('#agent-tasks-empty');
  const wrap = $('#agent-tasks-wrap');
  const body = $('#agent-tasks');
  if (!empty || !wrap || !body) return;
  const seq = ++agentTasksSeq;
  let tasks = [];
  try {
    const data = await api('/api/admin/agent-tasks?limit=10');
    if (seq !== agentTasksSeq) return; // a newer fetch superseded this one
    tasks = data.tasks || [];
  } catch (error) {
    if (seq !== agentTasksSeq) return;
    empty.hidden = false;
    empty.textContent = 'Agent tasks are unavailable.';
    wrap.hidden = true;
    return;
  }
  empty.hidden = tasks.length > 0;
  empty.textContent = 'No tasks yet. An agent action enqueues one when its workflow runs.';
  wrap.hidden = tasks.length === 0;
  body.innerHTML = tasks.map((task) => `<tr>
      <td class="cell-title mono"><span class="cell-name">${escapeHtml(wrapTokens(task.task_id || ''))}</span><span class="cell-sub">${escapeHtml(task.engine || '')}</span></td>
      <td class="mono muted-cell" data-label="Workflow">${escapeHtml(task.workflow || '—')}</td>
      <td data-label="Status">${statusLine(task.status)}</td>
      <td class="mono muted-cell" data-label="Result">${escapeHtml(task.summary || task.error || '—')}</td>
      <td class="mono muted-cell" data-label="Updated">${escapeHtml(formatTimestamp(task.finished_at || task.started_at || task.created_at) || '—')}</td>
    </tr>`).join('');
}

$('#agent-tasks-refresh')?.addEventListener('click', () => renderAgentTasks());

export function renderEmails(data) {
  current = {
    domain: (data && data.domain) || '',
    triggers: (data && data.triggers) || [],
  };
  $('#email-domain-hint').textContent = current.domain;
  $('#email-empty').hidden = current.triggers.length > 0;
  $('.table-wrap', $('[data-page=emails]')).hidden = current.triggers.length === 0;
  $('#email-table').innerHTML = current.triggers.map((trigger) => `<tr>
      <td class="cell-title"><span class="cell-name mono">${escapeHtml(addressOf(trigger))}</span><span class="cell-sub">${escapeHtml(trigger.description || '')}</span></td>
      <td class="mono muted-cell" data-label="Actions">${escapeHtml(actionSummary(trigger))}</td>
      <td data-label="Status">${statusLine(trigger.enabled ? 'enabled' : 'disabled')}</td>
      <td class="mono muted-cell" data-label="Updated">${formatTimestamp(trigger.updated_at) || '—'}</td>
      <td class="action-cell">
        <button class="button secondary email-edit" data-name="${escapeHtml(trigger.name)}" type="button">Edit</button>
        <button class="button secondary email-toggle" data-name="${escapeHtml(trigger.name)}" type="button">${trigger.enabled ? 'Disable' : 'Enable'}</button>
        <button class="button secondary email-delete" data-name="${escapeHtml(trigger.name)}" type="button">Delete</button>
      </td>
    </tr>`).join('');
  const yamlRoutes = (data && data.yaml_routes) || [];
  $('#email-yaml-routes').hidden = yamlRoutes.length === 0;
  $('#email-yaml-list').textContent = yamlRoutes.map((route) => `${route}@${current.domain}`).join(', ');
  bindRowButtons();
}

$('#new-email').addEventListener('click', () => openDialog(null));

$('#email-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  $('#email-error').textContent = '';
  try {
    const body = {
      name: form.name.value.trim(),
      description: form.description.value.trim(),
      enabled: form.enabled.checked,
    };
    if (form.event.value !== 'message.received') {
      body.event = form.event.value;
      const raw = form.filters.value.trim();
      if (raw) {
        let filters;
        try { filters = JSON.parse(raw); } catch (_) { throw new Error('Filters must be valid JSON'); }
        if (!filters || Array.isArray(filters) || typeof filters !== 'object') throw new Error('Filters must be a JSON object');
        body.filters = filters;
      }
    }
    if (editing && editing.flow) {
      body.flow = editing.flow;
    } else {
      let actions;
      try { actions = JSON.parse(form.actions.value); } catch (_) { throw new Error('Actions must be valid JSON'); }
      if (!Array.isArray(actions)) throw new Error('Actions must be a JSON list');
      body.actions = actions;
    }
    await api('/api/admin/email-triggers', { method: 'PUT', body: JSON.stringify(body) });
    $('#email-dialog').close();
    await refresh();
  } catch (error) {
    $('#email-error').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});
