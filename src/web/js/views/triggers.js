/* Polls — the trigger kind behind /api/admin/poll-triggers, the same
   operator endpoint `dapier polls` drives. It renders as a Workflows family
   tab (a start method of a flow). Hooks live in hooks.js, email triggers in
   the Emails tab, schedules in Schedules. */
import { state } from '../state.js';
import { $, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine } from '../format.js';
import { renderHooks } from './hooks.js';

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

let polls = [];
let fetching = false;

function findPoll(name) {
  return polls.find((item) => item.poll_id === name);
}

function pollWatches(poll) {
  if (poll.source && poll.source !== 'http') {
    const target = poll.bucket
      ? [poll.bucket, poll.prefix].filter(Boolean).join('/')
      : [poll.spreadsheet_id, poll.worksheet].filter(Boolean).join(' · ')
        || poll.folder_id || poll.for_email || poll.path
        || poll.channel_id || poll.list_id || poll.calendar_id || '';
    return `${poll.source} ${target}`.trim();
  }
  return `${poll.method || 'GET'} ${poll.url || ''}`;
}

function pollRow(poll) {
  return `<tr>
      <td class="cell-title"><span class="cell-name mono">${escapeHtml(poll.poll_id)}</span><span class="cell-sub">${escapeHtml(poll.description || '')}</span></td>
      <td class="mono muted-cell" data-label="Watches">${escapeHtml(pollWatches(poll))}</td>
      <td class="mono muted-cell" data-label="Schedule">${escapeHtml(poll.expression || '')}</td>
      <td data-label="Status">${statusLine(poll.enabled ? 'enabled' : 'disabled')}</td>
      <td class="action-cell">
        <button class="dk-button dk-button--secondary trigger-sample" data-name="${escapeHtml(poll.poll_id)}" type="button">Sample</button>
        <button class="dk-button dk-button--secondary trigger-edit" data-kind="poll" data-name="${escapeHtml(poll.poll_id)}" type="button">Edit</button>
        <button class="dk-button dk-button--secondary trigger-delete" data-kind="poll" data-name="${escapeHtml(poll.poll_id)}" type="button">Delete</button>
      </td>
    </tr>`;
}

function renderTables() {
  $('#poll-table').innerHTML = polls.map(pollRow).join('');
  $('#poll-empty').hidden = polls.length > 0;
  $('#poll-table-wrap').hidden = polls.length === 0;
}

async function fetchTriggers() {
  if (fetching) return;
  fetching = true;
  try {
    const pollData = await api('/api/admin/poll-triggers');
    polls = pollData.polls || [];
  } catch (error) {
    notice(error.message, true);
  } finally {
    fetching = false;
  }
  renderTables();
}

/* Called from the overview render after each refresh: fetch when the
   Workflows view (which hosts the tables) is on screen, otherwise just
   repaint the cache. The Hooks tab refreshes alongside. */
export function renderTriggers() {
  renderHooks();
  if (state.view === 'workflows') return void fetchTriggers();
  renderTables();
}

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
    await fetchTriggers();
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

function confirmDelete(title, message) {
  const dialog = $('#trigger-confirm-dialog');
  $('#trigger-confirm-title').textContent = title;
  $('#trigger-confirm-message').textContent = message;
  dialog.returnValue = '';
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true }));
}

document.addEventListener('click', async (event) => {
  const edit = event.target.closest('.trigger-edit');
  if (edit) {
    openPollDialog(findPoll(edit.dataset.name));
    return;
  }
  const sample = event.target.closest('.trigger-sample');
  if (sample && !sample.disabled) void pullSample(sample.dataset.name);
  const button = event.target.closest('.trigger-delete');
  if (!button || button.disabled) return;
  const item = findPoll(button.dataset.name);
  if (!item) return;
  const name = item.poll_id;
  const message = 'Its EventBridge rule is removed and the API is no longer polled.';
  if (!await confirmDelete(`Delete poll trigger ${name}?`, message)) return;
  button.disabled = true;
  try {
    await api(`/api/admin/poll-triggers?name=${encodeURIComponent(name)}`, { method: 'DELETE' });
    notice(`Deleted poll trigger ${name}.`);
    await fetchTriggers();
  } catch (error) {
    notice(error.message, true);
    button.disabled = false;
  }
});

/* Entering the Workflows family (nav, page tabs, back/forward) fetches
   fresh polls. */
document.addEventListener('click', (event) => {
  if (event.target.closest('[data-view="workflows"], [data-target="workflows"]')) void fetchTriggers();
});
window.addEventListener('popstate', () => {
  if (state.view === 'workflows') void fetchTriggers();
});
