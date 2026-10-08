/* Schedules view: when things run, and whether they did.

   Activity + health over the cron/rate schedule triggers. "Coming up" lists
   the next fires across every enabled schedule; the register shows each
   schedule's health (on time / waiting / needs attention / paused); the
   detail panel holds the plain-language timing, the workflows a fire
   reaches, the recent fires with their runs, and Run now / Pause / Resume /
   Edit / Delete. Everything is the API's reading — the same
   /api/admin/schedule-triggers routes `dapier schedules` drives through
   /api/agent; this file only renders it. */
import { state } from '../state.js';
import { $, $$, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, pad2 } from '../format.js';

let current = { schedules: [], now: null };
let upcoming = { hours: 24, data: null };
let filter = 'all';
let selected = null;
let fetching = false;
let editing = null; // schedule being edited, or null when creating
const UPCOMING_SHOWN = 6;
let upcomingExpanded = false;

const HEALTH = {
  ok: { label: 'On time', kind: 'ok' },
  waiting: { label: 'Waiting for first fire', kind: 'run' },
  attention: { label: 'Needs attention', kind: 'warn' },
  paused: { label: 'Paused', kind: 'off' },
};
const OUTCOME = {
  ran: { label: 'Ran', kind: 'ok' },
  no_listeners: { label: 'Fired, but no workflow is listening', kind: 'warn' },
  failed: { label: 'Failed', kind: 'err' },
  retrying: { label: 'Failed, retry queued', kind: 'warn' },
  paused: { label: 'Paused', kind: 'off' },
  resumed: { label: 'Resumed', kind: 'off' },
};
const DAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTH = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/* Times read in the viewer's own clock, like every console timestamp. */
const toDate = (value) => (value ? new Date(value) : null);
const clock = (date) => `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
function dayLabel(date) {
  const today = new Date();
  const start = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diff = Math.round((start(date) - start(today)) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Tomorrow';
  if (diff === -1) return 'Yesterday';
  return `${DAY[date.getDay()]} ${date.getDate()} ${MONTH[date.getMonth()]}`;
}
function when(value) {
  const date = toDate(value);
  if (!date || Number.isNaN(date.getTime())) return '';
  return `${dayLabel(date)} ${clock(date)}`;
}
function zoneName() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'local time'; } catch { return 'local time'; }
}

function healthOf(item) {
  const health = item.health || {};
  const meta = HEALTH[health.state] || { label: item.enabled ? 'On' : 'Off', kind: item.enabled ? 'ok' : 'off' };
  const kind = health.state === 'attention' && item.last_outcome === 'failed' ? 'err' : meta.kind;
  return { ...meta, kind, reason: health.reason || '', state: health.state || '' };
}

function dot(kind, label) {
  return `<span class="status ${kind}"><span class="status-dot" aria-hidden="true"></span>${escapeHtml(label)}</span>`;
}

function visibleSchedules() {
  const list = current.schedules;
  if (filter === 'attention') return list.filter((item) => item.health?.state === 'attention');
  if (filter === 'paused') return list.filter((item) => !item.enabled);
  return list;
}

function renderCounts() {
  const counts = {
    all: current.schedules.length,
    attention: current.schedules.filter((item) => item.health?.state === 'attention').length,
    paused: current.schedules.filter((item) => !item.enabled).length,
  };
  $$('[data-sched-filter]').forEach((button) => {
    const key = button.dataset.schedFilter;
    button.querySelector('span').textContent = String(counts[key] ?? 0);
    button.setAttribute('aria-pressed', String(key === filter));
  });
}

/* --- Coming up -------------------------------------------------------------- */

function workflowNames(ids) {
  return ids && ids.length ? ids.join(', ') : '';
}

function upcomingRow(entry) {
  const date = toDate(entry.at);
  const reach = workflowNames(entry.workflows);
  return `<li class="sched-up-row">
    <time datetime="${escapeHtml(entry.at)}">${entry.approximate ? '~' : ''}${escapeHtml(clock(date))}</time>
    <span class="sched-up-what">
      <button type="button" class="sched-up-name" data-schedule="${escapeHtml(entry.schedule_id)}">${escapeHtml(entry.schedule_id)}</button>
      <span class="sched-up-reach${reach ? '' : ' is-empty'}">${reach ? `runs ${escapeHtml(reach)}` : 'no workflow listening'}</span>
    </span>
  </li>`;
}

function renderUpcoming() {
  const box = $('#sched-upcoming');
  $$('[data-sched-window]').forEach((button) => {
    button.setAttribute('aria-pressed', String(Number(button.dataset.schedWindow) === upcoming.hours));
  });
  $('#sched-upcoming-zone').textContent = `Times in ${zoneName()}. ~ marks a rate schedule's estimated time.`;
  const data = upcoming.data;
  if (!data) { box.innerHTML = '<p class="sched-quiet">Loading…</p>'; return; }
  const entries = data.upcoming || [];
  const frequent = data.frequent || [];
  const span = upcoming.hours === 24 ? 'the next 24 hours' : 'the next 7 days';
  if (!entries.length && !frequent.length) {
    box.innerHTML = `<p class="sched-quiet">Nothing is scheduled to run in ${span}.</p>`;
    return;
  }
  const shown = upcomingExpanded ? entries : entries.slice(0, UPCOMING_SHOWN);
  const groups = [];
  shown.forEach((entry) => {
    const label = dayLabel(toDate(entry.at));
    if (!groups.length || groups[groups.length - 1].label !== label) groups.push({ label, rows: [] });
    groups[groups.length - 1].rows.push(entry);
  });
  const more = entries.length - shown.length;
  box.innerHTML = `
    ${frequent.map((entry) => `<p class="sched-frequent">
        <button type="button" class="sched-up-name" data-schedule="${escapeHtml(entry.schedule_id)}">${escapeHtml(entry.schedule_id)}</button>
        <span>${escapeHtml(entry.summary)} · ${escapeHtml(String(entry.count))} fires in ${span}</span></p>`).join('')}
    ${groups.map((group) => `<div class="sched-day"><h3>${escapeHtml(group.label)}</h3>
        <ol class="sched-up-list">${group.rows.map(upcomingRow).join('')}</ol></div>`).join('')}
    ${more > 0 ? `<button type="button" class="sched-more" data-sched-more>Show ${more} more</button>` : ''}
    ${upcomingExpanded && entries.length > UPCOMING_SHOWN ? '<button type="button" class="sched-more" data-sched-less>Show fewer</button>' : ''}
    ${data.truncated ? '<p class="sched-quiet">More fires follow; narrow the window to see them all.</p>' : ''}`;
}

