/* Connections view: the create-new provider grid and the accounts table. */
import { state } from '../state.js';
import { $, $$, notice, providerMark } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine, wrapTokens } from '../format.js';
import { refresh } from './overview.js';

const CONNECT_PROVIDERS = {
  google: {
    label: 'Google Calendar',
    blurb: 'Free/busy lookups and owned-event edits for the scheduling flows.',
    connectionId: 'google-calendar',
    displayName: 'Google Calendar',
    scopes: ['https://www.googleapis.com/auth/calendar.freebusy', 'https://www.googleapis.com/auth/calendar.events.owned', 'https://www.googleapis.com/auth/userinfo.email'],
  },
  youtube: {
    label: 'YouTube',
    blurb: 'Published-video triggers for your channel.',
    connectionId: 'youtube',
    displayName: 'YouTube channel',
    scopes: ['https://www.googleapis.com/auth/youtube.readonly'],
  },
  dropbox: {
    label: 'Dropbox',
    blurb: 'File watchers, uploads, and the invoice pipeline.',
    connectionId: 'dropbox',
    displayName: 'Dropbox',
    scopes: ['files.metadata.read', 'files.content.read'],
  },
  slack: {
    label: 'Slack',
    blurb: 'Post notifications with a bot token — no browser consent.',
    connectionId: 'slack',
    displayName: 'DataTalks Slack',
    scopes: [],
  },
  telegram: {
    label: 'Telegram',
    blurb: 'Message triggers and bot posts — paste a bot token from @BotFather.',
    connectionId: 'telegram-bot',
    displayName: 'Telegram bot',
    scopes: [],
  },
};

/* Token providers paste a credential instead of browser consent. */
const TOKEN_PROVIDERS = ['slack', 'telegram'];

const TOKEN_PROVIDER_META = {
  slack: {
    heading: 'New Slack connection',
    blurb: 'Slack is the one service that can\'t do browser consent here — paste a bot (xoxb-…) or user (xoxp-…) token from your Slack app settings. It is verified against Slack and stored as the connection\'s secret.',
    label: 'Slack token',
    placeholder: 'xoxb-… or xoxp-…',
    displayName: 'DataTalks Slack',
  },
  telegram: {
    heading: 'New Telegram connection',
    blurb: 'Paste a bot token from @BotFather (the digits:secret string). It is verified against Telegram and stored as the connection\'s secret; bots drive Telegram triggers and sendMessage actions.',
    label: 'Bot token',
    placeholder: '123456:ABC-DEF…',
    displayName: 'Telegram bot',
  },
};

/* Each connection is individually connected, awaiting consent, or expired
   (needs refreshing) — say so in plain words, not raw DynamoDB values. */
const CONNECTION_STATUS_LABELS = {
  ready: 'awaiting consent',
  expired: 'expired — needs refresh',
};

function renderConnectCards() {
  // The grid is a "create new" picker — connection state lives in the
  // Connected accounts table below, so the cards stay uniform.
  $('#connect-grid').innerHTML = Object.entries(CONNECT_PROVIDERS).map(([provider, meta]) => `
    <div class="connect-card">
      <div class="connect-card-head"><span class="connect-title">${providerMark(provider)}<span class="connect-name">${meta.label}</span></span></div>
      <p class="connect-blurb">${meta.blurb}</p>
      <button class="button secondary connect-button" data-provider="${provider}" type="button">Create new</button>
    </div>`).join('');
  $$('.connect-button').forEach((button) => button.addEventListener('click', () => connectProvider(button.dataset.provider)));
}

/* New connections must not clobber existing records, so derive the first
   free "<base>", "<base>-2", … ID and number the display name to match. */
function nextConnectionId(base) {
  const taken = new Set(((state.data || {}).connections || []).map((connection) => connection.connection_id));
  if (!taken.has(base)) return { id: base, suffix: 0 };
  let suffix = 2;
  while (taken.has(`${base}-${suffix}`)) suffix += 1;
  return { id: `${base}-${suffix}`, suffix };
}

async function connectProvider(provider) {
  if (TOKEN_PROVIDERS.includes(provider)) return openTokenDialog(provider);
  const meta = CONNECT_PROVIDERS[provider];
  const { id, suffix } = nextConnectionId(meta.connectionId);
  try {
    // Provision the new record with the provider's standard scopes, then
    // bounce straight to the consent screen.
    await api('/api/admin/connections', {
      method: 'PUT',
      body: JSON.stringify({
        connection_id: id,
        provider,
        display_name: suffix ? `${meta.displayName} ${suffix}` : meta.displayName,
        scopes: meta.scopes,
      }),
    });
    window.location.assign(`/api/admin/oauth/${encodeURIComponent(id)}/start`);
  } catch (error) { notice(error.message, true); }
}

