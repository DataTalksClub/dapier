/* Hooks tab — a webhook console over the same operator endpoints the
   `dapier hooks` CLI drives:
     GET  /api/admin/hook-triggers                 endpoints (+ workflows each starts)
     GET  /api/admin/hook-triggers/deliveries      recent deliveries + outcomes
     GET  /api/admin/hook-triggers/deliveries/<id> one delivery: headers, body, runs
     POST /api/admin/hook-triggers/test            Send test request
     POST /api/admin/triggers/inbox/<id>/replay    Replay (the trigger inbox)
     PUT/DELETE /api/admin/hook-triggers           create / edit / delete
   Recent deliveries are the main content; each endpoint (URL, how requests
   are verified, which workflows run) sits beside them as compact cards. */
import { state } from '../state.js';
import { $, icons, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp, jsonBlock } from '../format.js';
import { openRun } from './runs.js';

const PAGE = 50;
const TEST_SAMPLE = { event: 'test', message: 'Hello from Dapier', sent_by: 'Send test request' };

let hooks = [];
let deliveries = [];
let healthRows = []; // the latest unfiltered page: endpoint health ignores the chip filter
let nextToken = null;
let filter = ''; // hook name, '' = every hook
let fetchingHooks = false;
let fetchingDeliveries = false;
let loaded = false;
let openDeliveryId = null;

const findHook = (name) => hooks.find((item) => item.hook_id === name);

function onHooksTab() {
  return state.view === 'workflows' && !$('[data-workflow-panel="hooks"]').hidden;
}

/* --- plain-language bits ---------------------------------------------------- */

const OUTCOME_TONE = {
  ran: 'ok', run_failed: 'err', failed: 'err', running: 'run', processing: 'run',
  no_workflow: 'warn', filtered: 'off', ignored: 'off',
};

function outcomeLine(row) {
  const tone = OUTCOME_TONE[row.outcome] || 'off';
  let text = escapeHtml(row.outcome_text || row.outcome || '—');
  // Workflow names in the sentence link to the workflow.
  (row.matched || []).forEach((id) => {
    const safe = escapeHtml(id);
    text = text.replace(safe, `<a class="hook-flow-link" href="/workflows/${encodeURIComponent(id)}">${safe}</a>`);
  });
  return `<span class="status ${tone} hook-outcome"><span class="status-dot" aria-hidden="true"></span><span>${text}</span></span>`;
}

function verification(hook) {
  if (hook.kind === 'telegram') return 'Telegram secret token, managed for you';
  if (hook.kind === 'mailchimp') return 'Unguessable URL (Mailchimp sends no credentials)';
  if (hook.kind === 'youtube') return 'Hub signature on the shared YouTube callback';
  if (hook.signed) return `Signed: HMAC-SHA256 of the body in ${hook.signature_header || 'x-dapier-signature'}`;
  return 'Bearer token in the authorization header';
}

function formatSize(bytes) {
  if (bytes == null) return '—';
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
}

function responseLabel(row) {
  if (row.response_status) return `<span class="mono">${escapeHtml(row.response_status)}</span>`;
  // Telegram and Mailchimp answers are fixed by the intake; only webhook
  // requests record theirs.
  return '<span class="muted-cell">—</span>';
}

/* --- endpoints --------------------------------------------------------------- */

function healthFor(hookId) {
  const rows = healthRows.filter((row) => row.hook === hookId);
  if (!rows.length) return { text: 'No recent deliveries', tone: 'off' };
  const failed = rows.filter((row) => row.outcome === 'run_failed' || row.outcome === 'failed').length;
  const last = formatTimestamp(rows[0].received_at) || '—';
  const count = `${rows.length} recent`;
  return failed
    ? { text: `Last ${last} · ${failed} of ${count} failed`, tone: 'err' }
    : { text: `Last ${last} · ${count}, all fine`, tone: 'ok' };
}

