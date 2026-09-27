/* Schedules view: cron/rate schedule triggers — the console face of the
   same /api/admin/schedule-triggers endpoints `dapier schedules` drives. */
import { state } from '../state.js';
import { $, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp } from '../format.js';

/* Sentinel flow value: edit the action list inline instead of starting a
   shared workflow. resolve_actions_flow on the server enforces the pairing. */
const INLINE_ACTIONS = '__actions__';

let current = { schedules: [], flows: [] };
let fetching = false;
let editing = null; // schedule being edited, or null when creating

function runsLabel(item) {
  if (item.flow) return `flow: ${item.flow}`;
  return (item.actions || []).map((action) => action.type).join(' → ') || '—';
}

function scheduleRow(item) {
  return `<tr>
    <td class="cell-title"><span class="cell-name mono">${escapeHtml(item.schedule_id)}</span><span class="cell-sub">${escapeHtml(item.description || '')}</span></td>
    <td class="mono muted-cell" data-label="Expression">${escapeHtml(item.expression || '—')}</td>
    <td class="mono muted-cell" data-label="Runs">${escapeHtml(runsLabel(item))}</td>
    <td data-label="Status">${statusLine(item.enabled ? 'enabled' : 'disabled', { enabled: 'On', disabled: 'Off' })}</td>
    <td class="mono muted-cell" data-label="Updated">${escapeHtml(formatTimestamp(item.updated_at) || '—')}</td>
    <td class="action-cell">
      <button class="button secondary schedule-edit" data-name="${escapeHtml(item.schedule_id)}" type="button">Edit</button>
      <button class="button secondary schedule-toggle" data-name="${escapeHtml(item.schedule_id)}" type="button">${item.enabled ? 'Turn off' : 'Turn on'}</button>
      <button class="button secondary schedule-delete" data-name="${escapeHtml(item.schedule_id)}" type="button">Delete</button>
    </td>
  </tr>`;
}

function renderRows() {
  const list = current.schedules;
  $('#schedule-table').innerHTML = list.map(scheduleRow).join('');
  $('#schedule-empty').hidden = list.length > 0;
  $('#schedule-table-wrap').hidden = list.length === 0;
  bindRowButtons();
}

function bindRowButtons() {
  $$('.schedule-edit').forEach((button) => button.addEventListener('click', () => {
    const item = current.schedules.find((entry) => entry.schedule_id === button.dataset.name);
    if (item) openDialog(item);
  }));
  $$('.schedule-toggle').forEach((button) => button.addEventListener('click', async () => {
    const item = current.schedules.find((entry) => entry.schedule_id === button.dataset.name);
    if (!item || button.disabled) return;
    button.disabled = true;
    try {
      await api('/api/admin/schedule-triggers', { method: 'PUT', body: JSON.stringify(saveBody(item, !item.enabled)) });
      await fetchSchedules();
    } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  }));
  $$('.schedule-delete').forEach((button) => button.addEventListener('click', async () => {
    const item = current.schedules.find((entry) => entry.schedule_id === button.dataset.name);
    if (!item) return;
    const dialog = $('#schedule-confirm-dialog');
    $('#schedule-confirm-title').textContent = `Delete ${item.schedule_id}?`;
    $('#schedule-confirm-message').textContent = 'The EventBridge rule is removed and the schedule stops firing. Nothing else is changed.';
    dialog.returnValue = '';
    dialog.showModal();
    const confirmed = await new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true }));
    if (!confirmed) return;
    button.disabled = true;
    try {
      await api(`/api/admin/schedule-triggers?name=${encodeURIComponent(item.schedule_id)}`, { method: 'DELETE' });
      notice(`Schedule ${item.schedule_id} deleted`);
      await fetchSchedules();
    } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  }));
}

/* The PUT body is the full item: toggling or editing must restate the
   expression and the flow/actions binding, exactly like `schedules save`. */
function saveBody(item, enabled) {
  const body = {
    name: item.schedule_id,
    expression: item.expression,
    description: item.description || '',
    enabled: Boolean(enabled),
  };
  if (item.flow) body.flow = item.flow;
  else body.actions = item.actions || [];
  return body;
}

