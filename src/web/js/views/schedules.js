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
import { workflowName } from '../workflow-names.js';
import { openRun } from './runs.js';
import { OUTCOME_FILTERS, filterChips, whenHtml, workflowNames, outcomeCell, activityRow, sourceRow } from './activity.js';

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
/* A fire's outcome in the family vocabulary (activity.js). */
const OUTCOME_KEY = { ran: 'handled', no_listeners: 'none', failed: 'failed', retrying: 'failed', paused: 'paused', resumed: 'resumed' };
const fireKey = (fire) => OUTCOME_KEY[fire.outcome] || 'ignored';
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

/* Every fire of every schedule, newest first: the tab's activity. */
function allFires() {
  return current.schedules
    .flatMap((item) => (item.fires || []).map((fire) => ({ ...fire, schedule_id: item.schedule_id })))
    .sort((a, b) => String(b.at || '').localeCompare(String(a.at || '')));
}

function shownFires() {
  const fires = allFires();
  return filter === 'all' ? fires : fires.filter((fire) => fireKey(fire) === filter);
}

/* --- Coming up -------------------------------------------------------------- */

function upcomingRow(entry) {
  const date = toDate(entry.at);
  return `<li class="source-row sched-up-row">
    <div class="source-head">
      <time class="sched-up-time" datetime="${escapeHtml(entry.at)}">${entry.approximate ? '~' : ''}${escapeHtml(clock(date))}</time>
      <button type="button" class="source-name mono" data-schedule="${escapeHtml(entry.schedule_id)}" title="Open ${escapeHtml(entry.schedule_id)}">${escapeHtml(entry.schedule_id)}</button>
    </div>
    <div class="source-line">${(entry.workflows || []).length ? `Starts ${workflowNames(entry.workflows)}` : '<span class="status warn"><span class="status-dot" aria-hidden="true"></span>No workflow listening</span>'}</div>
  </li>`;
}

function renderUpcoming() {
  const box = $('#sched-upcoming');
  $('#sched-window-chips').innerHTML = filterChips([['24', '24 hours'], ['168', '7 days']], String(upcoming.hours), 'sched-window');
  $('#sched-upcoming-zone').textContent = `Times in ${zoneName()}; ~ marks an estimated time.`;
  const data = upcoming.data;
  if (!data) { box.innerHTML = '<li class="source-row sched-quiet">Loading…</li>'; return; }
  const entries = data.upcoming || [];
  const frequent = data.frequent || [];
  const span = upcoming.hours === 24 ? 'the next 24 hours' : 'the next 7 days';
  if (!entries.length && !frequent.length) {
    box.innerHTML = `<li class="source-row sched-quiet">Nothing runs in ${span}.</li>`;
    return;
  }
  const shown = upcomingExpanded ? entries : entries.slice(0, UPCOMING_SHOWN);
  const rows = [];
  let lastDay = '';
  shown.forEach((entry) => {
    const label = dayLabel(toDate(entry.at));
    if (label !== lastDay) { rows.push(`<li class="sched-day">${escapeHtml(label)}</li>`); lastDay = label; }
    rows.push(upcomingRow(entry));
  });
  const more = entries.length - shown.length;
  box.innerHTML = `
    ${frequent.map((entry) => `<li class="source-row"><div class="source-head"><button type="button" class="source-name mono" data-schedule="${escapeHtml(entry.schedule_id)}">${escapeHtml(entry.schedule_id)}</button></div>
        <div class="source-line">${escapeHtml(entry.summary)} · ${escapeHtml(String(entry.count))} fires in ${span}</div></li>`).join('')}
    ${rows.join('')}
    ${more > 0 ? `<li class="source-row"><button type="button" class="text-link" data-sched-more>Show ${more} more</button></li>` : ''}
    ${upcomingExpanded && entries.length > UPCOMING_SHOWN ? '<li class="source-row"><button type="button" class="text-link" data-sched-less>Show fewer</button></li>' : ''}
    ${data.truncated ? '<li class="source-row sched-quiet">More fires follow; narrow the window to see them all.</li>' : ''}`;
}

/* --- schedules (the tab's sources) ---------------------------------------------- */

function lastLine(item) {
  if (item.last_fired_at) return `Last fired ${when(item.last_fired_at)}`;
  if (!item.enabled) return 'Never fired';
  const manual = (item.fires || []).find((fire) => fire.manual);
  if (manual) return `Run now ${when(manual.at)}; no scheduled fire yet`;
  return 'Not fired yet';
}