/* --- register --------------------------------------------------------------- */

function lastLine(item) {
  if (!item.enabled) return 'Paused';
  if (item.last_fired_at) return `Last fired ${when(item.last_fired_at)}`;
  const manual = (item.fires || []).find((fire) => fire.manual);
  if (manual) return `Run now ${when(manual.at)}; no scheduled fire yet`;
  return 'Not fired yet';
}

function scheduleRow(item) {
  const health = healthOf(item);
  const next = (item.next_runs || [])[0];
  const isSelected = item.schedule_id === selected;
  return `<li><button type="button" class="sched-row${isSelected ? ' is-selected' : ''}" data-schedule="${escapeHtml(item.schedule_id)}" aria-pressed="${isSelected}">
    <span class="sched-row-head">
      <span class="sched-row-name">${escapeHtml(item.schedule_id)}</span>
      ${dot(health.kind, health.label)}
    </span>
    <span class="sched-row-when">${escapeHtml(item.summary || item.expression || '')}</span>
    ${health.state === 'attention' ? `<span class="sched-row-reason">${escapeHtml(health.reason)}</span>` : ''}
    <span class="sched-row-meta"><span>${escapeHtml(lastLine(item))}</span>${next ? `<span>Next ${escapeHtml(when(next))}</span>` : ''}</span>
  </button></li>`;
}

function renderList() {
  const list = visibleSchedules();
  $('#sched-list').innerHTML = list.length
    ? list.map(scheduleRow).join('')
    : `<li class="sched-quiet">${filter === 'attention' ? 'Every schedule is fine.' : 'No paused schedules.'}</li>`;
}

/* --- detail ------------------------------------------------------------------ */

function byLine(fire) {
  // Operator subjects are opaque ids; name the person only when the id reads as one.
  return fire.by && fire.by.includes('@') ? ` by ${fire.by}` : '';
}

function fireRow(fire) {
  const outcome = OUTCOME[fire.outcome] || { label: fire.outcome || 'Fired', kind: 'off' };
  const runs = fire.runs || [];
  const label = fire.outcome === 'paused' || fire.outcome === 'resumed'
    ? `${outcome.label}${byLine(fire)}`
    : outcome.label;
  const runLinks = runs.map((run) => `<button type="button" class="sched-run-link workflow-run-link" data-run="${escapeHtml(run.run_id)}">${escapeHtml(run.workflow_id)}</button>`).join('');
  const failedWithout = !runs.length && fire.workflows && fire.workflows.length
    ? `<span class="sched-fire-flows">${escapeHtml(fire.workflows.join(', '))}</span>` : '';
  return `<li class="sched-fire">
    <time datetime="${escapeHtml(fire.at || '')}">${escapeHtml(when(fire.at))}</time>
    <div class="sched-fire-body">
      <span class="sched-fire-line">${dot(outcome.kind, label)}${runLinks || failedWithout}${fire.manual ? `<span class="dk-chip">Run now${escapeHtml(byLine(fire))}</span>` : ''}</span>
      ${fire.error ? `<span class="sched-fire-error">${escapeHtml(fire.error)}</span>` : ''}
    </div>
  </li>`;
}