function renderFlowChoices(selected) {
  const select = $('#schedule-flow');
  const options = current.flows.map((flow) =>
    `<option value="${escapeHtml(flow.name)}">${escapeHtml(flow.name)}${flow.actions ? ` (${flow.actions} actions)` : ''}</option>`);
  if (selected && selected !== INLINE_ACTIONS && !current.flows.some((flow) => flow.name === selected)) {
    // A flow removed from the bundled YAML since the schedule was saved —
    // keep it selectable so the binding survives an unrelated edit.
    options.push(`<option value="${escapeHtml(selected)}">${escapeHtml(selected)} (missing)</option>`);
  }
  options.push(`<option value="${INLINE_ACTIONS}">(inline actions JSON)</option>`);
  select.innerHTML = options.join('');
  select.value = selected === INLINE_ACTIONS || current.flows.some((flow) => flow.name === selected) || selected
    ? selected : (current.flows.length ? current.flows[0].name : INLINE_ACTIONS);
  $('#schedule-actions-field').hidden = select.value !== INLINE_ACTIONS;
}

function openDialog(item) {
  editing = item || null;
  const form = $('#schedule-form');
  form.reset();
  $('#schedule-error').textContent = '';
  $('#schedule-dialog-title').textContent = item ? `Edit ${item.schedule_id}` : 'New schedule';
  form.name.value = item ? item.schedule_id : '';
  form.name.disabled = Boolean(item); // the name is the EventBridge rule's identity
  form.expression.value = item ? (item.expression || '') : '';
  form.description.value = item ? (item.description || '') : '';
  form.enabled.checked = item ? Boolean(item.enabled) : true;
  renderFlowChoices(item ? (item.flow || INLINE_ACTIONS) : (current.flows.length ? current.flows[0].name : INLINE_ACTIONS));
  form.actions.value = item && Array.isArray(item.actions)
    ? JSON.stringify(item.actions, null, 2) : '';
  $('#schedule-dialog').showModal();
  if (!item) form.name.focus();
}

$('#schedule-flow').addEventListener('change', (event) => {
  $('#schedule-actions-field').hidden = event.currentTarget.value !== INLINE_ACTIONS;
});

$('#new-schedule').addEventListener('click', () => openDialog(null));

$('#schedule-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  $('#schedule-error').textContent = '';
  try {
    const flow = form.flow.value;
    const body = {
      name: form.name.value.trim(),
      expression: form.expression.value.trim(),
      description: form.description.value.trim(),
      enabled: form.enabled.checked,
    };
    if (flow === INLINE_ACTIONS) {
      let actions;
      try { actions = JSON.parse(form.actions.value); } catch (_) { throw new Error('Actions must be valid JSON'); }
      if (!Array.isArray(actions)) throw new Error('Actions must be a JSON list');
      body.actions = actions;
    } else {
      body.flow = flow;
    }
    await api('/api/admin/schedule-triggers', { method: 'PUT', body: JSON.stringify(body) });
    $('#schedule-dialog').close();
    notice(editing ? `Schedule ${body.name} saved` : `Schedule ${body.name} created`);
    await fetchSchedules();
  } catch (error) {
    $('#schedule-error').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

export async function fetchSchedules() {
  if (fetching) return;
  fetching = true;
  try {
    const data = await api('/api/admin/schedule-triggers');
    current = { schedules: data.schedules || [], flows: data.flows || [] };
  } catch (error) {
    notice(error.message, true);
  } finally {
    fetching = false;
  }
  renderRows();
}

/* Called from the overview's refresh: fetch when the view is on screen,
   otherwise just repaint the cache. */
export function renderSchedules() {
  if (state.view === 'schedules') return void fetchSchedules();
  renderRows();
}

/* Entering the view (nav click, back/forward) fetches fresh schedules. */
document.addEventListener('click', (event) => {
  if (event.target.closest('.nav-item[data-view="schedules"]')) void fetchSchedules();
});
window.addEventListener('popstate', () => {
  if (state.view === 'schedules') void fetchSchedules();
});