function workflowsLine(hook) {
  if (!('workflows' in hook)) return '<span class="muted-cell">—</span>';
  const flows = hook.workflows || [];
  if (!flows.length) return '<span class="status warn"><span class="status-dot" aria-hidden="true"></span>No workflow — deliveries are logged, nothing runs</span>';
  return flows.map((flow) => {
    const notes = [!flow.enabled && 'off', flow.conditional && 'when its filters pass', flow.any_hook && 'any hook'].filter(Boolean);
    return `<a class="hook-flow-link mono" href="/workflows/${encodeURIComponent(flow.id)}">${escapeHtml(flow.id)}</a>${notes.length ? ` <span class="muted-cell">(${escapeHtml(notes.join(', '))})</span>` : ''}`;
  }).join('<br>');
}

function endpointCard(hook) {
  const health = healthFor(hook.hook_id);
  const name = escapeHtml(hook.hook_id);
  const selected = filter === hook.hook_id;
  return `<li class="hook-endpoint${selected ? ' is-selected' : ''}" data-hook="${name}">
    <div class="hook-endpoint-head">
      <button class="hook-endpoint-name mono" type="button" data-hook-filter="${name}" aria-pressed="${selected}" title="Show only ${name}'s deliveries">${name}</button>
      <span class="hook-kind">${escapeHtml(hook.kind || 'webhook')}</span>
      ${hook.enabled ? '' : statusLine('disabled')}
    </div>
    ${hook.description ? `<p class="hook-endpoint-desc">${escapeHtml(hook.description)}</p>` : ''}
    <div class="hook-url"><code>${escapeHtml(hook.url || '')}</code><button class="icon-button hook-copy" type="button" data-copy="${escapeHtml(hook.url || '')}" aria-label="Copy ${name} URL" title="Copy URL"><i data-lucide="copy"></i></button></div>
    <dl class="hook-facts">
      <div><dt>Verified by</dt><dd>${escapeHtml(verification(hook))}</dd></div>
      <div><dt>Starts</dt><dd>${workflowsLine(hook)}</dd></div>
      <div><dt>Health</dt><dd><span class="status ${health.tone}"><span class="status-dot" aria-hidden="true"></span>${escapeHtml(health.text)}</span></dd></div>
    </dl>
    <div class="hook-endpoint-actions">
      ${(hook.kind || 'webhook') === 'webhook' && hook.enabled ? `<button class="dk-button dk-button--sm dk-button--secondary hook-test" type="button" data-hook="${name}">Send test</button>` : ''}
      <button class="dk-button dk-button--sm dk-button--secondary hook-edit" type="button" data-hook="${name}">Edit</button>
      <button class="dk-button dk-button--sm dk-button--secondary hook-delete" type="button" data-hook="${name}">Delete</button>
    </div>
  </li>`;
}

/* --- deliveries -------------------------------------------------------------- */

function deliveryRow(row) {
  const id = escapeHtml(row.delivery_id);
  const badges = [row.test && '<span class="hook-badge">test</span>', row.replay && '<span class="hook-badge">replay</span>'].filter(Boolean).join('');
  return `<tr class="hook-delivery-open" data-delivery="${id}" role="button" tabindex="0" aria-label="Open delivery to ${escapeHtml(row.hook || '')}">
    <td class="cell-title"><span class="cell-name mono">${escapeHtml(row.hook || '—')}${badges}</span><span class="cell-sub mono">${escapeHtml(formatTimestamp(row.received_at) || '—')}</span></td>
    <td data-label="Outcome">${outcomeLine(row)}</td>
    <td data-label="Response">${responseLabel(row)}</td>
    <td class="mono muted-cell" data-label="Size">${escapeHtml(formatSize(row.size_bytes))}</td>
  </tr>`;
}

function renderFilter() {
  const names = hooks.filter((hook) => hook.kind !== 'youtube').map((hook) => hook.hook_id);
  const chip = (value, label) => `<button class="hook-chip${filter === value ? ' is-active' : ''}" type="button" data-hook-filter="${escapeHtml(value)}" aria-pressed="${filter === value}">${escapeHtml(label)}</button>`;
  $('#hooks-filter').innerHTML = chip('', 'All hooks') + names.map((name) => chip(name, name)).join('');
}

