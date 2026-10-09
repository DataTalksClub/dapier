/* Polls tab: a health monitor for poll triggers. Each poll shows what it
   watches, when it last checked, the last time it found something, whether
   it is healthy, and which workflow it starts; below, the items polls
   picked up (from the trigger inbox) with the runs they started.

   Everything here drives the same API `dapier polls` uses:
   GET  /api/admin/poll-triggers            list + health   (dapier polls list)
   GET  /api/admin/poll-triggers/activity   items picked up (dapier polls activity)
   POST /api/admin/poll-triggers/<n>/check  Poll now        (dapier polls check)
   POST …/pause | …/resume                  pause / resume  (dapier polls pause|resume)
   POST …/reset {to: now|date}              move position   (dapier polls reset)
   PUT/DELETE /api/admin/poll-triggers      create/edit/delete (dapier polls save|delete)
   Item detail + Replay reuse /api/admin/triggers/inbox/<id>[/replay]
   (dapier inbox show|replay). */
import { state } from '../state.js';
import { $, $$, icons, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, formatTimestamp, dataBlock } from '../format.js';
import { workflowName } from '../workflow-names.js';
import { OUTCOME_FILTERS, filterChips, whenHtml, workflowNames, outcomeCell, activityRow, sourceRow } from './activity.js';

/* Optional poll extras beyond the form fields; merged into the PUT body and
   validated server-side (headers, cursor). Provider
   sources (source: s3 | s3.updates | s3.deletions | google-sheets.rows |
   google-sheets.updates |
   google-drive.files | google-drive.updates | google-drive.deletions |
   google-calendar.events |
   zoom.recordings | dropbox.files | youtube.videos | mailchimp.members |
   slack.messages) take their target through these too: bucket/prefix,
   spreadsheet_id/worksheet, folder_id, for_email, path, channel_id,
   list_id, calendar_id. */
const POLL_OPTION_KEYS = ['headers', 'body', 'list_path', 'id_path', 'cursor_mode',
  'cursor_path', 'cursor_query', 'max_items', 'dedupe_ttl_days',
  'source', 'bucket', 'prefix', 'spreadsheet_id', 'worksheet', 'folder_id', 'drive_id', 'for_email',
  'path', 'channel_id', 'list_id', 'calendar_id'];

/* Sources that poll a connected account and so require connection_id on
   save (the server validates too — this is the early, human-readable
   check). s3 and mailchimp.members authenticate through stored credentials
   instead, so they stay off this list. */
const CONNECTION_POLL_SOURCES = ['google-sheets.rows', 'google-sheets.updates',
  'google-drive.files', 'google-drive.updates', 'google-drive.deletions',
  'google-calendar.events', 'gmail.messages',
  'zoom.recordings', 'dropbox.files', 'youtube.videos', 'slack.messages'];

/* What a poll watches, in words: the source's kind of thing, then its target. */
const SOURCE_LABELS = {
  http: 'API', rss: 'RSS feed', s3: 'S3 bucket', 's3.updates': 'S3 changes', 's3.deletions': 'S3 deletions',
  'google-sheets.rows': 'Sheet rows', 'google-sheets.updates': 'Sheet row edits',
  'google-drive.files': 'Drive folder', 'google-drive.updates': 'Drive changes',
  'google-drive.deletions': 'Drive deletions', 'google-calendar.events': 'Calendar',
  'gmail.messages': 'Gmail', 'zoom.recordings': 'Zoom recordings', 'dropbox.files': 'Dropbox folder',
  'youtube.videos': 'YouTube channel', 'mailchimp.members': 'Mailchimp audience',
  'slack.messages': 'Slack channel',
};

const HEALTH = {
  ok: ['ok', 'Healthy'],
  failing: ['err', 'Failing'],
  late: ['warn', 'Late'],
  paused: ['off', 'Paused'],
  waiting: ['off', 'Not checked yet'],
};

/* A picked-up item's status in the family vocabulary (activity.js). */
const OUTCOME_KEY = { matched: 'handled', unmatched: 'none', failed: 'failed', received: 'processing', ignored: 'ignored' };
const outcomeKey = (item) => OUTCOME_KEY[item.status] || 'ignored';

