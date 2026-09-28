/* Triggers view: webhook/Telegram hooks and API polls — the trigger kinds
   behind /api/admin/hook-triggers and /api/admin/poll-triggers, the same
   operator endpoints `dapier hooks` and `dapier polls` drive. (Email
   triggers live in the Emails view; schedules in Schedules.) */
import { state } from '../state.js';
import { $, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp } from '../format.js';

const HOOK_ACTIONS_TEMPLATE = JSON.stringify([
  { type: 'webhook', url: 'https://example.test/hook' },
], null, 2);
/* Optional poll extras beyond the form fields; merged into the PUT body and
   validated server-side (headers, cursor, and the actions list). Provider
   sources (source: s3 | s3.updates | s3.deletions | google-sheets.rows |
   google-drive.files | google-drive.updates | google-drive.deletions |
   google-calendar.events |
   zoom.recordings | dropbox.files | youtube.videos | mailchimp.members |
   slack.messages) take their target through these too: bucket/prefix,
   spreadsheet_id/worksheet, folder_id, for_email, path, channel_id,
   list_id, calendar_id. */
const POLL_OPTION_KEYS = ['headers', 'body', 'list_path', 'id_path', 'cursor_mode',
  'cursor_path', 'cursor_query', 'max_items', 'dedupe_ttl_days', 'actions', 'flow',
  'source', 'bucket', 'prefix', 'spreadsheet_id', 'worksheet', 'folder_id', 'for_email',
  'path', 'channel_id', 'list_id', 'calendar_id'];

/* Sources that poll a connected account and so require connection_id on
   save (the server validates too — this is the early, human-readable
   check). s3 and mailchimp.members authenticate through stored credentials
   instead, so they stay off this list. */
const CONNECTION_POLL_SOURCES = ['google-sheets.rows', 'google-drive.files',
  'google-drive.updates', 'google-drive.deletions',
  'google-calendar.events',
  'zoom.recordings', 'dropbox.files', 'youtube.videos', 'slack.messages'];

let hooks = [];
let polls = [];
let fetching = false;

function actionSummary(item) {
  if (item.flow) return `flow: ${item.flow}`;
  return (item.actions || []).map((action) => action.type).join(' → ') || '—';
}

function findHook(name) {
  return hooks.find((item) => item.hook_id === name);
}

function findPoll(name) {
  return polls.find((item) => item.poll_id === name);
}