function renderSummary() {
  const shown = deliveries.length;
  const failed = deliveries.filter((row) => row.outcome === 'run_failed' || row.outcome === 'failed').length;
  const unmatched = deliveries.filter((row) => row.outcome === 'no_workflow').length;
  const parts = [`${shown}${nextToken ? '+' : ''} ${shown === 1 ? 'delivery' : 'deliveries'}`];
  if (failed) parts.push(`${failed} failed`);
  if (unmatched) parts.push(`${unmatched} started nothing`);
  $('#hooks-summary').textContent = loaded ? parts.join(' · ') : 'Loading…';
}

function render() {
  const any = hooks.length > 0;
  $('#hook-empty').hidden = any || !loaded;
  $('#hooks-layout').hidden = !any;
  if (!any) return;
  renderFilter();
  renderSummary();
  $('#hook-endpoints').innerHTML = hooks.map(endpointCard).join('');
  $('#hook-deliveries').innerHTML = deliveries.map(deliveryRow).join('');
  const empty = loaded && !deliveries.length;
  $('#hook-deliveries-empty').hidden = !empty;
  $('#hook-deliveries-wrap').hidden = empty;
  $('#hook-deliveries-empty-text').textContent = filter
    ? `Nothing has called ${filter} in the last 30 days. Send a test request to see one arrive.`
    : 'No hook has been called in the last 30 days. Send a test request to see one arrive.';
  $('#hook-deliveries-more').hidden = !nextToken;
  icons();
}

async function fetchHooks() {
  if (fetchingHooks) return;
  fetchingHooks = true;
  try {
    hooks = (await api('/api/admin/hook-triggers')).hooks || [];
  } catch (error) {
    notice(error.message, true);
  } finally {
    fetchingHooks = false;
  }
}

async function fetchDeliveries({ append = false } = {}) {
  if (fetchingDeliveries) return;
  fetchingDeliveries = true;
  try {
    const params = new URLSearchParams({ limit: String(PAGE) });
    if (filter) params.set('hook', filter);
    if (append && nextToken) params.set('next', nextToken);
    const data = await api(`/api/admin/hook-triggers/deliveries?${params}`);
    const fresh = data.deliveries || [];
    deliveries = append ? [...deliveries, ...fresh] : fresh;
    if (!filter) healthRows = deliveries;
    nextToken = (data.paging || {}).next || null;
  } catch (error) {
    if (!append) { deliveries = []; nextToken = null; }
    notice(error.message, true);
  } finally {
    fetchingDeliveries = false;
    loaded = true;
  }
}

export async function refreshHooks() {
  await Promise.all([fetchHooks(), fetchDeliveries()]);
  render();
}

/* Called from the Workflows family refresh: fetch while the Hooks tab is
   on screen, otherwise just repaint the cache. */
export function renderHooks() {
  if (onHooksTab()) return void refreshHooks();
  if (loaded) render();
}

/* --- delivery detail ---------------------------------------------------------- */

function headersBlock(headers) {
  const entries = Object.entries(headers || {}).sort(([a], [b]) => a.localeCompare(b));
  if (!entries.length) return '<p class="detail-muted">Not recorded for this delivery (Telegram and Mailchimp requests, and deliveries from before the log kept headers).</p>';
  return `<dl class="hook-headers">${entries.map(([key, value]) =>
    `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`).join('')}</dl>`;
}

function bodyBlock(body, truncated) {
  if (body === undefined || body === null) return '<p class="detail-muted">Empty body.</p>';
  const text = JSON.stringify(body, null, 2) || '';
  const long = text.split('\n').length > 24 || text.length > 2400;
  return `<div class="hook-body${long ? ' is-clamped' : ''}">${jsonBlock(body)}</div>
    ${long ? '<button class="dk-button dk-button--sm dk-button--secondary hook-body-toggle" type="button" aria-expanded="false">Show full body</button>' : ''}
    ${truncated ? '<p class="field-hint">The stored copy was cut to keep the log small; replay still sends the original when it can.</p>' : ''}`;
}