function openTokenDialog(provider) {
  const meta = TOKEN_PROVIDER_META[provider] || TOKEN_PROVIDER_META.slack;
  const form = $('#connection-form');
  form.dataset.provider = provider;
  form.reset();
  $('#connection-heading').textContent = meta.heading;
  $('#connection-blurb').textContent = meta.blurb;
  $('#connection-token-label').firstChild.textContent = meta.label;
  form.token.placeholder = meta.placeholder;
  $('#connection-error').textContent = '';
  $('#connection-dialog').showModal();
  form.token.focus();
}

function openEditConnection(connectionId) {
  const connection = ((state.data || {}).connections || []).find((item) => item.connection_id === connectionId);
  if (!connection) return;
  const form = $('#edit-connection-form');
  form.reset();
  form.dataset.connectionId = connection.connection_id;
  form.dataset.provider = connection.provider;
  $('#edit-connection-title').textContent = `Edit ${connection.display_name || connection.connection_id}`;
  $('#edit-connection-meta').textContent = `${connection.provider} · ${connection.connection_id}`;
  form.display_name.value = connection.display_name || connection.connection_id;
  form.scopes.value = (connection.scopes || []).join(' ');
  $('#edit-scopes-field').hidden = TOKEN_PROVIDERS.includes(connection.provider);
  form.root_path.value = connection.root_path || '';
  $('#edit-root-path-field').hidden = connection.provider !== 'dropbox';
  form.token.value = '';
  $('#edit-token-field').hidden = !TOKEN_PROVIDERS.includes(connection.provider);
  $('#edit-connection-error').textContent = '';
  $('#edit-connection-dialog').showModal();
  form.display_name.focus();
}

function renderConnections(connections) {
  renderConnectCards();
  $('#connection-empty').hidden = connections.length > 0;
  $('.table-wrap', $('[data-page=connections]')).hidden = connections.length === 0;
  $('#connection-table').innerHTML = connections.map((connection) => {
    const reconnect = TOKEN_PROVIDERS.includes(connection.provider) ? ''
      : `<a class="button secondary" href="/api/admin/oauth/${encodeURIComponent(connection.connection_id)}/start">${['connected', 'expired'].includes(connection.status) ? 'Reconnect' : 'Connect'}</a>`;
    return `<tr>
    <td class="cell-title"><span class="cell-name">${escapeHtml(connection.display_name)}</span><span class="cell-sub">${wrapTokens(connection.connection_id)}</span></td>
    <td data-label="Provider"><span class="provider-cell">${providerMark(connection.provider)}<span class="mono muted-cell">${escapeHtml(connection.provider)}</span></span></td>
    <td data-label="Status">${statusLine(connection.status, CONNECTION_STATUS_LABELS)}</td>
    <td class="action-cell">${reconnect}<button class="button secondary connection-edit" data-connection="${escapeHtml(connection.connection_id)}" type="button">Edit</button></td>
  </tr>`;
  }).join('');
  $$('.connection-edit').forEach((button) => button.addEventListener('click', () => openEditConnection(button.dataset.connection)));
}

export { TOKEN_PROVIDERS, TOKEN_PROVIDER_META, renderConnections, nextConnectionId, notice };

$('#connection-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const provider = form.dataset.provider || 'slack';
  const meta = TOKEN_PROVIDER_META[provider] || TOKEN_PROVIDER_META.slack;
  $('#connection-error').textContent = '';
  const { id, suffix } = nextConnectionId(meta === TOKEN_PROVIDER_META.slack ? 'slack' : 'telegram-bot');
  const body = {
    connection_id: id,
    provider,
    display_name: suffix ? `${meta.displayName} ${suffix}` : meta.displayName,
    token: form.token.value,
  };
  try {
    await api('/api/admin/connections', { method: 'PUT', body: JSON.stringify(body) });
    form.token.value = '';
    $('#connection-dialog').close();
    notice(`${meta.displayName} connected`);
    await refresh();
  } catch (error) { $('#connection-error').textContent = error.message; }
});

$('#edit-connection-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const connectionId = form.dataset.connectionId;
  const provider = form.dataset.provider;
  $('#edit-connection-error').textContent = '';
  const body = {
    connection_id: connectionId,
    provider,
    display_name: form.display_name.value.trim() || connectionId,
  };
  if (!TOKEN_PROVIDERS.includes(provider)) {
    body.scopes = form.scopes.value.split(/\s+/).filter(Boolean);
  } else {
    const token = form.token.value.trim();
    if (token) body.token = token;
  }
  if (provider === 'dropbox') body.root_path = form.root_path.value.trim();
  const connection = ((state.data || {}).connections || []).find((item) => item.connection_id === connectionId);
  if (connection && connection.expected_account_id) body.expected_account_id = connection.expected_account_id;
  try {
    await api('/api/admin/connections', { method: 'PUT', body: JSON.stringify(body) });
    form.token.value = '';
    $('#edit-connection-dialog').close();
    notice('Connection updated');
    await refresh();
  } catch (error) { $('#edit-connection-error').textContent = error.message; }
});