function workflowLinks(item) {
  const flows = item.workflows || [];
  if (!flows.length) {
    return `<p class="sched-warning">No workflow yet. In the designer, start a workflow with the Schedule trigger and pick <code>${escapeHtml(item.schedule_id)}</code>.</p>`;
  }
  return `<ul class="sched-flows">${flows.map((flow) => `<li>
      <a class="workflow-edit" href="/workflows/${encodeURIComponent(flow.id)}" data-workflow="${escapeHtml(flow.id)}">${escapeHtml(flow.name || flow.id)}</a>
      ${flow.enabled ? '' : dot('off', 'off')}
    </li>`).join('')}</ul>`;
}

function renderDetail() {
  const panel = $('#sched-detail');
  const item = current.schedules.find((entry) => entry.schedule_id === selected);
  if (!item) {
    panel.innerHTML = '<div class="sched-detail-empty"><h3>Select a schedule</h3><p>See when it runs, what it starts, and whether its recent fires worked.</p></div>';
    return;
  }
  const health = healthOf(item);
  const nexts = item.next_runs || [];
  const fires = item.fires || [];
  panel.innerHTML = `
    <header class="sched-detail-head">
      <div class="sched-detail-title">
        <h2>${escapeHtml(item.schedule_id)}</h2>
        ${dot(health.kind, health.label)}
      </div>
      ${item.description ? `<p class="sub">${escapeHtml(item.description)}</p>` : ''}
      <div class="sched-actions">
        <button type="button" class="dk-button dk-button--secondary" data-sched-action="run">Run now</button>
        <button type="button" class="dk-button dk-button--secondary" data-sched-action="${item.enabled ? 'pause' : 'resume'}">${item.enabled ? 'Pause' : 'Resume'}</button>
        <button type="button" class="dk-button dk-button--secondary" data-sched-action="edit">Edit</button>
        <button type="button" class="dk-button dk-button--danger" data-sched-action="delete">Delete</button>
      </div>
    </header>
    ${health.reason && health.state !== 'ok' ? `<p class="sched-health sched-health--${escapeHtml(health.state)}">${escapeHtml(health.reason)}</p>` : ''}
    <dl class="sched-facts">
      <div><dt>When</dt><dd><span class="sched-plain">${escapeHtml(item.summary || '')}</span> <code>${escapeHtml(item.expression || '')}</code></dd></div>
      <div><dt>Starts</dt><dd>${workflowLinks(item)}</dd></div>
      <div><dt>Next</dt><dd>${nexts.length
        ? `<ul class="sched-next">${nexts.map((value) => `<li>${item.approximate ? '~' : ''}${escapeHtml(when(value))}</li>`).join('')}</ul>`
        : `<span class="sched-quiet">${item.enabled ? 'No fire within the next year.' : 'Paused, so nothing is coming up.'}</span>`}</dd></div>
    </dl>
    <section class="sched-history" aria-labelledby="sched-history-title">
      <h3 id="sched-history-title">Recent fires</h3>
      ${fires.length
        ? `<ol class="sched-fires">${fires.map(fireRow).join('')}</ol>`
        : '<p class="sched-quiet">No fires recorded yet. Each fire is listed here with the runs it started.</p>'}
    </section>`;
}

function renderAll() {
  const has = current.schedules.length > 0;
  $('#schedule-empty').hidden = has;
  $('#sched-workspace').hidden = !has;
  if (!has) return;
  if (!current.schedules.some((item) => item.schedule_id === selected)) {
    const first = current.schedules.find((item) => item.health?.state === 'attention') || current.schedules[0];
    selected = first ? first.schedule_id : null;
  }
  renderCounts();
  renderUpcoming();
  renderList();
  renderDetail();
}

function select(name, reveal) {
  selected = name;
  renderList();
  renderDetail();
  // Stacked layout (phones): the detail sits below the register, bring it up.
  if (reveal && window.matchMedia('(max-width: 900px)').matches) {
    $('#sched-detail').scrollIntoView({ block: 'start', behavior: 'smooth' });
    $('#sched-detail').focus({ preventScroll: true });
  }
}

/* --- actions ------------------------------------------------------------------ */

async function confirmDelete(item) {
  const dialog = $('#schedule-confirm-dialog');
  $('#schedule-confirm-title').textContent = `Delete ${item.schedule_id}?`;
  $('#schedule-confirm-message').textContent = 'The EventBridge rule is removed and the schedule stops firing. The workflows that listen to it are not changed.';
  dialog.returnValue = '';
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true }));
}

