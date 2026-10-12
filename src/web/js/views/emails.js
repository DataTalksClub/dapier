/* Emails is a mailbox of what Dapier received and what happened to each
   message: handled by a workflow, refused by the sender policy, or matched
   by nothing. The receiving addresses sit beside it; selecting one binds
   the "Who can write" panel to that address's sender policy (or the
   default). Rows come from the trigger inbox
   (GET /api/admin/triggers/inbox?connector=email), the same API behind
   `dapier emails received`; flows are still edited only in the designer. */
import { state } from '../state.js';
import { $, $$, icons, notice, whenMenusClosed } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, formatTimestamp } from '../format.js';
import { refresh } from './overview.js';
import { openDesigner } from './designer.js';
import { openRun } from './runs.js';
import { openReplayConfirm } from './inbox.js';
import { workflowName } from '../workflow-names.js';
import { filterChips, whenHtml, workflowNames, outcomeCell, activityRow, sourceRow } from './activity.js';

/* Server-side outcome filters; Handled includes the runs that failed —
   a workflow took the message, and its failure is what needs attention. */
const FILTERS = {
  all: '',
  handled: 'handled,pending',
  failed: 'failed',
  refused: 'refused',
  none: 'unmatched',
};
const CHIPS = [['all', 'All'], ['handled', 'Handled'], ['failed', 'Failed'], ['refused', 'Refused'], ['none', 'No workflow']];
const mail = { events: null, next: null, filter: 'all', loading: false, error: '' };
let config = {};
let fromPolicy = { default: { mode: 'allowlist', addresses: [] }, routes: {}, addresses: [] };
let selectedKey = null;

/* ---- formatting ------------------------------------------------------- */

function senderName(email) {
  const header = email.from || '';
  const match = header.match(/^\s*"?([^"<]+?)"?\s*<[^>]+>\s*$/);
  return (match && match[1]) || email.from_address || header || 'Unknown sender';
}

