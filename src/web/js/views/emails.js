/* Emails view: app-managed email triggers — reserve an address, bind actions. */
import { $, $$, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp } from '../format.js';
import { refresh } from './overview.js';
import { openDesigner } from './designer.js';

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

/* The Actions column renders as tag chips — one per action type, dashed for a
   shared flow (a reference, not inline actions), matching the workflow list. */
function actionChips(trigger) {
  if (trigger.flow) return `<span class="tag-chip folder-chip">flow: ${escapeHtml(trigger.flow)}</span>`;
  const chips = (trigger.actions || []).map((action) => `<span class="tag-chip">${escapeHtml(action.type || 'unknown')}</span>`);
  return chips.join(' ') || '<span class="muted-cell">—</span>';
}

function flowStep(number, title, detail) {
  return `<div class="flow-step"><div class="flow-head"><span class="flow-icon">${number}</span><strong class="flow-title">${escapeHtml(title)}</strong></div><div class="email-flow-detail">${detail}</div></div>`;
}

function openFlow(trigger) {
  const address = addressOf(trigger);
  $('#email-flow-title').textContent = `Flow for ${address}`;
  $('#email-flow-description').textContent = trigger.description || 'What happens when this email trigger fires.';
  const steps = [];
  const watcher = trigger.event && trigger.event !== 'message.received';
  steps.push(flowStep(1, watcher ? trigger.event : 'Email received',
    watcher ? 'A matching SES feedback event starts this flow.'
      : `Mail to <span class="mono">${escapeHtml(address)}</span> from an allowed sender starts this flow.`));
  if (trigger.flow) {
    steps.push(flowStep(steps.length + 1, `Shared flow: ${trigger.flow}`, 'Dapier runs the actions in this shared flow.'));
  } else {
    for (const action of trigger.actions || []) {
      if (action.type === 'agent') {
        const engine = action.engine || 'claude';
        const workspace = action.workspace || 'the worker’s configured root';
        const to = Object.hasOwn(action, 'notify_to') ? (action.notify_to || 'disabled') : 'the email sender';
        const from = action.notify_from || 'the deployment sender';
        const detail = `Dapier queues a headless <span class="mono">${escapeHtml(engine)}</span> run. It starts in <span class="mono">${escapeHtml(workspace)}</span>. When it finishes, Dapier sends a summary from <span class="mono">${escapeHtml(from)}</span> to <span class="mono">${escapeHtml(to)}</span>.`;
        const prompt = action.prompt ? `<details><summary>Prompt template</summary><pre class="email-flow-prompt">${escapeHtml(action.prompt)}</pre></details>` : '';
        steps.push(flowStep(steps.length + 1, 'Headless agent', detail + prompt));
      } else {
        steps.push(flowStep(steps.length + 1, action.type || 'Action',
          `Dapier runs the <span class="mono">${escapeHtml(action.type || 'unknown')}</span> action.`));
      }
    }
  }
  $('#email-flow-steps').innerHTML = steps.join('<div class="flow-link" aria-hidden="true"></div>');
  $('#email-flow-dialog').showModal();
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
  $$('.email-flow').forEach((button) => button.addEventListener('click', () => {
    const trigger = current.triggers.find((item) => item.name === button.dataset.name);
    if (trigger) openFlow(trigger);
  }));
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
  list.innerHTML = items.map((address) => `<li class="sender-chip">
      <span class="mono">${escapeHtml(address)}</span>
      <button class="sender-remove" type="button" data-address="${escapeHtml(address)}" aria-label="Remove ${escapeHtml(address)}">&times;</button>
    </li>`).join('') || '<li class="sender-empty">No senders — every message is ignored until one is added above.</li>';
  $$('.sender-remove').forEach((button) => button.addEventListener('click', async () => {
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

function triggerRow(trigger) {
  const watcher = Boolean(trigger.event && trigger.event !== 'message.received');
  const title = watcher
    ? `<span class="email-addr-row"><span class="cell-name mono">${escapeHtml(trigger.name)}</span><span class="tag-chip" title="SES feedback watcher — matches domain-wide, not a reserved address">${escapeHtml(trigger.event)}</span></span>`
    : `<span class="cell-name mono">${escapeHtml(addressOf(trigger))}</span>`;
  const description = trigger.description ? `<span class="cell-sub">${escapeHtml(trigger.description)}</span>` : '';
  const name = escapeHtml(trigger.name);
  return `<tr>
      <td class="cell-title">${title}${description}</td>
      <td data-label="Actions"><span class="cell-tags">${actionChips(trigger)}</span></td>
      <td data-label="Status">${statusLine(trigger.enabled ? 'enabled' : 'disabled')}</td>
      <td class="mono muted-cell" data-label="Updated">${formatTimestamp(trigger.updated_at) || '—'}</td>
      <td class="action-cell workflow-actions" data-label="Manage">
        <button class="button secondary email-flow" data-name="${name}" type="button">Flow</button>
        <button class="button secondary email-edit" data-name="${name}" type="button">Edit</button>
        <button type="button" class="button secondary workflow-more" popovertarget="email-menu-${name}" aria-label="More actions for ${name}">More <span aria-hidden="true">⋯</span></button>
        <div id="email-menu-${name}" class="workflow-menu" popover aria-label="Actions for ${name}">
          <button class="button secondary email-toggle" data-name="${name}" type="button">${trigger.enabled ? 'Disable' : 'Enable'}</button>
          <button class="button danger email-delete" data-name="${name}" type="button">Delete</button>
        </div>
      </td>
    </tr>`;
}

/* A route a published workflow claims: the same table, one row, the owning
   workflow as its flow chip. The workflow is the source of truth, so the
   only action here opens it in Workflows. */
function managedRow(route) {
  const workflow = escapeHtml(route.workflow);
  const address = `${escapeHtml(route.name)}@${escapeHtml(current.domain)}`;
  return `<tr>
      <td class="cell-title"><span class="cell-name mono">${address}</span><span class="cell-sub">Handled by the <strong>${workflow}</strong> workflow.</span></td>
      <td data-label="Actions"><span class="cell-tags"><span class="tag-chip folder-chip" title="This address is bound in the workflow definition">flow: ${workflow}</span></span></td>
      <td data-label="Status">${statusLine(route.status || 'enabled')}</td>
      <td class="mono muted-cell" data-label="Updated">—</td>
      <td class="action-cell workflow-actions" data-label="Manage">
        <button class="button secondary email-workflow" data-workflow="${workflow}" type="button">Edit in Workflows</button>
      </td>
    </tr>`;
}

export function renderEmails(data) {
  current = {
    domain: (data && data.domain) || '',
    triggers: (data && data.triggers) || [],
    managed: (data && data.managed_routes) || [],
  };
  $('#email-domain-hint').textContent = current.domain;
  // One list of every address at the domain: stored triggers and
  // workflow-handled routes, interleaved alphabetically by local part.
  const rows = [
    ...current.triggers.map((trigger) => ({ name: String(trigger.name || ''), row: triggerRow(trigger) })),
    ...current.managed.map((route) => ({ name: String(route.name || ''), row: managedRow(route) })),
  ].sort((a, b) => a.name.localeCompare(b.name));
  $('#email-empty').hidden = rows.length > 0;
  $('.table-wrap', $('[data-page=emails]')).hidden = rows.length === 0;
  $('#email-table').innerHTML = rows.map((entry) => entry.row).join('');
  bindRowButtons();
  bindManagedButtons();
}

function bindManagedButtons() {
  $$('.email-workflow').forEach((button) => button.addEventListener('click', () => {
    if (button.dataset.workflow) openDesigner(button.dataset.workflow);
  }));
}

// Menus close on pick; the delegated handlers above run unchanged when the
// popover closes (same contract as the workflow rows).
$('#email-table').addEventListener('click', (event) => {
  const action = event.target.closest('.workflow-menu button');
  if (action && !action.disabled) action.closest('.workflow-menu').hidePopover();
});

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