async function runAction(action, button) {
  const item = current.schedules.find((entry) => entry.schedule_id === selected);
  if (!item || button.disabled) return;
  if (action === 'edit') return openDialog(item);
  const name = encodeURIComponent(item.schedule_id);
  if (action === 'delete') {
    if (!await confirmDelete(item)) return;
  }
  button.disabled = true;
  try {
    if (action === 'delete') {
      await api(`/api/admin/schedule-triggers?name=${name}`, { method: 'DELETE' });
      notice(`Schedule ${item.schedule_id} deleted`);
      selected = null;
    } else if (action === 'run') {
      const data = await api(`/api/admin/schedule-triggers/${name}/run`, { method: 'POST', body: '{}' });
      const flows = data.workflows || [];
      notice(flows.length
        ? `Run queued for ${flows.join(', ')}; it shows under Recent fires in a moment`
        : 'Fired, but no workflow listens to this schedule, so nothing will run');
      window.setTimeout(() => { if (state.view === 'schedules') void fetchSchedules(); }, 4000);
    } else {
      await api(`/api/admin/schedule-triggers/${name}/${action}`, { method: 'POST', body: '{}' });
      notice(`Schedule ${item.schedule_id} ${action === 'pause' ? 'paused' : 'resumed'}`);
    }
    await fetchSchedules();
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
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
  $('#schedule-dialog').showModal();
  if (!item) form.name.focus();
}

$('#new-schedule').addEventListener('click', () => openDialog(null));

$('#schedule-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  $('#schedule-error').textContent = '';
  try {
    const body = {
      name: form.name.value.trim(),
      expression: form.expression.value.trim(),
      description: form.description.value.trim(),
      enabled: form.enabled.checked,
    };
    await api('/api/admin/schedule-triggers', { method: 'PUT', body: JSON.stringify(body) });
    $('#schedule-dialog').close();
    notice(editing ? `Schedule ${body.name} saved` : `Schedule ${body.name} created`);
    selected = body.name;
    await fetchSchedules();
  } catch (error) {
    $('#schedule-error').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

$('[data-page="schedules"]').addEventListener('click', (event) => {
  const chip = event.target.closest('[data-sched-filter]');
  if (chip) {
    filter = chip.dataset.schedFilter;
    renderCounts();
    renderList();
    return;
  }
  const windowButton = event.target.closest('[data-sched-window]');
  if (windowButton) {
    upcoming.hours = Number(windowButton.dataset.schedWindow);
    upcomingExpanded = false;
    void fetchUpcoming();
    return;
  }
  if (event.target.closest('[data-sched-more]')) { upcomingExpanded = true; renderUpcoming(); return; }
  if (event.target.closest('[data-sched-less]')) { upcomingExpanded = false; renderUpcoming(); return; }
  const action = event.target.closest('[data-sched-action]');
  if (action) { void runAction(action.dataset.schedAction, action); return; }
  const row = event.target.closest('[data-schedule]');
  if (row) {
    if (filter !== 'all' && !visibleSchedules().some((item) => item.schedule_id === row.dataset.schedule)) {
      filter = 'all';
      renderCounts();
    }
    select(row.dataset.schedule, true);
  }
});

/* --- data ---------------------------------------------------------------------- */

async function fetchUpcoming() {
  try {
    upcoming.data = await api(`/api/admin/schedule-triggers/upcoming?hours=${upcoming.hours}`);
  } catch (error) {
    upcoming.data = { upcoming: [], frequent: [] };
    notice(error.message, true);
  }
  renderUpcoming();
}

export async function fetchSchedules() {
  if (fetching) return;
  fetching = true;
  try {
    const [list, soon] = await Promise.all([
      api('/api/admin/schedule-triggers'),
      api(`/api/admin/schedule-triggers/upcoming?hours=${upcoming.hours}`).catch(() => null),
    ]);
    current = { schedules: list.schedules || [], now: list.now || null };
    upcoming.data = soon || { upcoming: [], frequent: [] };
  } catch (error) {
    notice(error.message, true);
  } finally {
    fetching = false;
  }
  renderAll();
}

/* Called from the overview's refresh: fetch when the view is on screen,
   otherwise just repaint the cache. */
export function renderSchedules() {
  if (state.view === 'schedules') return void fetchSchedules();
  renderAll();
}

/* Entering the view (nav click, back/forward) fetches fresh schedules. */
document.addEventListener('click', (event) => {
  if (event.target.closest('[data-view="schedules"]')) void fetchSchedules();
});
window.addEventListener('popstate', () => {
  if (state.view === 'schedules') void fetchSchedules();
});