function formatSize(bytes) {
  if (bytes == null) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function isFeedback(event) {
  return Boolean((event.email || {}).feedback);
}

/* The workflows bound to an address in the current configuration — used
   to point a failed message at the run that failed, which the inbox row
   cannot name (the failure closes the row before matching is recorded). */
function handlersFor(event) {
  const to = ((event.email || {}).to || []).map((value) => value.toLowerCase());
  return (config.addresses || [])
    .filter((address) => to.includes(String(address.address).toLowerCase()))
    .flatMap((address) => (address.handlers || []).map((handler) => handler.workflow));
}

/* The outcome in the family vocabulary (activity.js). */
const OUTCOME_KEY = { handled: 'handled', failed: 'failed', refused: 'refused', unmatched: 'none', pending: 'processing' };

function outcomeHtml(event) {
  const key = OUTCOME_KEY[event.outcome] || 'ignored';
  const runs = (event.runs || []).map((run) => run.workflow);
  if (key === 'handled' && runs.length) return outcomeCell(key, { by: workflowNames(runs) });
  if (key === 'failed') return outcomeCell(key, { by: runs.length ? workflowNames(runs) : '', error: event.error || '' });
  if (key === 'refused') return outcomeCell(key, { note: 'Sender not allowed' });
  if (key === 'none') return outcomeCell(key, { note: isFeedback(event) ? 'No workflow watches feedback' : 'No workflow for this address' });
  return outcomeCell(key);
}

/* ---- received list ---------------------------------------------------- */

function mailRow(event) {
  const email = event.email || {};
  const feedback = isFeedback(event);
  const from = feedback ? `${email.feedback === 'bounce' ? 'Bounce' : 'Complaint'} report` : senderName(email);
  const subject = feedback
    ? `${email.feedback === 'bounce' ? 'Bounced' : 'Complaint'}: ${(email.recipients || []).join(', ') || 'unknown recipient'}`
    : (email.subject || '(no subject)');
  const to = (email.to || []).join(', ');
  const attachments = (email.attachments || []).filter((item) => !item.inline).length;
  return activityRow({
    attrs: `data-inbox="${escapeHtml(event.inbox_id)}" data-outcome="${escapeHtml(event.outcome || '')}"`,
    label: `Open ${subject}`,
    when: whenHtml(event.received_at),
    primary: `<span title="${escapeHtml(email.from || '')}">${escapeHtml(from)}</span>${to ? `<span class="activity-to mono" title="${escapeHtml(to)}">to ${escapeHtml(to)}</span>` : ''}`,
    secondary: `${escapeHtml(subject)}${attachments ? ` · ${attachments} attachment${attachments === 1 ? '' : 's'}` : ''}`,
    outcome: outcomeHtml(event),
  });
}

function emptyMessage() {
  const first = (config.addresses || [])[0];
  if (mail.filter === 'refused') return '<h3>Nothing refused</h3><p>Mail from senders outside the allowed list shows up here.</p>';
  if (mail.filter === 'none') return '<h3>Nothing unmatched</h3><p>Every message reached a workflow.</p>';
  if (mail.filter === 'failed') return '<h3>Nothing failed</h3><p>Messages whose workflow run failed show up here.</p>';
  if (mail.filter === 'handled') return '<h3>Nothing handled yet</h3><p>Messages a workflow ran for show up here.</p>';
  if (first) {
    return `<h3>No email received yet</h3><p>Send a message to <span class="mono">${escapeHtml(first.address)}</span> from an allowed sender — it shows up here within seconds.</p>`;
  }
  return '<h3>No email received yet</h3><p>Add an email trigger with an address filter in <a class="text-link view-link" href="/workflows" data-target="workflows">Workflows</a>, then send a message to that address.</p>';
}

function renderMail() {
  const events = mail.events || [];
  const loaded = mail.events !== null;
  $('#email-mail-list').innerHTML = events.map(mailRow).join('');
  $('#email-mail-list').hidden = events.length === 0;
  $('#email-mail-empty').hidden = !loaded || events.length > 0 || Boolean(mail.error);
  $('#email-mail-empty').innerHTML = emptyMessage();
  const note = $('#email-mail-note');
  note.textContent = mail.error || (loaded ? '' : 'Loading received email…');
  note.classList.toggle('form-error', Boolean(mail.error));
  note.hidden = !note.textContent;
  $('#email-mail-footer').hidden = !mail.next;
  $('#email-filter-chips').innerHTML = filterChips(CHIPS, mail.filter, 'email-filter');
  $('#email-mail-summary').textContent = loaded
    ? `${events.length}${mail.next ? '+' : ''} message${events.length === 1 ? '' : 's'}` : '';
}

export async function fetchMail({ append = false } = {}) {
  if (mail.loading) return;
  mail.loading = true;
  try {
    const params = new URLSearchParams({ connector: 'email', limit: '25' });
    if (FILTERS[mail.filter]) params.set('outcome', FILTERS[mail.filter]);
    if (append && mail.next) params.set('next', mail.next);
    const data = await api(`/api/admin/triggers/inbox?${params}`);
    const fresh = data.events || [];
    mail.events = append && mail.events ? [...mail.events, ...fresh] : fresh;
    mail.next = (data.paging || {}).next || null;
    mail.error = '';
  } catch (error) {
    mail.error = `Could not load received email: ${error.message}`;
    if (!append) mail.next = null;
  } finally {
    mail.loading = false;
  }
  renderMail();
}

/* Called from the overview refresh: fetch while the page is on screen. */
export function renderReceivedEmail() {
  if (state.view === 'emails') void fetchMail();
  else renderMail();
}

/* ---- detail dialog ---------------------------------------------------- */

function detailHtml(event) {
  const email = event.email || {};
  const runs = event.runs || [];
  const rows = [
    ['From', email.from],
    ['To', (email.to || []).join(', ')],
    ['Cc', email.cc],
    ['Date', email.date],
    ['Received', formatTimestamp(event.received_at)],
    ['Message-ID', email.message_id],
  ];
  if (isFeedback(event)) {
    rows.unshift(['Feedback', `${email.feedback}${email.bounce_type ? ` (${email.bounce_type})` : ''}`],
      ['Recipients', (email.recipients || []).join(', ')]);
  }
  const header = `<dl class="detail-list email-headers">${rows.filter(([, value]) => value)
    .map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd class="mono">${escapeHtml(value)}</dd></div>`).join('')}</dl>`;
  const attachments = email.attachments || [];
  const files = attachments.length ? `<section class="email-detail-block">
      <h3>Attachments <span class="email-muted">${attachments.length}</span></h3>
      <ul class="email-files">${attachments.map((item) => `<li><i data-lucide="file-text"></i><span class="mono">${escapeHtml(item.name || '(unnamed)')}</span><span class="email-muted">${escapeHtml([formatSize(item.size), item.inline ? 'inline' : ''].filter(Boolean).join(' · '))}</span></li>`).join('')}</ul>
    </section>` : '';
  let result;
  if (runs.length) {
    result = `<ul class="email-runs">${runs.map((run) => `<li>
        <span class="cell-name" title="${escapeHtml(run.workflow)}">${escapeHtml(workflowName(run.workflow))}</span>
        ${run.status ? statusLine(run.status) : '<span class="email-muted">status unknown</span>'}
        <button type="button" class="dk-button dk-button--secondary email-run" data-run="${escapeHtml(run.run_id)}">Open run</button>
      </li>`).join('')}</ul>`;
  } else if (event.outcome === 'failed') {
    const candidates = handlersFor(event);
    result = `<p class="detail-error">${escapeHtml(event.error || 'The run failed.')}</p>${candidates.length
      ? `<ul class="email-runs">${candidates.map((workflow) => `<li><span class="cell-name" title="${escapeHtml(workflow)}">${escapeHtml(workflowName(workflow))}</span><span class="email-muted">handles this address</span><button type="button" class="dk-button dk-button--secondary email-run" data-run="${escapeHtml(`${workflow}:${event.inbox_id}`)}">Open run</button></li>`).join('')}</ul>` : ''}`;
  } else if (event.outcome === 'refused') {
    const sender = email.from_address;
    result = `<p class="sub">${sender ? `<span class="mono">${escapeHtml(sender)}</span> is not` : 'The sender is not'} allowed to write to this address, so no workflow ran. Allow the sender, then replay the message to run it now.</p>`;
  } else if (event.outcome === 'unmatched') {
    result = `<p class="sub">${isFeedback(event)
      ? 'No workflow watches bounce or complaint feedback.'
      : 'No published workflow has an email trigger for this address.'} Add one in Workflows, then replay the message.</p>`;
  } else {
    result = '';
  }
  return `<section class="email-detail-block">
      <h3>Outcome</h3>
      <div class="activity-outcome">${outcomeHtml(event)}</div>
      ${result}
    </section>
    ${header}
    ${files}`;
}

let openInboxId = null;

async function openEmail(inboxId) {
  openInboxId = inboxId;
  const dialog = $('#email-dialog');
  const row = (mail.events || []).find((event) => event.inbox_id === inboxId) || {};
  $('#email-dialog-title').textContent = (row.email || {}).subject || 'Email';
  $('#email-dialog-body').innerHTML = row.inbox_id ? detailHtml(row) : '<p class="detail-muted">Loading…</p>';
  $('#email-allow-sender').hidden = true;
  icons();
  if (!dialog.open) dialog.showModal();
  let event;
  try {
    event = (await api(`/api/admin/triggers/inbox/${encodeURIComponent(inboxId)}`)).event || {};
  } catch (error) {
    if (!row.inbox_id) $('#email-dialog-body').innerHTML = `<p class="detail-muted">${escapeHtml(error.message)}</p>`;
    return;
  }
  if (openInboxId !== inboxId) return;
  const email = event.email || {};
  $('#email-dialog-title').textContent = isFeedback(event)
    ? `${email.feedback === 'bounce' ? 'Bounce' : 'Complaint'} report`
    : (email.subject || '(no subject)');
  $('#email-dialog-body').innerHTML = detailHtml(event);
  const sender = (email.from_address || '').toLowerCase();
  const route = (email.route || '').toLowerCase();
  const allow = $('#email-allow-sender');
  allow.hidden = !(event.outcome === 'refused' && sender && !senderAllowed(sender, route));
  allow.dataset.address = sender;
  allow.dataset.route = route;
  allow.textContent = `Allow ${sender}`;
  allow.disabled = false;
  icons();
}

/* ---- addresses and senders -------------------------------------------- */

function routeRule(route) {
  if (!route) return 'any address';
  if (route.equals) return `${route.equals}@`;
  if (route.in) return route.in.map((name) => `${name}@`).join(', ');
  if (route.prefix) return `${route.prefix}*@`;
  if (route.suffix) return `*${route.suffix}@`;
  if (route.contains) return `*${route.contains}*@`;
  if (route.matches || route.regex) return `/${route.matches || route.regex}/`;
  return JSON.stringify(route);
}

const menuId = (text) => String(text).replace(/[^a-z0-9]+/gi, '-').toLowerCase();

function localPart(address) {
  const value = String(address || '').toLowerCase();
  const at = value.indexOf('@');
  return at > 0 ? value.slice(0, at) : value;
}

function normalizeFrom(data) {
  if (Array.isArray(data)) {
    return { default: { mode: 'allowlist', addresses: data }, routes: {}, addresses: data };
  }
  const def = (data && data.default) || { mode: 'allowlist', addresses: (data && data.addresses) || [] };
  return {
    default: { mode: def.mode || 'allowlist', addresses: def.addresses || [] },
    routes: (data && data.routes) || {},
    addresses: def.addresses || [],
  };
}

function resolvePolicy(route) {
  const named = route && (fromPolicy.routes || {})[route];
  if (named) return { ...named, inherited: false, route };
  const def = fromPolicy.default || { mode: 'allowlist', addresses: fromPolicy.addresses || [] };
  return { mode: def.mode || 'allowlist', addresses: def.addresses || [], inherited: true, route: route || '' };
}

function senderAllowed(sender, route) {
  const policy = resolvePolicy(route);
  if (policy.mode === 'anyone') return true;
  return (policy.addresses || []).some((item) => String(item).toLowerCase() === sender);
}

function senderChip(route) {
  const policy = resolvePolicy(route);
  if (policy.mode === 'anyone') return 'anyone';
  const count = (policy.addresses || []).length;
  return count === 1 ? '1 sender' : `${count} senders`;
}

function addressItem({ label, kind = '', copy = '', handlers, route = '' }) {
  const off = handlers.filter((handler) => handler.status && handler.status !== 'enabled').length;
  const chips = [];
  if (kind) chips.push(`<span class="dk-chip">${escapeHtml(kind)}</span>`);
  else chips.push(`<span class="dk-chip">${escapeHtml(senderChip(route))}</span>`);
  return sourceRow({
    attrs: `data-email-select="${escapeHtml(route)}"`,
    selected: Boolean(route) && selectedKey === route,
    /* The address itself is the copy button: one target, the icon says so. */
    name: `<span class="source-name-text">${escapeHtml(label)}</span>${copy ? '<i data-lucide="copy" aria-hidden="true"></i>' : ''}`,
    nameClass: copy ? 'email-copy' : '',
    nameAttrs: copy ? `data-copy="${escapeHtml(copy)}" aria-label="Copy ${escapeHtml(copy)}"` : 'disabled',
    nameTitle: copy ? `Copy ${copy}` : label,
    status: chips.join(''),
    /* Each workflow the address starts opens from the row menu. */
    tools: handlers.length ? `<button class="icon-button row-more" type="button" popovertarget="email-menu-${escapeHtml(menuId(label))}" aria-label="Workflows for ${escapeHtml(label)}" title="Open a workflow"><i data-lucide="more-horizontal" aria-hidden="true"></i></button>
      <div id="email-menu-${escapeHtml(menuId(label))}" class="row-menu" popover aria-label="Workflows for ${escapeHtml(label)}">${handlers.map((handler) =>
        `<button type="button" class="row-menu-item email-workflow" data-workflow="${escapeHtml(handler.workflow)}" title="${escapeHtml(handler.workflow)}">Open ${escapeHtml(workflowName(handler.workflow))}</button>`).join('')}</div>` : '',
    lines: [
      handlers.length
        ? `<span>Starts ${workflowNames(handlers.map((handler) => handler.workflow))}</span>${off ? ` <span class="muted-cell">(${off} off)</span>` : ''}`
        : '<span class="status warn"><span class="status-dot" aria-hidden="true"></span>Starts no workflow</span>',
    ],
  });
}

function addressRoutes() {
  return (config.addresses || []).map((address) => localPart(address.address)).filter(Boolean);
}

function chooseSelection() {
  const routes = addressRoutes();
  if (selectedKey === null) {
    selectedKey = routes[0] || '';
    return;
  }
  if (selectedKey && !routes.includes(selectedKey)) selectedKey = routes[0] || '';
}

function renderAddressList() {
  chooseSelection();
  const addresses = config.addresses || [];
  const subscriptions = config.subscriptions || [];
  const watchers = config.watchers || [];
  const items = [
    ...addresses.map((address) => addressItem({
      label: address.address, copy: address.address, handlers: address.handlers || [],
      route: localPart(address.address),
    })),
    ...subscriptions.map((handler) => addressItem({
      label: routeRule((handler.filters || {}).route), kind: 'pattern', handlers: [handler],
    })),
    ...watchers.map((handler) => addressItem({
      label: handler.event === 'complaint.received' ? 'complaint reports' : 'bounce reports',
      kind: handler.event === 'complaint.received' ? 'complaints' : 'bounces',
      handlers: [handler],
    })),
  ];
  $('#email-addresses').innerHTML = items.join('');
  $('#email-addresses').hidden = items.length === 0;
  $('#email-empty').hidden = items.length > 0 || Boolean(config.error);
}

function bindSenderRemoves() {
  $$('.sender-remove').forEach((button) => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await removeSender(button.dataset.address);
    } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  }));
}

function renderSenderList(addresses) {
  const list = $('#email-from-list');
  const items = addresses || [];
  list.innerHTML = items.map((address) => `<li class="source-row sender-row">
    <div class="source-head">
      <span class="mono sender-address" title="${escapeHtml(address)}">${escapeHtml(address)}</span>
      <button class="icon-button sender-remove" type="button" data-address="${escapeHtml(address)}" aria-label="Remove ${escapeHtml(address)}" title="Remove sender"><i data-lucide="x"></i></button>
    </div>
  </li>`).join('') || '<li class="source-row sender-empty">No senders yet. Every incoming message is refused until one is added.</li>';
  bindSenderRemoves();
}

function renderSenderPanel() {
  const policy = resolvePolicy(selectedKey || '');
  const named = Boolean(selectedKey);
  const anyone = policy.mode === 'anyone';
  $('#email-from-title').textContent = named ? `Who can write to ${selectedKey}@` : 'Default senders';
  const note = $('#email-from-note');
  note.textContent = named
    ? 'Mail from anyone else is refused before any workflow runs.'
    : 'Used when an address has no sender policy of its own. Mail from anyone else is refused.';
  note.hidden = anyone;
  $('#email-from-mode').hidden = !named;
  $$('#email-from-mode [data-from-mode]').forEach((button) => {
    button.setAttribute('aria-pressed', String(button.dataset.fromMode === (anyone ? 'anyone' : 'allowlist')));
  });
  $('#email-from-anyone').hidden = !anyone;
  $('#email-from-list').hidden = anyone;
  $('#email-from-add').hidden = anyone;
  if (!anyone) renderSenderList(policy.addresses || []);
}

async function removeSender(address) {
  const route = selectedKey || '';
  if (route && !(fromPolicy.routes || {})[route]) {
    await api('/api/admin/email-from', { method: 'POST', body: JSON.stringify({ route, mode: 'allowlist' }) });
  }
  const params = new URLSearchParams({ address });
  if (route) params.set('route', route);
  await api(`/api/admin/email-from?${params}`, { method: 'DELETE' });
  await refresh();
}

function selectAddress(key) {
  selectedKey = key || '';
  $$('#email-addresses .source-row').forEach((row) => {
    const route = row.dataset.emailSelect || '';
    row.classList.toggle('is-selected', Boolean(route) && route === selectedKey);
  });
  renderSenderPanel();
}

export function renderEmails(data) {
  whenMenusClosed($('.view[data-page="emails"]'), () => renderEmailsNow(data));
}

function renderEmailsNow(data) {
  config = data || {};
  $('#email-domain-hint').textContent = config.domain ? `*@${config.domain}` : 'the receiving domain';
  $('#email-load-error').textContent = config.error || '';
  renderAddressList();
  renderSenderPanel();
  if (mail.events && mail.events.length === 0) renderMail();
  icons();
}

export function renderEmailFrom(data) {
  fromPolicy = normalizeFrom(data);
  whenMenusClosed($('.view[data-page="emails"]'), () => {
    renderAddressList();
    renderSenderPanel();
    icons();
  });
}

/* ---- events ----------------------------------------------------------- */

$('#email-from-add').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const address = form.address.value.trim();
  if (!address) return;
  const body = { address };
  if (selectedKey) body.route = selectedKey;
  try {
    await api('/api/admin/email-from', { method: 'POST', body: JSON.stringify(body) });
    form.reset();
    await refresh();
  } catch (error) { notice(error.message, true); }
});

$('#email-from-mode').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-from-mode]');
  if (!button || !selectedKey) return;
  try {
    await api('/api/admin/email-from', {
      method: 'POST',
      body: JSON.stringify({ route: selectedKey, mode: button.dataset.fromMode }),
    });
    await refresh();
  } catch (error) { notice(error.message, true); }
});

$('#email-filter-chips').addEventListener('click', (event) => {
  const button = event.target.closest('[data-email-filter]');
  if (!button) return;
  if (mail.filter === button.dataset.emailFilter && mail.events) return;
  mail.filter = button.dataset.emailFilter;
  mail.events = null;
  mail.next = null;
  renderMail();
  void fetchMail();
});

$('#email-mail-more').addEventListener('click', () => fetchMail({ append: true }));

$('.view[data-page="emails"]').addEventListener('click', async (event) => {
  const run = event.target.closest('.email-run');
  if (run) { event.stopPropagation(); void openRun(run.dataset.run); return; }
  const workflow = event.target.closest('.email-workflow');
  if (workflow) { void openDesigner(workflow.dataset.workflow); return; }
  const source = event.target.closest('#email-addresses .source-row');
  if (source && !event.target.closest('.row-more, .row-menu')) {
    selectAddress(source.dataset.emailSelect || '');
  }
  const copy = event.target.closest('.email-copy');
  if (copy) {
    try {
      await navigator.clipboard.writeText(copy.dataset.copy);
      notice(`Copied ${copy.dataset.copy}`);
    } catch (_) { notice('Copy is not available here; select the address instead.', true); }
    return;
  }
  const row = event.target.closest('.activity-row[data-inbox]');
  if (row) void openEmail(row.dataset.inbox);
});

$('#email-dialog-body').addEventListener('click', (event) => {
  const run = event.target.closest('.email-run');
  if (run) void openRun(run.dataset.run);
});

$('#email-replay').addEventListener('click', () => {
  if (!openInboxId) return;
  openReplayConfirm(openInboxId, async () => {
    $('#email-dialog').close();
    await fetchMail();
  });
});

$('#email-allow-sender').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  const address = button.dataset.address;
  if (!address) return;
  const body = { address };
  if (button.dataset.route) body.route = button.dataset.route;
  button.disabled = true;
  try {
    await api('/api/admin/email-from', { method: 'POST', body: JSON.stringify(body) });
    button.hidden = true;
    notice(`Allowed ${address}. Replay the message to run it now.`);
    await refresh();
  } catch (error) {
    notice(error.message, true);
    button.disabled = false;
  }
});

/* Entering the page (tab click, back/forward) loads fresh mail. */
document.addEventListener('click', (event) => {
  if (event.target.closest('a[data-view="emails"]')) void fetchMail();
});
window.addEventListener('popstate', () => {
  if (state.view === 'emails') void fetchMail();
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const row = event.target.closest('#email-mail-list .activity-row');
  if (!row || event.target !== row) return;
  event.preventDefault();
  void openEmail(row.dataset.inbox);
});
document.addEventListener('dapier:workflows-loaded', () => { if (mail.events) renderMail(); renderEmails(config); });