function scheduleRow(item) {
  const health = healthOf(item);
  const next = (item.next_runs || [])[0];
  const flows = (item.workflows || []).map((flow) => flow.id);
  return sourceRow({
    attrs: `data-schedule-row="${escapeHtml(item.schedule_id)}"`,
    name: escapeHtml(item.schedule_id),
    nameAttrs: `data-schedule="${escapeHtml(item.schedule_id)}"`,
    nameTitle: `Open ${item.schedule_id}`,
    status: dot(health.kind, health.label),
    lines: [
      { html: `<span>${escapeHtml(item.summary || '')}</span> <code class="mono">${escapeHtml(item.expression || '')}</code>`, title: item.expression || '' },
      `<span>${escapeHtml(lastLine(item))}${next ? ` · next ${escapeHtml(when(next))}` : ''}</span>`,
      flows.length ? `<span>Starts ${workflowNames(flows)}</span>` : '<span class="status warn"><span class="status-dot" aria-hidden="true"></span>Starts no workflow</span>',
      health.state === 'attention' ? { html: `<span class="activity-error">${escapeHtml(health.reason)}</span>`, title: health.reason } : '',
    ],
  });
}

/* --- fires (the tab's activity) --------------------------------------------------- */

function byLine(fire) {
  // Operator subjects are opaque ids; name the person only when the id reads as one.
  return fire.by && fire.by.includes('@') ? ` by ${fire.by}` : '';
}

function fireOutcome(fire) {
  const key = fireKey(fire);
  const flows = (fire.runs || []).map((run) => run.workflow_id).concat((fire.runs || []).length ? [] : (fire.workflows || []));
  if (key === 'handled') return outcomeCell(key, { by: workflowNames(flows) });
  if (key === 'failed') return outcomeCell(key, { note: fire.outcome === 'retrying' ? 'Retry queued' : '', error: fire.error || '' });
  if (key === 'none') return outcomeCell(key, { note: 'Fired; nothing listens' });
  return outcomeCell(key, { note: escapeHtml(byLine(fire).trim()) });
}

function fireRow(fire) {
  const run = (fire.runs || [])[0];
  return activityRow({
    attrs: `data-fire-schedule="${escapeHtml(fire.schedule_id)}"${run ? ` data-run="${escapeHtml(run.run_id)}"` : ''}`,
    label: run ? `Open the run from ${fire.schedule_id}` : `Open ${fire.schedule_id}`,
    when: whenHtml(fire.at),
    primary: `<span class="mono">${escapeHtml(fire.schedule_id)}</span>${fire.manual ? '<span class="dk-chip">Run now</span>' : ''}`,
    secondary: escapeHtml(fire.manual ? `Started by hand${byLine(fire)}`
      : fire.outcome === 'paused' || fire.outcome === 'resumed' ? 'Schedule changed' : 'On schedule'),
    outcome: fireOutcome(fire),
  });
}

function renderActivity() {
  $('#sched-outcome-chips').innerHTML = filterChips(OUTCOME_FILTERS, filter, 'sched-filter');
  const fires = shownFires();
  $('#sched-fires').innerHTML = fires.map(fireRow).join('');
  $('#sched-fires').hidden = fires.length === 0;
  $('#sched-fires-empty').hidden = fires.length > 0;
  $('#sched-fires-empty').querySelector('p').textContent = allFires().length
    ? 'No fire matches this filter.'
    : 'No fires recorded yet. Each fire is listed here with the runs it started.';
  $('#sched-fires-summary').textContent = `${fires.length} fire${fires.length === 1 ? '' : 's'}`;
}

function renderList() {
  const list = [...current.schedules].sort((a, b) =>
    Number(b.health?.state === 'attention') - Number(a.health?.state === 'attention'));
  $('#sched-list').innerHTML = list.map(scheduleRow).join('');
  const attention = current.schedules.filter((item) => item.health?.state === 'attention').length;
  const paused = current.schedules.filter((item) => !item.enabled).length;
  $('#sched-summary').textContent = [`${current.schedules.length} schedule${current.schedules.length === 1 ? '' : 's'}`,
    attention ? `${attention} need attention` : '', paused ? `${paused} paused` : ''].filter(Boolean).join(' · ');
}

/* --- detail dialog ---------------------------------------------------------------- */

function workflowLinks(item) {
  const flows = item.workflows || [];
  if (!flows.length) {
    return `<p class="sched-warning">No workflow yet. In the designer, start a workflow with the Schedule trigger and pick <code>${escapeHtml(item.schedule_id)}</code>.</p>`;
  }
  return `<ul class="sched-flows">${flows.map((flow) => `<li>
      <a class="text-link workflow-edit" href="/workflows/${encodeURIComponent(flow.id)}" data-workflow="${escapeHtml(flow.id)}" title="${escapeHtml(flow.id)}">${escapeHtml(workflowName(flow.id))}</a>
      ${flow.enabled ? '' : dot('off', 'Off')}
    </li>`).join('')}</ul>`;
}

