/* Inbox view: every inbound trigger event, matched or not — with replay.
   The API surfaces (GET /api/admin/triggers/inbox[/id], POST …/replay)
   existed before this view; the console just never rendered them. */
import { state } from '../state.js';
import { $, icons, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, formatTimestamp, dataBlock } from '../format.js';

let events = null;
let fetching = false;

function triggerLabel(event) {
  return `${event.connector || '?'} · ${event.event || '?'}`;
}

function matchedLabel(event) {
  const matched = event.matched || [];
  if (!matched.length) return '<span class="muted-cell">no workflow</span>';
  return matched.map((id) => `<span class="mono">${escapeHtml(id)}</span>`).join(', ');
}

function eventRow(event) {
  const matched = (event.matched || []).length ? 'matched' : 'unmatched';
  return `<tr class="inbox-open" data-inbox="${escapeHtml(event.inbox_id)}" role="button" tabindex="0" data-matched="${matched}">
    <td class="mono muted-cell" data-label="Received">${escapeHtml(formatTimestamp(event.received_at) || '—')}</td>
    <td class="mono" data-label="Trigger">${escapeHtml(triggerLabel(event))}</td>
    <td data-label="Matched">${matchedLabel(event)}</td>
    <td class="mono muted-cell" data-label="Source">${escapeHtml(event.source || '—')}</td>
    <td class="action-col"><button class="dk-button dk-button--secondary inbox-replay" data-inbox="${escapeHtml(event.inbox_id)}" type="button">Replay</button></td>
  </tr>`;
}

function renderRows() {
  const list = events || [];
  $('#inbox-table').innerHTML = list.map(eventRow).join('');
  $('#inbox-empty').hidden = list.length > 0;
  $('#inbox-table-wrap').hidden = list.length === 0;
  $('#inbox-load-more').hidden = !paging.nextToken;
  icons();
}

/* Server-paged like the runs view: Load more appends the next page through
   the API's paging token; a fresh fetch resets it. */
const paging = { nextToken: null };

export async function fetchInbox({ append = false } = {}) {
  if (fetching) return;
  fetching = true;
  try {
    const params = new URLSearchParams({ limit: '25' });
    if (append && paging.nextToken) params.set('next', paging.nextToken);
    const data = await api(`/api/admin/triggers/inbox?${params}`);
    const fresh = data.events || [];
    paging.nextToken = (data.paging || {}).next || null;
    events = append && events ? [...events, ...fresh] : fresh;
  } catch (error) {
    if (!append) paging.nextToken = null;
    notice(error.message, true);
  } finally {
    fetching = false;
  }
  renderRows();
}

/* Called from the overview's refresh: fetch when the view is on screen,
   otherwise just repaint the cache. */
export function renderInbox() {
  if (state.view === 'runs') return void fetchInbox();
  renderRows();
}

async function openEvent(inboxId) {
  try {
    const data = await api(`/api/admin/triggers/inbox/${encodeURIComponent(inboxId)}`);
    const event = data.event || {};
    $('#inbox-event-title').textContent = triggerLabel(event);
    const detail = $('#inbox-event-detail');
    const matched = event.matched || [];
    detail.innerHTML = `
      <p class="sub">Received ${escapeHtml(formatTimestamp(event.received_at) || '—')} · source ${escapeHtml(event.source || '—')}</p>
      <p>Matched workflows: ${matched.length ? matched.map((id) => `<span class="mono">${escapeHtml(id)}</span>`).join(', ') : '<em>none — no workflow picked this up</em>'}</p>
      ${event.error ? `<p class="dialog-feedback error">${escapeHtml(event.error)}</p>` : ''}
      <h3 class="sub-head">Envelope</h3>
      ${dataBlock(event.data ?? event)}`;
    $('#inbox-replay-button').dataset.inbox = inboxId;
    $('#inbox-event-dialog').showModal();
  } catch (error) {
    notice(error.message, true);
  }
}

document.addEventListener('click', async (event) => {
  const replay = event.target.closest('.inbox-replay');
  if (replay) {
    event.stopPropagation();
    openReplayConfirm(replay.dataset.inbox);
    return;
  }
  const row = event.target.closest('.inbox-open');
  if (row && row.dataset.inbox) openEvent(row.dataset.inbox);
});

/* Shared with the Emails page: `after` runs once the replay is queued so
   the caller repaints its own list. */
export function openReplayConfirm(inboxId, after = null) {
  const dialog = $('#inbox-replay-confirm-dialog');
  $('#inbox-replay-confirm-message').textContent =
    `Replay this recorded event? It is re-injected with a fresh id; workflow actions may send messages or change external data a second time.`;
  dialog.returnValue = '';
  dialog.showModal();
  dialog.addEventListener('close', () => {
    if (dialog.returnValue === 'confirm') void replayEvent(inboxId, after);
  }, { once: true });
}

async function replayEvent(inboxId, after = null) {
  try {
    const data = await api(`/api/admin/triggers/inbox/${encodeURIComponent(inboxId)}/replay`, {
      method: 'POST',
      body: '{}',
    });
    notice(data.run_id ? `Replay queued (event ${data.run_id}).` : 'Replay queued.');
    await (after ? after() : fetchInbox());
  } catch (error) {
    notice(error.message, true);
  }
}

/* Entering the view (nav click, back/forward) fetches fresh events. */
document.addEventListener('click', (event) => {
  if (event.target.closest('.nav-item[data-view="runs"]')) void fetchInbox();
});
$('#inbox-load-more').addEventListener('click', () => fetchInbox({ append: true }));
$('#inbox-replay-button').addEventListener('click', (event) => {
  if (event.currentTarget.dataset.inbox) openReplayConfirm(event.currentTarget.dataset.inbox);
});
window.addEventListener('popstate', () => {
  if (state.view === 'runs') void fetchInbox();
});