function runsBlock(row) {
  const runs = row.runs || [];
  if (!runs.length) {
    return (row.matched || []).length
      ? '<p class="detail-muted">The run is not in history yet.</p>'
      : '<p class="detail-muted">No workflow ran for this delivery.</p>';
  }
  return `<ul class="hook-runs">${runs.map((run) => `<li>
      <a class="hook-flow-link mono" href="/workflows/${encodeURIComponent(run.workflow_id)}">${escapeHtml(run.workflow_id)}</a>
      ${statusLine(run.status)}
      <button class="dk-button dk-button--sm dk-button--secondary hook-open-run" type="button" data-run="${escapeHtml(run.run_id)}">Open run</button>
      ${run.error ? `<span class="hook-run-error">${escapeHtml(run.error)}</span>` : ''}
    </li>`).join('')}</ul>`;
}

export async function openDelivery(deliveryId) {
  try {
    const row = (await api(`/api/admin/hook-triggers/deliveries/${encodeURIComponent(deliveryId)}`)).delivery || {};
    openDeliveryId = deliveryId;
    $('#hook-delivery-title').textContent = `Delivery to ${row.hook || 'hook'}`;
    const facts = [
      ['Received', formatTimestamp(row.received_at)],
      ['Response', row.response_status],
      ['Size', formatSize(row.size_bytes)],
      ['Content type', row.content_type],
      ['Event', row.kind && row.event ? `${row.kind} · ${row.event}` : null],
      ['Delivery id', row.delivery_id],
    ].filter(([, value]) => value !== null && value !== undefined && value !== '');
    $('#hook-delivery-detail').innerHTML = `
      <div class="hook-delivery-summary">${outcomeLine(row)}${row.test ? '<span class="hook-badge">test request</span>' : ''}</div>
      ${row.error ? `<p class="dialog-feedback error">${escapeHtml(row.error)}</p>` : ''}
      <dl class="detail-list">${facts.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd><pre>${escapeHtml(String(value))}</pre></dd></div>`).join('')}</dl>
      <section><h3 class="hook-section-title">Workflow runs</h3>${runsBlock(row)}</section>
      <section><h3 class="hook-section-title">Request headers</h3>${headersBlock(row.headers)}</section>
      ${row.query && Object.keys(row.query).length ? `<section><h3 class="hook-section-title">Query</h3>${jsonBlock(row.query)}</section>` : ''}
      <section><h3 class="hook-section-title">Body</h3>${bodyBlock(row.body, row.truncated)}</section>`;
    const dialog = $('#hook-delivery-dialog');
    if (!dialog.open) dialog.showModal();
    icons();
  } catch (error) {
    notice(error.message, true);
  }
}

function confirmReplay(deliveryId) {
  const dialog = $('#inbox-replay-confirm-dialog');
  $('#inbox-replay-confirm-message').textContent =
    'Replay this delivery? It goes through the workflows again with a fresh id; their actions may send messages or change external data a second time.';
  dialog.returnValue = '';
  dialog.showModal();
  dialog.addEventListener('close', async () => {
    if (dialog.returnValue !== 'confirm') return;
    try {
      const data = await api(`/api/admin/triggers/inbox/${encodeURIComponent(deliveryId)}/replay`, { method: 'POST', body: '{}' });
      notice(data.event_id ? `Replay queued as delivery ${data.event_id}.` : 'Replay queued.');
      $('#hook-delivery-dialog').close();
      await refreshHooks();
    } catch (error) {
      notice(error.message, true);
    }
  }, { once: true });
}

/* --- send test request -------------------------------------------------------- */

let lastTestDelivery = null;

function testTarget(name) {
  const hook = findHook(name);
  $('#hook-test-target').textContent = hook
    ? `POST ${hook.url} · ${hook.signed ? 'signed with the hook secret' : 'with the hook bearer token'}`
    : '';
}

function openTest(name) {
  const webhooks = hooks.filter((hook) => (hook.kind || 'webhook') === 'webhook' && hook.enabled);
  if (!webhooks.length) {
    notice('Send test request works on enabled webhook hooks; create one first.', true);
    return;
  }
  const form = $('#hook-test-form');
  form.name.innerHTML = webhooks.map((hook) => `<option value="${escapeHtml(hook.hook_id)}">${escapeHtml(hook.hook_id)}</option>`).join('');
  const preferred = name || filter;
  form.name.value = webhooks.some((hook) => hook.hook_id === preferred) ? preferred : webhooks[0].hook_id;
  if (!form.data.value.trim()) form.data.value = JSON.stringify(TEST_SAMPLE, null, 2);
  testTarget(form.name.value);
  $('#hook-test-error').textContent = '';
  $('#hook-test-result').hidden = true;
  $('#hook-test-view').hidden = true;
  lastTestDelivery = null;
  $('#hook-test-dialog').showModal();
}

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

/* The queue hands the delivery to the worker a moment after the 202;
   poll the log briefly so the dialog can say what the workflow did. */
async function followDelivery(deliveryId) {
  for (let attempt = 0; attempt < 8; attempt += 1) {
    await sleep(attempt < 3 ? 800 : 1500);
    try {
      const row = (await api(`/api/admin/hook-triggers/deliveries/${encodeURIComponent(deliveryId)}`)).delivery;
      if (row && row.outcome !== 'processing' && row.outcome !== 'running') return row;
    } catch (_) { /* not recorded yet */ }
  }
  return null;
}

$('#hook-test-form').elements.name.addEventListener('change', (event) => testTarget(event.currentTarget.value));

$('#hook-test-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  $('#hook-test-error').textContent = '';
  let data;
  try {
    data = form.data.value.trim() ? JSON.parse(form.data.value) : undefined;
  } catch (_) {
    $('#hook-test-error').textContent = 'Payload must be valid JSON.';
    return;
  }
  submit.disabled = true;
  const result = $('#hook-test-result');
  result.hidden = false;
  result.innerHTML = '<span class="status run"><span class="status-dot" aria-hidden="true"></span>Sending…</span>';
  $('#hook-test-view').hidden = true;
  try {
    const sent = await api('/api/admin/hook-triggers/test', {
      method: 'POST',
      body: JSON.stringify({ name: form.name.value, ...(data === undefined ? {} : { data }) }),
    });
    const response = sent.response || {};
    const ok = response.status >= 200 && response.status < 300;
    lastTestDelivery = sent.delivery_id || null;
    result.innerHTML = `<p><span class="status ${ok ? 'ok' : 'err'}"><span class="status-dot" aria-hidden="true"></span>Answered ${escapeHtml(response.status)}</span></p>
      ${jsonBlock(response.body)}
      ${lastTestDelivery ? '<p class="hook-test-follow sub">Waiting for the workflow…</p>' : ''}`;
    if (lastTestDelivery) {
      $('#hook-test-view').hidden = false;
      const row = await followDelivery(lastTestDelivery);
      const follow = result.querySelector('.hook-test-follow');
      if (follow) follow.innerHTML = row ? outcomeLine(row) : 'Still processing; it will show in Recent deliveries.';
    }
    await refreshHooks();
  } catch (error) {
    result.hidden = true;
    $('#hook-test-error').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

$('#hook-test-view').addEventListener('click', () => {
  if (!lastTestDelivery) return;
  $('#hook-test-dialog').close();
  void openDelivery(lastTestDelivery);
});

/* --- create / edit / delete --------------------------------------------------- */

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
  $('#hook-connection-field').hidden = form.kind.value !== 'telegram';
  $('#hook-list-field').hidden = form.kind.value !== 'mailchimp';
  $('#hook-dialog').showModal();
  if (!hook) form.name.focus();
}

$('#new-hook').addEventListener('click', () => openHookDialog(null));
$('#hook-send-test').addEventListener('click', () => openTest(null));

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
    const saved = await api('/api/admin/hook-triggers', { method: 'PUT', body: JSON.stringify(body) });
    $('#hook-dialog').close();
    const warnings = saved.warnings || [];
    notice(`Saved hook ${body.name}. It is live immediately.`
      + (warnings.length ? ` Mailchimp warning: ${warnings.join(' ')}` : ''));
    await refreshHooks();
  } catch (error) {
    $('#hook-error').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

function confirmDelete(title, message) {
  const dialog = $('#trigger-confirm-dialog');
  $('#trigger-confirm-title').textContent = title;
  $('#trigger-confirm-message').textContent = message;
  dialog.returnValue = '';
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true }));
}

async function deleteHook(button) {
  const hook = findHook(button.dataset.hook);
  if (!hook) return;
  const message = hook.kind === 'telegram'
    ? 'The bot stops forwarding updates and the trigger is removed.'
    : hook.kind === 'mailchimp'
      ? 'Its webhook subscription is removed from the Mailchimp audience and the trigger is deleted.'
      : 'Callers using the hook URL are rejected and the token stops working.';
  if (!await confirmDelete(`Delete ${hook.kind || 'webhook'} hook ${hook.hook_id}?`, message)) return;
  button.disabled = true;
  try {
    await api(`/api/admin/hook-triggers?name=${encodeURIComponent(hook.hook_id)}&kind=${encodeURIComponent(hook.kind || 'webhook')}`, { method: 'DELETE' });
    notice(`Deleted hook ${hook.hook_id}.`);
    if (filter === hook.hook_id) filter = '';
    await refreshHooks();
  } catch (error) {
    notice(error.message, true);
    button.disabled = false;
  }
}

/* --- events ------------------------------------------------------------------- */

async function copyUrl(button) {
  try {
    await navigator.clipboard.writeText(button.dataset.copy);
    button.innerHTML = '<i data-lucide="check"></i>';
    icons();
    setTimeout(() => { button.innerHTML = '<i data-lucide="copy"></i>'; icons(); }, 1600);
  } catch (_) {
    notice('Copy failed; select the URL and copy it by hand.', true);
  }
}

function setFilter(value) {
  filter = filter === value && value ? '' : value;
  deliveries = [];
  nextToken = null;
  loaded = false;
  render();
  void fetchDeliveries().then(render);
}

$('[data-workflow-panel="hooks"]').addEventListener('click', (event) => {
  const target = event.target;
  const filterButton = target.closest('[data-hook-filter]');
  if (filterButton) return void setFilter(filterButton.dataset.hookFilter);
  const copy = target.closest('.hook-copy');
  if (copy) return void copyUrl(copy);
  const test = target.closest('.hook-test');
  if (test) return void openTest(test.dataset.hook);
  const edit = target.closest('.hook-edit');
  if (edit) return void openHookDialog(findHook(edit.dataset.hook));
  const del = target.closest('.hook-delete');
  if (del && !del.disabled) return void deleteHook(del);
  if (target.closest('a')) return undefined; // workflow links navigate
  const row = target.closest('.hook-delivery-open');
  if (row) void openDelivery(row.dataset.delivery);
  return undefined;
});

$('#hook-deliveries').addEventListener('keydown', (event) => {
  const row = event.target.closest('.hook-delivery-open');
  if (row && (event.key === 'Enter' || event.key === ' ')) {
    event.preventDefault();
    void openDelivery(row.dataset.delivery);
  }
});

$('#hook-deliveries-more').addEventListener('click', async () => {
  await fetchDeliveries({ append: true });
  render();
});

$('#hook-delivery-dialog').addEventListener('click', (event) => {
  const toggle = event.target.closest('.hook-body-toggle');
  if (toggle) {
    const body = toggle.previousElementSibling;
    const open = body.classList.toggle('is-clamped') === false;
    toggle.textContent = open ? 'Show less' : 'Show full body';
    toggle.setAttribute('aria-expanded', String(open));
    return;
  }
  const run = event.target.closest('.hook-open-run');
  if (run) void openRun(run.dataset.run);
});

$('#hook-delivery-replay').addEventListener('click', () => {
  if (openDeliveryId) confirmReplay(openDeliveryId);
});

/* Entering the tab (page tabs, nav, back/forward) fetches fresh data. */
document.addEventListener('click', (event) => {
  if (event.target.closest('[data-view="workflows"], [data-target="workflows"]')) {
    setTimeout(() => { if (onHooksTab()) void refreshHooks(); }, 0);
  }
});
window.addEventListener('popstate', () => {
  setTimeout(() => { if (onHooksTab()) void refreshHooks(); }, 0);
});