function renderDetail() {
  const item = current.schedules.find((entry) => entry.schedule_id === selected);
  if (!item) return;
  const health = healthOf(item);
  const nexts = item.next_runs || [];
  const fires = (item.fires || []).map((fire) => ({ ...fire, schedule_id: item.schedule_id }));
  $('#schedule-detail-title').textContent = item.schedule_id;
  $('#schedule-detail-health').innerHTML = dot(health.kind, health.label);
  $('#schedule-detail-body').innerHTML = `
    ${item.description ? `<p class="sub">${escapeHtml(item.description)}</p>` : ''}
    ${health.reason && health.state !== 'ok' ? `<p class="sched-health sched-health--${escapeHtml(health.state)}">${escapeHtml(health.reason)}</p>` : ''}
    <dl class="detail-list">
      <div><dt>When</dt><dd>${escapeHtml(item.summary || '')} <code>${escapeHtml(item.expression || '')}</code></dd></div>
      <div><dt>Starts</dt><dd>${workflowLinks(item)}</dd></div>
      <div><dt>Next</dt><dd>${nexts.length
        ? `<ul class="sched-next">${nexts.map((value) => `<li class="mono">${item.approximate ? '~' : ''}${escapeHtml(when(value))}</li>`).join('')}</ul>`
        : `<span class="muted-cell">${item.enabled ? 'No fire within the next year.' : 'Paused, so nothing is coming up.'}</span>`}</dd></div>
    </dl>
    <section aria-labelledby="sched-history-title">
      <h3 id="sched-history-title" class="detail-heading">Recent fires</h3>
      ${fires.length
        ? `<ul class="activity-list sched-history">${fires.map(fireRow).join('')}</ul>`
        : '<p class="muted-cell">No fires recorded yet.</p>'}
    </section>`;
  const toggle = $('#schedule-detail-toggle');
  toggle.dataset.schedAction = item.enabled ? 'pause' : 'resume';
  toggle.textContent = item.enabled ? 'Pause' : 'Resume';
}

function renderAll() {
  const has = current.schedules.length > 0;
  $('#schedule-empty').hidden = has;
  $('#sched-workspace').hidden = !has;
  if (!has) return;
  renderActivity();
  renderUpcoming();
  renderList();
  if ($('#schedule-detail-dialog').open) renderDetail();
}

function select(name) {
  selected = name;
  renderDetail();
  const dialog = $('#schedule-detail-dialog');
  if (!dialog.open) dialog.showModal();
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
      $('#schedule-detail-dialog').close();
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
  if (chip) { filter = chip.dataset.schedFilter; renderActivity(); return; }
  const windowButton = event.target.closest('[data-sched-window]');
  if (windowButton) {
    upcoming.hours = Number(windowButton.dataset.schedWindow);
    upcomingExpanded = false;
    void fetchUpcoming();
    return;
  }
  if (event.target.closest('[data-sched-more]')) { upcomingExpanded = true; renderUpcoming(); return; }
  if (event.target.closest('[data-sched-less]')) { upcomingExpanded = false; renderUpcoming(); return; }
  const name = event.target.closest('[data-schedule]');
  if (name) { select(name.dataset.schedule); return; }
  const fire = event.target.closest('.activity-row[data-fire-schedule]');
  if (fire) {
    if (fire.dataset.run) void openRun(fire.dataset.run);
    else select(fire.dataset.fireSchedule);
  }
});

$('#schedule-detail-dialog').addEventListener('click', (event) => {
  const action = event.target.closest('[data-sched-action]');
  if (action) { void runAction(action.dataset.schedAction, action); return; }
  if (event.target.closest('.workflow-edit')) { $('#schedule-detail-dialog').close(); return; }
  const fire = event.target.closest('.activity-row[data-run]');
  if (fire) void openRun(fire.dataset.run);
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const row = event.target.closest('.activity-row[data-fire-schedule]');
  if (!row || event.target !== row) return;
  event.preventDefault();
  row.click();
});
document.addEventListener('dapier:workflows-loaded', () => renderAll());

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
  if (event.target.closest('a[data-view="schedules"]')) void fetchSchedules();
});
window.addEventListener('popstate', () => {
  if (state.view === 'schedules') void fetchSchedules();
});