let polls = [];
let items = [];
let filter = 'all';
let fetching = false;

function findPoll(name) {
  return polls.find((poll) => poll.poll_id === name);
}

function statusWord(kind, text) {
  return `<span class="status ${kind}"><span class="status-dot" aria-hidden="true"></span>${escapeHtml(text)}</span>`;
}

/* "4 min ago" for scanning, the exact time in the tooltip. */
function ago(value) {
  if (!value) return '';
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return String(value);
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

function when(value, empty = 'Never') {
  if (!value) return `<span class="poll-muted">${escapeHtml(empty)}</span>`;
  return `<time datetime="${escapeHtml(value)}" title="${escapeHtml(formatTimestamp(value) || '')}">${escapeHtml(ago(value))}</time>`;
}

function every(poll) {
  const seconds = poll.interval_seconds;
  if (!seconds) return 'On a cron schedule';
  if (seconds % 86400 === 0) return seconds === 86400 ? 'Every day' : `Every ${seconds / 86400} days`;
  if (seconds % 3600 === 0) return seconds === 3600 ? 'Every hour' : `Every ${seconds / 3600} h`;
  return `Every ${Math.round(seconds / 60)} min`;
}

function pollTarget(poll) {
  if (poll.source && poll.source !== 'http') {
    const target = poll.bucket
      ? [poll.bucket, poll.prefix].filter(Boolean).join('/')
      : [poll.spreadsheet_id, poll.worksheet].filter(Boolean).join(' · ')
        || poll.folder_id || poll.for_email || poll.path
        || poll.channel_id || poll.list_id || poll.calendar_id || poll.url || '';
    return target;
  }
  return `${poll.method && poll.method !== 'GET' ? `${poll.method} ` : ''}${poll.url || ''}`;
}

function watches(poll) {
  return `${SOURCE_LABELS[poll.source || 'http'] || poll.source} ${pollTarget(poll)}`.trim();
}

function workflowLinks(flows, empty = 'No workflow') {
  if (!flows || !flows.length) return `<span class="poll-muted">${escapeHtml(empty)}</span>`;
  return flows.map((flow) => `<a class="text-link workflow-edit poll-workflow" href="/workflows/${encodeURIComponent(flow.id)}" data-workflow="${escapeHtml(flow.id)}" title="${escapeHtml(flow.id)}">${escapeHtml(workflowName(flow.id))}</a>${flow.enabled ? '' : ' <span class="poll-muted">(off)</span>'}`).join('<br>');
}

/* A poll as a source row: name and health, what it watches, then its
   rhythm and what it starts. The name opens the poll's detail. */
function pollRow(poll) {
  const status = poll.status || {};
  const [kind, text] = HEALTH[poll.health] || ['off', poll.health || 'Unknown'];
  const checked = status.last_checked_at ? `checked ${escapeHtml(ago(status.last_checked_at))}` : 'not checked yet';
  const failing = poll.health === 'failing' && status.last_error
    ? { html: `<span class="activity-error">${escapeHtml(status.last_error)}</span>`, title: status.last_error } : '';
  return sourceRow({
    attrs: `data-poll="${escapeHtml(poll.poll_id)}"`,
    name: escapeHtml(poll.poll_id),
    nameClass: 'poll-open',
    nameAttrs: `data-poll="${escapeHtml(poll.poll_id)}"`,
    nameTitle: `Open ${poll.poll_id}`,
    status: statusWord(kind, text),
    lines: [
      { html: `<span>${escapeHtml(SOURCE_LABELS[poll.source || 'http'] || poll.source)}</span> <span class="mono">${escapeHtml(pollTarget(poll))}</span>`, title: watches(poll) },
      `<span>${escapeHtml(every(poll))} · ${checked}</span>`,
      (poll.workflows || []).length ? `<span>Starts ${workflowNames(poll.workflows.map((flow) => flow.id))}</span>` : '<span class="status warn"><span class="status-dot" aria-hidden="true"></span>Starts no workflow</span>',
      failing,
    ],
  });
}

function outcomeLine(item) {
  const key = outcomeKey(item);
  const runs = (item.runs || []).map((run) => String(run).split(':')[0]);
  if (key === 'handled' || (key === 'failed' && runs.length)) {
    return outcomeCell(key, { by: workflowNames(runs.length ? runs : item.matched), error: item.error || '' });
  }
  return outcomeCell(key, { error: item.error || '', note: key === 'none' ? 'Recorded; nothing ran' : '' });
}

function itemRow(item) {
  return activityRow({
    attrs: `data-inbox="${escapeHtml(item.inbox_id)}" data-status="${escapeHtml(item.status || '')}"`,
    label: `Open ${item.title || item.item_id || 'item'}`,
    when: whenHtml(item.received_at),
    primary: `<span>${escapeHtml(item.title || item.item_id || item.inbox_id)}</span>`,
    secondary: `<span class="mono">${escapeHtml(item.poll || '')}</span>`,
    outcome: outcomeLine(item),
  });
}

function filtered() {
  if (filter === 'all') return items;
  return items.filter((item) => outcomeKey(item) === filter);
}

function renderSummary() {
  const counts = {};
  polls.forEach((poll) => { counts[poll.health] = (counts[poll.health] || 0) + 1; });
  const parts = [`${polls.length} poll${polls.length === 1 ? '' : 's'}`];
  if (counts.failing) parts.push(`${counts.failing} failing`);
  if (counts.late) parts.push(`${counts.late} late`);
  if (counts.paused) parts.push(`${counts.paused} paused`);
  $('#poll-summary').textContent = parts.join(' · ');
  const shown = filtered().length;
  $('#poll-activity-summary').textContent = `${shown} item${shown === 1 ? '' : 's'} in the last 30 days`;
}

function render() {
  $('#poll-outcome-chips').innerHTML = filterChips(OUTCOME_FILTERS, filter, 'poll-outcome');
  $('#poll-table').innerHTML = polls.map(pollRow).join('');
  $('#poll-empty').hidden = polls.length > 0;
  $('#poll-workspace').hidden = polls.length === 0;
  renderSummary();
  const shown = filtered();
  $('#poll-activity-table').innerHTML = shown.map(itemRow).join('');
  $('#poll-activity-table').hidden = shown.length === 0;
  $('#poll-activity-empty').hidden = shown.length > 0;
  $('#poll-activity-empty').querySelector('p').textContent = items.length
    ? 'No picked-up items match this filter.'
    : 'Nothing picked up in the last 30 days. Items appear here as polls find them, whether or not a workflow ran.';
  icons();
}

export async function fetchPolls() {
  if (fetching) return;
  fetching = true;
  try {
    const [pollData, activity] = await Promise.all([
      api('/api/admin/poll-triggers'),
      api('/api/admin/poll-triggers/activity?limit=50').catch(() => ({ items: [] })),
    ]);
    polls = pollData.polls || [];
    items = activity.items || [];
  } catch (error) {
    notice(error.message, true);
  } finally {
    fetching = false;
  }
  render();
}

function pollsPanelShown() {
  const panel = $('[data-workflow-panel="polls"]');
  return state.view === 'workflows' && panel && !panel.hidden;
}

/* Called from the triggers render after each overview refresh. */
export function renderPolls() {
  if (pollsPanelShown()) return void fetchPolls();
  render();
}

/* --- poll detail ---------------------------------------------------------------- */

function factRows(rows) {
  return `<dl class="detail-list poll-facts">${rows.filter(([, value]) => value).map(([label, value]) =>
    `<div><dt>${escapeHtml(label)}</dt><dd>${value}</dd></div>`).join('')}</dl>`;
}

function lastCheckText(status) {
  if (!status.last_checked_at) return '<span class="poll-muted">Not checked yet</span>';
  const how = status.last_reason === 'manual' ? 'Poll now' : 'on schedule';
  const found = status.last_found ? `found ${status.last_found} new` : 'nothing new';
  return `${when(status.last_checked_at)} <span class="poll-muted">· ${escapeHtml(how)} · ${escapeHtml(found)}</span>`;
}

function renderPollDetail(poll, recent) {
  const status = poll.status || {};
  const [kind, text] = HEALTH[poll.health] || ['off', poll.health];
  $('#poll-detail-title').textContent = poll.poll_id;
  $('#poll-detail-health').innerHTML = statusWord(kind, text);
  const problem = poll.health === 'failing' && status.last_error
    ? `<div class="detail-error poll-problem" role="status"><div><strong>The last ${status.failures > 1 ? `${status.failures} checks` : 'check'} failed.</strong><p class="mono">${escapeHtml(status.last_error)}</p></div></div>`
    : poll.health === 'late'
      ? '<p class="detail-muted poll-note">This poll has missed several scheduled checks. Use Poll now to check it by hand; if that works, re-save the poll to restore its schedule.</p>'
      : '';
  const lastNew = status.last_new_at
    ? `${when(status.last_new_at)}${status.last_new_count ? ` <span class="poll-muted">· ${status.last_new_count} item${status.last_new_count === 1 ? '' : 's'}</span>` : ''}`
    : '<span class="poll-muted">Nothing found yet</span>';
  const position = poll.cursor != null && poll.cursor !== ''
    ? `<span class="mono break-all">${escapeHtml(poll.cursor)}</span>`
    : '<span class="poll-muted">Not started — the first check sets it</span>';
  const lastError = status.last_error && poll.health !== 'failing'
    ? `${when(status.last_error_at)} <span class="poll-muted">· ${escapeHtml(status.last_error)}</span>`
    : '';
  $('#poll-detail-body').innerHTML = `${problem}
    ${factRows([
      ['Watches', `${escapeHtml(SOURCE_LABELS[poll.source || 'http'] || poll.source)} <span class="mono break-all">${escapeHtml(pollTarget(poll))}</span>`],
      ['Checks', `${escapeHtml(every(poll))} <span class="poll-muted mono">${escapeHtml(poll.expression || '')}</span>`],
      ['Last check', lastCheckText(status)],
      ['Last new item', lastNew],
      ['Starts', workflowLinks(poll.workflows, 'No workflow — items are recorded but nothing runs')],
      ['Earlier error', lastError],
      ['Position', position],
      ['About', poll.description ? escapeHtml(poll.description) : ''],
    ])}
    <section class="poll-recent" aria-labelledby="poll-recent-title">
      <h3 id="poll-recent-title" class="poll-subhead">Recently picked up</h3>
      ${recent.length
    ? `<ul class="activity-list poll-recent-list">${recent.map(itemRow).join('')}</ul>`
    : '<p class="detail-muted">Nothing picked up in the last 30 days.</p>'}
    </section>`;
  const toggle = $('#poll-detail-toggle');
  toggle.textContent = poll.enabled ? 'Pause' : 'Resume';
  $('#poll-detail-check').disabled = !poll.enabled;
  $('#poll-detail-check').title = poll.enabled ? '' : 'Resume the poll to check it';
  $$('#poll-detail-dialog [data-poll-action]').forEach((button) => { button.dataset.poll = poll.poll_id; });
  icons();
}

async function openPollDetail(name) {
  const poll = findPoll(name);
  if (!poll) return;
  const recent = items.filter((item) => item.poll === name).slice(0, 5);
  renderPollDetail(poll, recent);
  const dialog = $('#poll-detail-dialog');
  if (!dialog.open) dialog.showModal();
}

async function refreshDetail(name) {
  await fetchPolls();
  if ($('#poll-detail-dialog').open && findPoll(name)) {
    renderPollDetail(findPoll(name), items.filter((item) => item.poll === name).slice(0, 5));
  }
}

/* --- actions: Poll now, pause/resume, reset ------------------------------------------ */

async function pollAction(name, action, body = {}) {
  return api(`/api/admin/poll-triggers/${encodeURIComponent(name)}/${action}`, {
    method: 'POST', body: JSON.stringify(body),
  });
}

async function pollNow(button) {
  const name = button.dataset.poll;
  button.disabled = true;
  try {
    await pollAction(name, 'check');
    notice(`Checking ${name} now. New items start their workflows; this page refreshes in a few seconds.`);
    window.setTimeout(() => void refreshDetail(name), 6000);
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function togglePoll(button) {
  const poll = findPoll(button.dataset.poll);
  if (!poll) return;
  button.disabled = true;
  try {
    await pollAction(poll.poll_id, poll.enabled ? 'pause' : 'resume');
    notice(poll.enabled ? `Paused ${poll.poll_id}. Its position is kept.` : `Resumed ${poll.poll_id}.`);
    await refreshDetail(poll.poll_id);
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function openReset(name) {
  const poll = findPoll(name);
  if (!poll) return;
  const form = $('#poll-reset-form');
  form.reset();
  form.dataset.poll = name;
  $('#poll-reset-error').textContent = '';
  $('#poll-reset-title').textContent = `Reset ${name}`;
  const dated = (poll.reset_modes || []).includes('date');
  form.elements.to.forEach((radio) => { radio.disabled = radio.value === 'date' && !dated; });
  form.date.disabled = true;
  $('#poll-reset-date-hint').textContent = dated
    ? 'Items newer than this date run again on the next check, including ones that already ran.'
    : 'Not available: this poll does not track its position by date.';
  $('#poll-reset-dialog').showModal();
}

$('#poll-reset-form').addEventListener('change', (event) => {
  const form = event.currentTarget;
  form.date.disabled = form.elements.to.value !== 'date';
  if (!form.date.disabled) form.date.focus();
});

$('#poll-reset-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  const name = form.dataset.poll;
  const to = form.elements.to.value;
  $('#poll-reset-error').textContent = '';
  if (to === 'date' && !form.date.value) {
    $('#poll-reset-error').textContent = 'Pick a date.';
    return;
  }
  submit.disabled = true;
  try {
    const result = await pollAction(name, 'reset', to === 'date' ? { to, date: form.date.value } : { to });
    $('#poll-reset-dialog').close();
    notice(to === 'date'
      ? `${name} picks up items newer than ${form.date.value} on its next check.`
      : `${name} now starts from now${result.skipped ? ` — ${result.skipped} waiting item${result.skipped === 1 ? '' : 's'} skipped` : ''}.`);
    await refreshDetail(name);
  } catch (error) {
    $('#poll-reset-error').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

/* --- picked-up item detail + replay -------------------------------------------------------- */

async function openItem(inboxId) {
  const cached = items.find((item) => item.inbox_id === inboxId) || {};
  let event = cached;
  try {
    event = { ...cached, ...((await api(`/api/admin/triggers/inbox/${encodeURIComponent(inboxId)}`)).event || {}) };
  } catch (error) {
    if (!cached.inbox_id) return void notice(error.message, true);
  }
  const data = event.data && typeof event.data === 'object' ? event.data : {};
  const runs = cached.runs || (event.matched || []).map((flow) => `${flow}:${inboxId}`);
  $('#poll-item-title').textContent = cached.title || data.title || data.name || data.item_id || 'Picked-up item';
  $('#poll-item-body').innerHTML = `
    ${event.error ? `<div class="detail-error" role="status"><p class="mono">${escapeHtml(event.error)}</p></div>` : ''}
    ${factRows([
      ['Poll', data.poll ? `<button class="text-link poll-open-link" type="button" data-poll="${escapeHtml(data.poll)}">${escapeHtml(data.poll)}</button>` : ''],
      ['Picked up', event.received_at ? `${escapeHtml(formatTimestamp(event.received_at))} <span class="poll-muted">· ${escapeHtml(ago(event.received_at))}</span>` : ''],
      ['Outcome', `<span class="activity-outcome">${outcomeCell(outcomeKey(event), { note: outcomeKey(event) === 'none' ? 'Recorded; nothing ran' : '' })}</span>`],
      ['Runs', runs.length ? runs.map((run) => `<button class="text-link workflow-run-link" type="button" data-run="${escapeHtml(run)}" title="${escapeHtml(run)}">Open the ${escapeHtml(workflowName(run.split(':')[0]))} run</button>`).join('<br>') : ''],
      ['Item id', data.item_id ? `<span class="mono break-all">${escapeHtml(data.item_id)}</span>` : ''],
    ])}
    <h3 class="poll-subhead">What the poll found</h3>
    ${dataBlock(data)}`;
  $('#poll-item-replay').dataset.inbox = inboxId;
  const dialog = $('#poll-item-dialog');
  if (!dialog.open) dialog.showModal();
  icons();
}

function confirmReplay() {
  const dialog = $('#poll-replay-confirm-dialog');
  dialog.returnValue = '';
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true }));
}

$('#poll-item-replay').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  const inboxId = button.dataset.inbox;
  if (!inboxId || !await confirmReplay()) return;
  button.disabled = true;
  try {
    const data = await api(`/api/admin/triggers/inbox/${encodeURIComponent(inboxId)}/replay`, { method: 'POST', body: '{}' });
    notice(`Replay queued${data.event_id ? ` as ${data.event_id}` : ''}. The new run appears under Runs once the worker picks it up.`);
    window.setTimeout(() => void fetchPolls(), 4000);
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
});

/* --- create / edit / sample / delete (the existing poll dialog) ------------------------------ */

function pollOptions(poll) {
  const options = {};
  POLL_OPTION_KEYS.forEach((key) => {
    const value = poll ? poll[key] : undefined;
    if (value !== undefined && value !== null && value !== '') options[key] = value;
  });
  return JSON.stringify(options, null, 2);
}

function openPollDialog(poll) {
  const form = $('#poll-form');
  form.reset();
  $('#poll-error').textContent = '';
  $('#poll-dialog-title').textContent = poll ? `Edit poll ${poll.poll_id}` : 'New poll';
  form.name.value = poll ? poll.poll_id : '';
  form.name.disabled = Boolean(poll); // the name is the EventBridge rule's suffix
  form.expression.value = poll ? (poll.expression || '') : '';
  form.source.value = poll && poll.source && poll.source !== 'http' ? poll.source : '';
  form.url.value = poll ? (poll.url || '') : '';
  form.method.value = poll ? (poll.method || 'GET') : 'GET';
  form.connection_id.value = poll ? (poll.connection_id || '') : '';
  form.description.value = poll ? (poll.description || '') : '';
  form.enabled.checked = poll ? Boolean(poll.enabled) : true;
  form.options.value = poll ? pollOptions(poll) : '{}';
  $('#poll-dialog').showModal();
  if (!poll) form.name.focus();
}

$('#new-poll').addEventListener('click', () => openPollDialog(null));

$('#poll-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  $('#poll-error').textContent = '';
  try {
    const source = form.source.value;
    /* rss polls a public feed URL like the HTTP source — no connection, the
       URL field carries the target. Other provider sources blank it and take
       their params through Options. */
    const urlSource = !source || source === 'rss';
    if (urlSource && !form.url.value.trim()) throw new Error('URL is required for the HTTP and RSS sources');
    if (CONNECTION_POLL_SOURCES.includes(source) && !form.connection_id.value.trim()) {
      throw new Error(`${source} needs a connection (the connected account to poll)`);
    }
    const body = {
      name: form.name.value.trim(),
      expression: form.expression.value.trim(),
      url: urlSource ? form.url.value.trim() : '',
      method: form.method.value,
      connection_id: form.connection_id.value.trim(),
      description: form.description.value.trim(),
      enabled: form.enabled.checked,
    };
    if (source) body.source = source;
    if (form.options.value.trim()) {
      let options;
      try { options = JSON.parse(form.options.value); } catch (_) { throw new Error('Options must be valid JSON'); }
      if (!options || typeof options !== 'object' || Array.isArray(options)) throw new Error('Options must be a JSON object');
      Object.assign(body, options);
    }
    await api('/api/admin/poll-triggers', { method: 'PUT', body: JSON.stringify(body) });
    $('#poll-dialog').close();
    notice(`Saved poll ${body.name}. It is live immediately.`);
    await refreshDetail(body.name);
  } catch (error) {
    $('#poll-error').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

/* Zapier's "Test trigger": pull one sample event the poll would publish —
   live from the provider when it can, else the newest recorded run, else a
   documented example. Goes through POST /api/admin/discover with the
   generic poll connector, whose fetch runs the stored poll's own page
   fetch (HTTP or provider source) without advancing its cursor. */
async function pullSample(pollId) {
  const dialog = $('#poll-sample-dialog');
  $('#poll-sample-title').textContent = `Sample — ${pollId}`;
  $('#poll-sample-meta').textContent = 'Pulling…';
  $('#poll-sample-data').textContent = '';
  dialog.showModal();
  try {
    const result = await api('/api/admin/discover', {
      method: 'POST',
      body: JSON.stringify({ connector: 'poll', event: pollId }),
    });
    const sample = result.sample || {};
    $('#poll-sample-meta').textContent =
      `${result.connector || 'poll'} · ${sample.event || ''} · ${result.source || 'sample'}`
      + (sample.occurred_at ? ` · ${sample.occurred_at}` : '');
    $('#poll-sample-data').textContent = JSON.stringify(sample.data ?? sample, null, 2);
  } catch (error) {
    $('#poll-sample-meta').textContent = '';
    $('#poll-sample-data').textContent = error.message;
  }
}

function confirmDelete(name) {
  const dialog = $('#trigger-confirm-dialog');
  $('#trigger-confirm-title').textContent = `Delete poll trigger ${name}?`;
  $('#trigger-confirm-message').textContent = 'Its EventBridge rule is removed and the source is no longer checked. Workflows that start from it stop receiving items.';
  dialog.returnValue = '';
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true }));
}

async function deletePoll(button) {
  const name = button.dataset.poll;
  if (!name || !await confirmDelete(name)) return;
  button.disabled = true;
  try {
    await api(`/api/admin/poll-triggers?name=${encodeURIComponent(name)}`, { method: 'DELETE' });
    $('#poll-detail-dialog').close();
    notice(`Deleted poll trigger ${name}.`);
    await fetchPolls();
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
}

/* --- wiring ------------------------------------------------------------------------------------- */

document.addEventListener('click', (event) => {
  const actionButton = event.target.closest('#poll-detail-dialog [data-poll-action]');
  if (actionButton && !actionButton.disabled) {
    const action = actionButton.dataset.pollAction;
    const name = actionButton.dataset.poll;
    if (action === 'check') return void pollNow(actionButton);
    if (action === 'toggle') return void togglePoll(actionButton);
    if (action === 'reset') return openReset(name);
    if (action === 'sample') return void pullSample(name);
    if (action === 'edit') return openPollDialog(findPoll(name));
    if (action === 'delete') return void deletePoll(actionButton);
  }
  /* A workflow link inside a poll dialog opens the designer: close the
     dialogs first so the canvas is not under a modal. */
  if (event.target.closest('.poll-workflow')) {
    ['#poll-item-dialog', '#poll-detail-dialog'].forEach((id) => { if ($(id).open) $(id).close(); });
    return;
  }
  if (event.target.closest('a, .workflow-run-link')) return;
  const chip = event.target.closest('[data-poll-outcome]');
  if (chip) {
    filter = chip.dataset.pollOutcome;
    return void render();
  }
  const pollLink = event.target.closest('.poll-open-link');
  if (pollLink) {
    $('#poll-item-dialog').close();
    return void openPollDetail(pollLink.dataset.poll);
  }
  const itemRowEl = event.target.closest('.poll-item-open, .activity-row[data-inbox]');
  if (itemRowEl && itemRowEl.closest('[data-workflow-panel="polls"], #poll-detail-dialog')) return void openItem(itemRowEl.dataset.inbox);
  const pollRowEl = event.target.closest('.poll-open');
  if (pollRowEl) return void openPollDetail(pollRowEl.dataset.poll);
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const row = event.target.closest('.activity-row[data-inbox]');
  if (!row || event.target !== row || !row.closest('[data-workflow-panel="polls"], #poll-detail-dialog')) return;
  event.preventDefault();
  void openItem(row.dataset.inbox);
});

/* Entering the Polls tab (tab link, back/forward) fetches fresh state. */
document.addEventListener('click', (event) => {
  if (event.target.closest('a[data-view="workflows"][data-tab="polls"]')) window.setTimeout(() => void fetchPolls(), 0);
});
window.addEventListener('popstate', () => {
  if (pollsPanelShown()) void fetchPolls();
});