function hookRow(hook) {
  return `<tr>
      <td class="cell-title"><span class="cell-name mono">${escapeHtml(hook.hook_id)}</span><span class="cell-sub">${escapeHtml(hook.description || '')}</span></td>
      <td data-label="Kind">${escapeHtml(hook.kind || 'webhook')}</td>
      <td class="mono muted-cell" data-label="Runs">${escapeHtml(actionSummary(hook))}</td>
      <td data-label="Status">${statusLine(hook.enabled ? 'enabled' : 'disabled')}</td>
      <td class="mono muted-cell" data-label="Updated">${formatTimestamp(hook.updated_at) || '—'}</td>
      <td class="action-cell">
        <button class="button secondary trigger-edit" data-kind="hook" data-name="${escapeHtml(hook.hook_id)}" type="button">Edit</button>
        <button class="button secondary trigger-delete" data-kind="hook" data-name="${escapeHtml(hook.hook_id)}" type="button">Delete</button>
      </td>
    </tr>`;
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
      <td class="mono muted-cell" data-label="Runs">${escapeHtml(actionSummary(poll))}</td>
      <td data-label="Status">${statusLine(poll.enabled ? 'enabled' : 'disabled')}</td>
      <td class="action-cell">
        <button class="button secondary trigger-sample" data-name="${escapeHtml(poll.poll_id)}" type="button">Sample</button>
        <button class="button secondary trigger-edit" data-kind="poll" data-name="${escapeHtml(poll.poll_id)}" type="button">Edit</button>
        <button class="button secondary trigger-delete" data-kind="poll" data-name="${escapeHtml(poll.poll_id)}" type="button">Delete</button>
      </td>
    </tr>`;
}

function renderTables() {
  $('#hook-table').innerHTML = hooks.map(hookRow).join('');
  $('#hook-empty').hidden = hooks.length > 0;
  $('#hook-table-wrap').hidden = hooks.length === 0;
  $('#poll-table').innerHTML = polls.map(pollRow).join('');
  $('#poll-empty').hidden = polls.length > 0;
  $('#poll-table-wrap').hidden = polls.length === 0;
}

async function fetchTriggers() {
  if (fetching) return;
  fetching = true;
  try {
    const [hookData, pollData] = await Promise.all([
      api('/api/admin/hook-triggers'),
      api('/api/admin/poll-triggers'),
    ]);
    hooks = hookData.hooks || [];
    polls = pollData.polls || [];
  } catch (error) {
    notice(error.message, true);
  } finally {
    fetching = false;
  }
  renderTables();
}

/* Called from main after the startup refresh: fetch when the view is on
   screen, otherwise just repaint the cache. */
export function renderTriggers() {
  if (state.view === 'triggers') return void fetchTriggers();
  renderTables();
}

function openHookDialog(hook) {
  const form = $('#hook-form');
  form.reset();
  $('#hook-error').textContent = '';
  $('#hook-dialog-title').textContent = hook ? `Edit hook ${hook.hook_id}` : 'New hook';
  form.name.value = hook ? hook.hook_id : '';
  form.name.disabled = Boolean(hook); // the name is the hook URL's last segment
  form.kind.value = hook ? (hook.kind || 'webhook') : 'webhook';
  form.kind.disabled = Boolean(hook); // a kind change would orphan the URL
  form.connection_id.value = hook ? (hook.connection_id || '') : '';
  form.list_id.value = hook ? (hook.list_id || '') : '';
  form.description.value = hook ? (hook.description || '') : '';
  form.dedupe_path.value = hook ? (hook.dedupe_path || '') : '';
  const response = hook ? (hook.response || {}) : {};
  form.response_mode.value = response.mode || 'ack';
  form.response_template.value = response.template !== undefined
    ? JSON.stringify(response.template, null, 2)
    : '';
  $('#hook-response-field').hidden = form.kind.value === 'telegram';
  $('#hook-response-template-field').hidden =
    form.kind.value === 'telegram' || form.response_mode.value !== 'sync';
  // The secret is write-only (the API never echoes it back): a signed hook
  // shows only that the lock exists, and an edit keeps it unless the value
  // is replaced or explicitly cleared.
  const signed = Boolean(hook && hook.signed);
  form.secret.value = '';
  form.secret.placeholder = signed ? '(configured — leave empty to keep)' : 'shared secret';
  form.signature_header.value = signed ? (hook.signature_header || '') : '';
  form.clear_secret.checked = false;
  $('#hook-clear-secret-field').hidden = !signed;
  $('#hook-secret-field').hidden = form.kind.value !== 'webhook';
  form.enabled.checked = hook ? Boolean(hook.enabled) : true;
  form.actions.value = hook && hook.actions
    ? JSON.stringify(hook.actions, null, 2)
    : HOOK_ACTIONS_TEMPLATE;
  form.actions.disabled = Boolean(hook && hook.flow);
  $('#hook-connection-field').hidden = form.kind.value !== 'telegram';
  $('#hook-list-field').hidden = form.kind.value !== 'mailchimp';
  $('#hook-dialog').showModal();
  if (!hook) form.name.focus();
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

$('#new-hook').addEventListener('click', () => openHookDialog(null));
$('#new-poll').addEventListener('click', () => openPollDialog(null));

$('#hook-form').elements.kind.addEventListener('change', (event) => {
  $('#hook-connection-field').hidden = event.currentTarget.value !== 'telegram';
  $('#hook-list-field').hidden = event.currentTarget.value !== 'mailchimp';
  $('#hook-response-field').hidden = event.currentTarget.value === 'telegram';
  $('#hook-secret-field').hidden = event.currentTarget.value !== 'webhook';
});

$('#hook-form').elements.response_mode.addEventListener('change', (event) => {
  $('#hook-response-template-field').hidden = event.currentTarget.value !== 'sync';
});

$('#hook-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  $('#hook-error').textContent = '';
  const editingHook = form.name.disabled ? findHook(form.name.value) : null;
  try {
    const body = {
      kind: form.kind.value,
      name: form.name.value.trim(),
      description: form.description.value.trim(),
      dedupe_path: form.dedupe_path.value.trim(),
      enabled: form.enabled.checked,
    };
    if (body.kind === 'telegram') {
      body.connection_id = form.connection_id.value.trim();
    } else {
      if (body.kind === 'webhook') {
        const secret = form.secret.value.trim();
        if (form.clear_secret.checked) body.secret = '';
        else if (secret) body.secret = secret; // omitted: keep the stored lock
        // the header rides with the lock (blank = the x-dapier-signature
        // default); a signed hook round-trips its stored name here
        if (secret || form.clear_secret.checked || form.signature_header.value.trim()) {
          body.signature_header = form.signature_header.value.trim().toLowerCase();
        }
      }
      if (body.kind === 'mailchimp') body.list_id = form.list_id.value.trim();
      const response = { mode: form.response_mode.value };
      if (response.mode === 'sync' && form.response_template.value.trim()) {
        try { response.template = JSON.parse(form.response_template.value); } catch (_) { throw new Error('Response template must be valid JSON'); }
      }
      body.response = response;
    }
    if (editingHook && editingHook.flow) {
      body.flow = editingHook.flow;
    } else {
      let actions;
      try { actions = JSON.parse(form.actions.value); } catch (_) { throw new Error('Actions must be valid JSON'); }
      if (!Array.isArray(actions)) throw new Error('Actions must be a JSON list');
      body.actions = actions;
    }
    const saved = await api('/api/admin/hook-triggers', { method: 'PUT', body: JSON.stringify(body) });
    $('#hook-dialog').close();
    const warnings = saved.warnings || [];
    notice(`Saved hook ${body.name}. It is live immediately.`
      + (warnings.length ? ` Mailchimp warning: ${warnings.join(' ')}` : ''));
    await fetchTriggers();
  } catch (error) {
    $('#hook-error').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

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
    if (edit.dataset.kind === 'hook') openHookDialog(findHook(edit.dataset.name));
    else openPollDialog(findPoll(edit.dataset.name));
    return;
  }
  const sample = event.target.closest('.trigger-sample');
  if (sample && !sample.disabled) void pullSample(sample.dataset.name);
  const button = event.target.closest('.trigger-delete');
  if (!button || button.disabled) return;
  const isHook = button.dataset.kind === 'hook';
  const item = isHook ? findHook(button.dataset.name) : findPoll(button.dataset.name);
  if (!item) return;
  const name = isHook ? item.hook_id : item.poll_id;
  const message = isHook
    ? (item.kind === 'telegram'
      ? 'The bot stops forwarding updates and the trigger is removed.'
      : item.kind === 'mailchimp'
        ? 'Its webhook subscription is removed from the Mailchimp audience and the trigger is deleted.'
        : 'Callers using the hook URL are rejected and the token stops working.')
    : 'Its EventBridge rule is removed and the API is no longer polled.';
  if (!await confirmDelete(`Delete ${isHook ? `${item.kind || 'hook'} hook` : 'poll trigger'} ${name}?`, message)) return;
  button.disabled = true;
  try {
    const query = `name=${encodeURIComponent(name)}${isHook ? `&kind=${encodeURIComponent(item.kind || 'webhook')}` : ''}`;
    await api(`/api/admin/${isHook ? 'hook' : 'poll'}-triggers?${query}`, { method: 'DELETE' });
    notice(`Deleted ${isHook ? 'hook' : 'poll trigger'} ${name}.`);
    await fetchTriggers();
  } catch (error) {
    notice(error.message, true);
    button.disabled = false;
  }
});

/* Entering the view (nav click, back/forward) fetches fresh triggers. */
document.addEventListener('click', (event) => {
  if (event.target.closest('.nav-item[data-view="triggers"]')) void fetchTriggers();
});
window.addEventListener('popstate', () => {
  if (state.view === 'triggers') void fetchTriggers();
});
