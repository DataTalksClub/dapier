/* Connections view: a register of services, and the add-service picker. */
import { state } from '../state.js';
import { $, $$, notice, serviceMark } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, formatTimestamp, statusLine } from '../format.js';
import { refresh } from './overview.js';

/* Keep ids, labels, providers, connectionId, and default scopes in lockstep
   with src/dapier/connections/services.py — tests/test_connection_services.py
   pins the pair. Google products share one OAuth provider; the picker still
   offers each product on its own. */
const CONNECT_SERVICES = {
  gmail: {
    label: 'Gmail',
    blurb: 'Read mail and send from the connected inbox.',
    provider: 'google',
    connectionId: 'gmail',
    scopes: ['https://www.googleapis.com/auth/gmail.readonly', 'https://www.googleapis.com/auth/gmail.send', 'https://www.googleapis.com/auth/userinfo.email'],
  },
  calendar: {
    label: 'Google Calendar',
    blurb: 'Calendar listings, event triggers, and owned-event edits.',
    provider: 'google',
    connectionId: 'google-calendar',
    scopes: ['https://www.googleapis.com/auth/calendar.freebusy', 'https://www.googleapis.com/auth/calendar.events.owned', 'https://www.googleapis.com/auth/calendar.readonly', 'https://www.googleapis.com/auth/userinfo.email'],
  },
  drive: {
    label: 'Google Drive',
    blurb: 'Find, read, and watch files in Drive.',
    provider: 'google',
    connectionId: 'google-drive',
    scopes: ['https://www.googleapis.com/auth/drive.readonly', 'https://www.googleapis.com/auth/userinfo.email'],
  },
  docs: {
    label: 'Google Docs',
    blurb: 'Read and edit Google Docs the account can access.',
    provider: 'google',
    connectionId: 'google-docs',
    scopes: ['https://www.googleapis.com/auth/documents', 'https://www.googleapis.com/auth/userinfo.email'],
  },
  sheets: {
    label: 'Google Sheets',
    blurb: 'Read and write spreadsheets, including row triggers.',
    provider: 'google',
    connectionId: 'google-sheets',
    scopes: ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/userinfo.email'],
  },
  youtube: {
    label: 'YouTube',
    blurb: 'Published-video triggers for your channel.',
    provider: 'youtube',
    connectionId: 'youtube',
    scopes: ['https://www.googleapis.com/auth/youtube.readonly'],
  },
  dropbox: {
    label: 'Dropbox',
    blurb: 'File watchers, uploads, and the invoice pipeline.',
    provider: 'dropbox',
    connectionId: 'dropbox',
    scopes: ['account_info.read', 'files.metadata.read', 'files.content.read', 'files.content.write'],
  },
  slack: {
    label: 'Slack',
    blurb: 'Connect a Slack account for agent access with a bot or user token. Workflow Slack actions can also use the shared Service credential.',
    provider: 'slack',
    connectionId: 'slack',
    scopes: [],
  },
  telegram: {
    label: 'Telegram',
    blurb: 'Message triggers and bot posts — paste a bot token from @BotFather.',
    provider: 'telegram',
    connectionId: 'telegram-bot',
    scopes: [],
  },
  zoom: {
    label: 'Zoom Webhooks',
    blurb: 'Start workflows when a Zoom cloud recording video finishes processing.',
    provider: 'zoom',
    connectionId: 'zoom',
    scopes: [],
  },
};

const SERVICE_MARKERS = {
  gmail: ['/auth/gmail.'],
  calendar: ['/auth/calendar'],
  drive: ['/auth/drive'],
  docs: ['/auth/documents'],
  sheets: ['/auth/spreadsheets'],
  youtube: ['/auth/youtube', 'youtube.force-ssl', 'yt-analytics'],
};

const SERVICE_ORDER = Object.keys(CONNECT_SERVICES);
const GOOGLE_FAMILY = new Set(['gmail', 'calendar', 'drive', 'docs', 'sheets']);

function serviceLabel(serviceId) {
  return CONNECT_SERVICES[serviceId]?.label || (serviceId === 'google' ? 'Google' : serviceId);
}

function oauthClientProvider(meta) {
  return (meta.provider === 'google' || meta.provider === 'youtube') ? 'google' : meta.provider;
}

function scopesOf(connection) {
  const granted = connection.granted_scopes || [];
  if (granted.length) return granted.map(String);
  return (connection.scopes || []).map(String);
}

function deriveServices(connection) {
  const provider = connection.provider || '';
  const scopes = scopesOf(connection);
  if (provider === 'zoom') return [{ id: 'zoom', label: scopes.length ? 'Zoom API' : 'Zoom Webhooks',
    description: scopes.length
      ? "Read and manage meetings and recordings through Zoom's API, within the permissions granted."
      : 'Receive Zoom event notifications, such as completed cloud recordings, to start workflows.' }];
  const found = [];
  const seen = new Set();
  for (const [id, meta] of Object.entries(CONNECT_SERVICES)) {
    if (['dropbox', 'slack', 'telegram', 'zoom'].includes(id)) {
      if (provider === meta.provider && !seen.has(id)) { found.push({ id, label: meta.label }); seen.add(id); }
      continue;
    }
    if (id === 'youtube' && provider === 'youtube' && !seen.has(id)) {
      found.push({ id, label: meta.label }); seen.add(id); continue;
    }
    if ((provider === 'google' || provider === 'youtube')
        && (SERVICE_MARKERS[id] || []).some((marker) => scopes.some((scope) => scope.includes(marker)))
        && !seen.has(id)) {
      found.push({ id, label: meta.label }); seen.add(id);
    }
  }
  if (found.length) return found;
  if (provider === 'google') return [{ id: 'google', label: 'Google' }];
  return [{ id: provider || 'unknown', label: serviceLabel(provider || 'unknown') }];
}

function servicesFor(connection) {
  const listed = connection.services;
  if (Array.isArray(listed) && listed.length) {
    return listed.map((entry) => (typeof entry === 'string'
      ? { id: entry, label: serviceLabel(entry) }
      : { ...entry, label: entry.label || serviceLabel(entry.id) }));
  }
  return deriveServices(connection);
}

/* What each register group is for, shown behind the (?) next to its
   title. Zoom's two kinds carry their own text from the API's services. */
const SERVICE_HELP = {
  gmail: 'Read messages and send mail from the connected inbox. Email triggers and Gmail send actions use it.',
  calendar: 'List calendars and free/busy times, start workflows from events, and edit events this account owns.',
  drive: 'Find, read, and watch files in Google Drive, within the permissions granted.',
  docs: 'Read and edit the Google Docs this account can access.',
  sheets: 'Read and write the spreadsheets this account can access, including row triggers.',
  youtube: 'Start workflows when the channel publishes a video, and read channel data within the permissions granted.',
  dropbox: "Watch folders, read files, and upload under the connection's root path.",
  slack: 'Agent access to a Slack workspace with a bot or user token. Workflow Slack actions can also use the shared Slack Service credential.',
  telegram: 'A Telegram bot: start workflows from the messages it receives and post messages from actions.',
  dataops: 'Read invoices from DataOps with its invoice-reader credential. Invoice parsing and bookkeeping stay in DataOps.',
  google: "A Google sign-in whose grant doesn't cover a specific product yet. Edit its scopes to choose what it is for.",
};

function helpTip(text) {
  if (!text) return '';
  const safe = escapeHtml(text);
  return `<button type="button" class="help-tip" aria-label="${safe}" data-tip="${safe}">?</button>`;
}

function connectionHasService(connection, serviceId) {
  return servicesFor(connection).some((service) => service.id === serviceId);
}

/* Verified identity (email, channel) — never the mashed display_name leftover
   like "Google Calendar + Drive (Gmail)". Unverified grants have no identity. */
function accountIdentity(connection) {
  return connection.account_title || connection.verified_account_id || '';
}

/* People name a connection by its account, never by the internal
   connection_id (which is an opaque key since one Google sign-in backs
   several services). A grant that never finished consent has no account
   yet, so it is named by what it was meant to cover. */
function accountLabel(connection) {
  if (connection.provider === 'zoom' && !scopesOf(connection).length) {
    return connection.display_name || 'Zoom webhook';
  }
  return accountIdentity(connection) || `Unfinished ${productList(connection) || connection.provider} setup`;
}

function productList(connection) {
  return servicesFor(connection).map((service) => service.label.replace(/^Google /, '')).join(', ');
}

/* Slack and Telegram paste a credential. Zoom meetings are OAuth (Reconnect
   starts consent); the webhook secret is a Manage field, not the reconnect path. */
const TOKEN_PROVIDERS = ['slack', 'telegram', 'zoom'];
/* The API says whether a connection finishes/renews through OAuth consent
   (oauth_consent): pasted-token providers — plugins add their own — and
   Zoom webhook connections never do. The fallback covers older payloads. */
const usesOAuthConsent = (connection) => connection.oauth_consent ?? (
  connection.provider !== 'slack' && connection.provider !== 'telegram'
  && !(connection.provider === 'zoom' && !(connection.scopes || []).length));
/* Google and YouTube grants verify an account (email, channel) during
   consent; one without a verified identity never finished signing in.
   Token and no-auth providers have no identity to verify. */
const verifiesIdentity = (connection) => ['google', 'youtube'].includes(connection.provider);

const TOKEN_PROVIDER_META = {
  slack: {
    heading: 'New Slack connection',
    blurb: 'Paste a bot (xoxb-…) or user (xoxp-…) token from your Slack app settings. This creates an account for agent access; the shared Slack Service credential for workflow actions is configured separately under Credentials. To listen for messages, open Manage afterwards and wire up event subscriptions.',
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
  zoom: {
    heading: 'New Zoom recording connection',
    blurb: 'Create a Zoom Webhook Only app, then paste its Secret Token. After creating this connection, copy its callback URL into the Zoom event subscription and select recording.completed.',
    label: 'Webhook Secret Token',
    placeholder: 'Secret Token from Zoom Marketplace',
    displayName: 'Zoom recordings',
    connectionId: 'zoom',
  },
};

/* Each connection is individually connected, awaiting consent, expiring
   soon, or expired (needs refreshing) — say so in plain words, not raw
   DynamoDB values. */
const CONNECTION_STATUS_LABELS = {
  ready: 'setup incomplete',
  expired: 'needs reconnection',
  revoked: 'revoked',
  expiring: 'expiring soon',
};

/* Tokens inside this horizon still work, but the daily digest will email
   them — keep in lockstep with DAPIER_CONNECTION_DIGEST_HOURS
   (src/dapier/connection_digest.py). The register shows them on the row
   (status + Reconnect); there is no send-now email button. */
const EXPIRY_HORIZON_HOURS = 48;

const tokenExpiringSoon = (connection) => {
  if (connection.auto_refresh || connection.health === 'expired' || !connection.token_expires_at) return false;
  const when = Date.parse(connection.token_expires_at);
  return !Number.isNaN(when) && when <= Date.now() + EXPIRY_HORIZON_HOURS * 3600 * 1000;
};

/* A stored token can expire (or be about to) while the record still reads
   'connected' — the API flags a lapse via health; the horizon flags the
   ones that still work. Treat both as needing attention everywhere the
   list aggregates, not just in the row rendering. */
const effectiveStatus = (connection) => {
  if (connection.health === 'expired' && connection.status === 'connected') return 'expired';
  if (connection.status === 'connected' && tokenExpiringSoon(connection)) return 'expiring';
  return connection.status;
};
const needsAttention = (connection) =>
  ['ready', 'expired', 'revoked', 'expiring'].includes(effectiveStatus(connection));

let addPickerOpen = false;

/* Server-paged accounts register: the overview snapshot clips at its scan
   limit, so once this page is open the view fetches /api/admin/connections
   (the paged list the API owns) and Load more appends the next page through
   the paging token — the same pattern as the runs view. Until the fetch
   lands, the register shows the snapshot (state.data.connections); the
   snapshot keeps feeding the header widgets either way. */
const connectionsPage = { connections: null, nextToken: null, seq: 0 };

function resetConnectionsPage() {
  connectionsPage.connections = null;
  connectionsPage.nextToken = null;
  connectionsPage.seq += 1;
}

async function fetchConnectionsPage({ append = false } = {}) {
  const seq = ++connectionsPage.seq;
  const params = new URLSearchParams({ limit: '50' });
  if (append && connectionsPage.nextToken) params.set('next', connectionsPage.nextToken);
  try {
    const data = await api(`/api/admin/connections?${params}`);
    if (seq !== connectionsPage.seq) return; // a newer fetch superseded this one
    const fresh = data.connections || [];
    connectionsPage.nextToken = (data.paging || {}).next || null;
    connectionsPage.connections = append && connectionsPage.connections
      ? [...connectionsPage.connections, ...fresh] : fresh;
  } catch (error) {
    if (seq === connectionsPage.seq && !append) {
      resetConnectionsPage(); // fall back to the snapshot until the next render
    }
    notice(error.message, true);
    return;
  }
  renderConnections(connectionsPage.connections || []);
}

/* Created lazily beside the table (index.html has no static button for it —
   only this view needs Load more). */
function connectionsLoadMoreButton() {
  const existing = $('#connections-load-more');
  if (existing) return existing;
  const button = document.createElement('button');
  button.id = 'connections-load-more';
  button.className = 'dk-button dk-button--secondary';
  button.type = 'button';
  button.hidden = true;
  button.textContent = 'Load more';
  $('#connection-register')?.after(button);
  button.addEventListener('click', () => fetchConnectionsPage({ append: true }));
  return button;
}

/* A snapshot refresh re-renders from the overview scan (clipped at 50);
   reset the server-paged table first so the next render refetches it all. */
async function refreshConnections() {
  resetConnectionsPage();
  await refresh();
}

const OAUTH_RESULTS = {
  access_denied: ['Access was not approved', 'Retry setup and approve the requested permissions.'],
  session_expired: ['Setup link expired', 'Start connection setup again from this page. Consent links can only be used once.'],
  wrong_operator: ['Different operator account', 'Sign in with the account that started setup, then try again.'],
  incomplete: ['Setup could not finish', 'The connection or authorization code was missing. Retry setup.'],
  client_configuration: ['OAuth client needs attention', 'Check this provider’s client ID and secret in Credentials, then retry.'],
  exchange_failed: ['Provider authorization failed', 'The authorization code may have expired. Retry setup.'],
  missing_scopes: ['Permissions were not granted', 'Retry setup and approve every requested permission.'],
  verification_failed: ['Account could not be verified', 'Retry setup. If it happens again, check the provider account and OAuth client.'],
  wrong_account: ['Wrong provider account', 'Choose the account already linked to this connection, or add a separate connection for the other account.'],
};

export function showOAuthResult(code, connectionId) {
  const banner = $('#oauth-result');
  if (code === 'connected') {
    banner.hidden = true;
    notice('Connection ready');
    return;
  }
  const [title, message] = OAUTH_RESULTS[code] || ['Setup could not finish', 'Return to Connections and try setup again.'];
  $('#oauth-result-title').textContent = title;
  $('#oauth-result-message').textContent = message;
  const known = ((state.data || {}).connections || []).some((item) => item.connection_id === connectionId);
  $('#oauth-retry').hidden = !known || code === 'client_configuration';
  $('#oauth-retry').dataset.connection = known ? connectionId : '';
  $('#oauth-credentials').hidden = code !== 'client_configuration';
  banner.hidden = false;
}

function openOAuthWindow(url, connectionId) {
  const popup = window.open(url, '_blank', 'width=680,height=760');
  if (!popup) {
    notice('Allow pop-ups to connect this account, then try again.', true);
    return;
  }
  popup.focus();
  watchOAuthPopup(popup, connectionId);
}

$('#oauth-retry').addEventListener('click', (event) => {
  const connectionId = event.currentTarget.dataset.connection;
  if (connectionId) openOAuthWindow(`/api/admin/oauth/${encodeURIComponent(connectionId)}/start`, connectionId);
});
$('#oauth-dismiss').addEventListener('click', () => { $('#oauth-result').hidden = true; });

function renderConnectCards(connections) {
  $('#connect-grid').innerHTML = Object.entries(CONNECT_SERVICES).map(([serviceId, meta]) => {
    const clientProvider = oauthClientProvider(meta);
    const oauthClient = ((state.data || {}).oauth_clients || []).find((item) => item.provider === clientProvider);
    const needsClient = !TOKEN_PROVIDERS.includes(meta.provider) && oauthClient && !oauthClient.configured;
    const pending = !TOKEN_PROVIDERS.includes(meta.provider)
      ? connections.find((connection) => connection.status === 'ready' && connectionHasService(connection, serviceId)) : null;
    const accounts = connections.filter((connection) => connectionHasService(connection, serviceId)
      && (serviceId !== 'zoom' || !scopesOf(connection).length));
    const reusable = reusableGoogleConnections(serviceId, connections);
    const addLabel = meta.provider === 'zoom' ? 'Add Zoom app'
      : reusable.length ? 'Add to an account' : 'Connect';
    const action = pending
      ? `<a class="dk-button dk-button--secondary connection-oauth" href="/api/admin/oauth/${encodeURIComponent(pending.connection_id)}/start" data-connection="${escapeHtml(pending.connection_id)}" target="_blank" rel="noopener">Finish setup</a>
         <button class="dk-button dk-button--secondary connect-button" data-service="${serviceId}" type="button">Add another account</button>`
      : `<button class="dk-button dk-button--secondary connect-button" data-service="${serviceId}" type="button">${addLabel}</button>`;
    return `
    <div class="connect-card">
      <div class="connect-card-head"><span class="connect-title">${serviceMark(serviceId)}<span class="connect-name">${meta.label}</span></span>${accounts.length ? `<span class="connect-count">${accounts.length} ${serviceId === 'zoom' ? 'connection' : 'account'}${accounts.length === 1 ? '' : 's'}</span>` : ''}</div>
      <p class="connect-blurb">${meta.blurb}</p>
      ${needsClient ? `<p class="connect-pending">Set up the ${escapeHtml(clientProvider)} OAuth client in <a href="/credentials">Credentials</a> before consent.</p>` : ''}
      ${pending ? `<p class="connect-pending">${escapeHtml(pending.account_title || pending.display_name || pending.connection_id)} is waiting for setup.</p>` : ''}
      <div class="connect-card-actions">${action}</div>
    </div>`;
  }).join('');
  $$('.connect-button').forEach((button) => button.addEventListener('click', async () => {
    if (button.disabled) return;
    button.disabled = true;
    const label = button.textContent;
    button.textContent = 'Starting…';
    try { await connectService(button.dataset.service); }
    finally { button.disabled = false; button.textContent = label; }
  }));
}

function allKnownConnections() {
  if (connectionsPage.connections) return connectionsPage.connections;
  return (state.data || {}).connections || [];
}

/* New connections must not clobber existing records, so derive the first
   free "<base>", "<base>-2", … ID and number the display name to match. */
function nextConnectionId(base) {
  const taken = new Set(allKnownConnections().map((connection) => connection.connection_id));
  if (!taken.has(base)) return { id: base, suffix: 0 };
  let suffix = 2;
  while (taken.has(`${base}-${suffix}`)) suffix += 1;
  return { id: `${base}-${suffix}`, suffix };
}

function reusableGoogleConnections(serviceId, connections) {
  if (!GOOGLE_FAMILY.has(serviceId)) return [];
  const list = connections || allKnownConnections();
  return list.filter((connection) =>
    connection.provider === 'google'
    && connection.status === 'connected'
    && !connectionHasService(connection, serviceId));
}

async function connectService(serviceId) {
  const meta = CONNECT_SERVICES[serviceId];
  if (!meta) return;
  if (TOKEN_PROVIDERS.includes(meta.provider)) return openTokenDialog(meta.provider);
  const reuse = reusableGoogleConnections(serviceId);
  if (reuse.length) return openReuseDialog(serviceId, reuse);
  return startNewOAuthConnection(meta);
}

function openReuseDialog(serviceId, candidates) {
  const meta = CONNECT_SERVICES[serviceId];
  const dialog = $('#reuse-connection-dialog');
  $('#reuse-connection-title').textContent = `Add ${meta.label}`;
  $('#reuse-connection-blurb').textContent =
    `Use an existing Google account to add ${meta.label} to that grant. You'll approve the extra permissions. The same credential then covers every service on that account.`;
  $('#reuse-connection-list').innerHTML = candidates.map((connection) => {
    const identity = accountLabel(connection);
    const already = productList(connection);
    return `<button class="reuse-account" type="button" data-connection="${escapeHtml(connection.connection_id)}">
      <span class="cell-name">${escapeHtml(identity)}</span>
      <span class="cell-sub">${escapeHtml(already ? `Already has ${already} · ${connection.connection_id}` : connection.connection_id)}</span>
    </button>`;
  }).join('');
  $$('.reuse-account', dialog).forEach((button) => button.addEventListener('click', async () => {
    const connection = candidates.find((item) => item.connection_id === button.dataset.connection);
    dialog.close();
    if (connection) await addServiceToConnection(serviceId, connection);
  }));
  $('#reuse-connection-new').onclick = () => {
    dialog.close();
    void startNewOAuthConnection(meta);
  };
  dialog.showModal();
}

async function addServiceToConnection(serviceId, connection) {
  const meta = CONNECT_SERVICES[serviceId];
  const scopes = [...new Set([
    ...(connection.granted_scopes || []),
    ...(connection.scopes || []),
    ...(meta.scopes || []),
  ])];
  const popup = window.open('', '_blank', 'width=680,height=760');
  if (!popup) return notice('Allow pop-ups to add this service, then try again.', true);
  popup.document.title = `Add ${meta.label}`;
  if (popup.document.body) popup.document.body.textContent = 'Preparing extra permissions…';
  const stopWatching = watchOAuthPopup(popup, connection.connection_id);
  try {
    const body = {
      connection_id: connection.connection_id,
      provider: connection.provider,
      display_name: connection.display_name || connection.connection_id,
      scopes,
    };
    if (connection.expected_account_id) body.expected_account_id = connection.expected_account_id;
    await api('/api/admin/connections', { method: 'PUT', body: JSON.stringify(body) });
    const startUrl = `/api/admin/oauth/${encodeURIComponent(connection.connection_id)}/start`;
    if (!popup.closed) {
      popup.location.assign(startUrl);
      popup.focus();
    } else {
      notice('Scopes saved. Reconnect from the account row to finish.');
    }
    await refreshConnections();
  } catch (error) {
    stopWatching();
    if (!popup.closed) popup.close();
    notice(error.message, true);
  }
}

async function startNewOAuthConnection(meta) {
  // Open during the click: browsers block windows opened after the PUT awaits.
  const popup = window.open('', '_blank', 'width=680,height=760');
  if (!popup) return notice('Allow pop-ups to add this connection, then try again.', true);
  popup.document.title = `Connect ${meta.label}`;
  if (popup.document.body) popup.document.body.textContent = 'Preparing connection…';
  let stopWatching = () => {};
  try {
    // Provision the new record with the service's standard scopes, then
    // bounce straight to the consent screen. No display name: once consent
    // verifies the account, the record takes the verified identity (the
    // account email) as its name — that is what tells same-provider
    // accounts apart, not a "Google Calendar 2" counter. No connection_id
    // either: the API mints an opaque internal key, and people address the
    // connection as "<service> <account>" from then on.
    const created = await api('/api/admin/connections', {
      method: 'PUT',
      body: JSON.stringify({
        provider: meta.provider,
        scopes: meta.scopes,
      }),
    });
    const id = created.connection_id;
    stopWatching = watchOAuthPopup(popup, id);
    const startUrl = `/api/admin/oauth/${encodeURIComponent(id)}/start`;
    if (!popup.closed) {
      popup.location.assign(startUrl);
      popup.focus();
    } else {
      notice('Connection created. Use Finish setup on the account to complete consent.');
    }
    await refreshConnections();
  } catch (error) {
    stopWatching();
    if (!popup.closed) popup.close();
    notice(error.message, true);
  }
}

function watchOAuthPopup(popup, connectionId) {
  const timer = setInterval(async () => {
    if (popup.closed) {
      clearInterval(timer);
      await refreshConnections();
      const connection = ((state.data || {}).connections || []).find((item) => item.connection_id === connectionId);
      if (connection?.status === 'connected') notice('Consent window closed. Current connection status refreshed.');
      else showOAuthResult('incomplete', connectionId);
      return;
    }
    try {
      // The callback redirects to /connections?oauth=... after consent.
      const params = new URLSearchParams(popup.location.search);
      const result = params.get('oauth');
      if (popup.location.pathname === '/connections' && result) {
        clearInterval(timer);
        popup.close();
        await refreshConnections();
        showOAuthResult(result, params.get('connection') || connectionId);
      }
    } catch (_) {
      // The provider's consent page is on another origin until it redirects.
    }
  }, 500);
  return () => clearInterval(timer);
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

export function openEditConnection(connectionId) {
  const connection = ((state.data || {}).connections || []).find((item) => item.connection_id === connectionId);
  if (!connection) return;
  const form = $('#edit-connection-form');
  form.reset();
  form.dataset.connectionId = connection.connection_id;
  form.dataset.provider = connection.provider;
  const refs = usageRefs(connection);
  $('#edit-connection-title').textContent = `Manage ${accountLabel(connection)}`;
  $('#edit-connection-meta').textContent = [productList(connection), connection.status === 'ready' ? 'not signed in' : '']
    .filter(Boolean).join(' · ');
  renderManageDetails(connection, refs);
  form.display_name.value = connection.display_name || accountLabel(connection);
  form.scopes.value = (connection.scopes || []).join(' ');
  $('#edit-scopes-field').hidden = TOKEN_PROVIDERS.includes(connection.provider);
  form.root_path.value = connection.root_path || '';
  $('#edit-root-path-field').hidden = connection.provider !== 'dropbox';
  form.token.value = '';
  $('#edit-token-field').hidden = !TOKEN_PROVIDERS.includes(connection.provider);
  $('#edit-token-field').firstChild.textContent = connection.provider === 'zoom' ? 'Replace webhook Secret Token' : 'Replace token';
  form.token.placeholder = connection.provider === 'zoom' ? 'Secret Token from Zoom Marketplace' : 'xoxb-… or 123456:ABC-…';
  $('#edit-token-field .field-hint').textContent = connection.provider === 'zoom'
    ? 'Leave blank to keep the stored secret. Replacing it requires Zoom to validate the callback again.'
    : 'Leave blank to keep the stored token — it is re-verified on save.';
  const zoomSetup = $('#edit-zoom-setup');
  zoomSetup.hidden = connection.provider !== 'zoom';
  if (connection.provider === 'zoom') $('#edit-zoom-url').textContent = `${window.location.origin}/hooks/zoom/${encodeURIComponent(connectionId)}`;
  const slackSetup = $('#edit-slack-setup');
  slackSetup.hidden = connection.provider !== 'slack';
  if (connection.provider === 'slack') {
    $('#edit-slack-url').textContent = `${window.location.origin}/hooks/slack/${encodeURIComponent(connectionId)}`;
    form.signing_secret.value = '';
  }
  const youtubeSetup = $('#edit-youtube-setup');
  youtubeSetup.hidden = connection.provider !== 'youtube';
  if (connection.provider === 'youtube') {
    const callback = `${window.location.origin}/hooks/youtube`;
    $('#edit-youtube-url').textContent = callback;
    $('#edit-youtube-curl').textContent = [
      'curl -d hub.mode=subscribe -d hub.verify=async \\',
      `  -d hub.callback=${callback} \\`,
      `  -d 'hub.topic=https://www.youtube.com/xml/feeds/videos.xml?channel_id=CHANNEL_ID' \\`,
      '  -d hub.secret=<webhook secret> \\',
      '  https://pubsubhubbub.appspot.com/subscribe',
    ].join('\n');
  }
  const dropboxSetup = $('#edit-dropbox-setup');
  dropboxSetup.hidden = connection.provider !== 'dropbox';
  if (connection.provider === 'dropbox') $('#edit-dropbox-url').textContent = `${window.location.origin}/hooks/dropbox`;
  const reconnect = $('#edit-connection-reconnect');
  reconnect.hidden = !usesOAuthConsent(connection) || connection.status === 'ready';
  reconnect.href = `/api/admin/oauth/${encodeURIComponent(connectionId)}/start`;
  $('#edit-connection-revoke').hidden = !(['connected', 'expired'].includes(connection.status) ||
    (connection.provider === 'zoom' && connection.status === 'ready'));
  $('#edit-connection-revoke').textContent = connection.provider === 'zoom' ? 'Disable webhook' : 'Revoke tokens';
  $('#edit-connection-access').hidden = connection.provider === 'zoom';
  $('#edit-connection-error').textContent = '';
  $('#edit-connection-test-result').hidden = true;
  $('#edit-connection-dialog').showModal();
  form.display_name.focus();
}

function renderConnections(connections) {
  /* Server-paged rows win once fetched; the argument (the overview snapshot)
     is the fallback that keeps the view usable before and without them. */
  const serverPaged = connectionsPage.connections !== null;
  if (serverPaged) connections = connectionsPage.connections;
  connections = connections || [];
  if (!serverPaged && document.body.dataset.view === 'connections') fetchConnectionsPage();
  connectionsLoadMoreButton().hidden = !(serverPaged && connectionsPage.nextToken);
  renderConnectCards(connections);
  const connected = connections.filter((connection) => effectiveStatus(connection) === 'connected').length;
  const attention = connections.filter(needsAttention).length;
  $('#connection-summary').textContent = connections.length
    ? `${connected} connected · ${attention} ${attention === 1 ? 'needs' : 'need'} attention`
    : 'No accounts connected';
  $('#connect-picker').hidden = !addPickerOpen && connections.length > 0;
  $('#add-connection').setAttribute('aria-expanded', String(!$('#connect-picker').hidden));
  $('#connection-empty').hidden = connections.length > 0;
  $('#connection-register').hidden = connections.length === 0;
  const query = ($('#connection-search')?.value || '').trim().toLowerCase();
  const statusFilter = $('#connection-status-filter')?.value || 'all';
  const filtered = connections.filter((connection) => {
    if (statusFilter === 'connected' && effectiveStatus(connection) !== 'connected') return false;
    if (statusFilter === 'attention' && !needsAttention(connection)) return false;
    if (!query) return true;
    return [connection.display_name, connection.connection_id, connection.provider,
      connection.account_title, connection.verified_account_id,
      ...servicesFor(connection).flatMap((service) => [service.id, service.label]),
      ...(connection.used_in || []).map((entry) => entry.ref)].some((value) => String(value || '').toLowerCase().includes(query));
  });
  $('#connection-filter-empty').hidden = filtered.length > 0 || connections.length === 0;
  const withinGroup = (a, b) => {
    const name = (connection) => accountIdentity(connection) || connection.connection_id;
    return String(name(a)).localeCompare(String(name(b)));
  };
  /* One panel per service — a Google grant covering Calendar and Drive shows
     under both, with a same-grant line so the shared credential stays
     visible; grants that never finished consent have no verified identity
     and count as not signed in. */
  const groups = new Map();
  for (const connection of filtered) {
    for (const service of servicesFor(connection)) {
      const groupId = service.id === 'zoom'
        ? (scopesOf(connection).length ? 'zoom-api' : 'zoom-webhooks') : service.id;
      groups.set(groupId, [...(groups.get(groupId) || []), connection]);
    }
  }
  const ordered = [];
  for (const id of SERVICE_ORDER) {
    if (groups.has(id)) ordered.push([id, groups.get(id)]);
  }
  for (const [id, group] of groups) {
    if (!SERVICE_ORDER.includes(id)) ordered.push([id, group]);
  }
  $('#connection-register').innerHTML = ordered.map(([serviceId, group]) => {
    const zoom = serviceId === 'zoom-api' || serviceId === 'zoom-webhooks';
    const service = zoom ? servicesFor(group[0]).find((entry) => entry.id === 'zoom') : null;
    const rows = [...group].sort(withinGroup).map((connection) => accountRow(connection, zoom ? 'zoom' : serviceId)).join('');
    const unfinished = group.filter((connection) => !accountIdentity(connection)
      && verifiesIdentity(connection)).length;
    const needs = group.filter(needsAttention).length;
    const meta = [
      `${group.length} ${zoom ? 'connection' : 'account'}${group.length === 1 ? '' : 's'}`,
      unfinished ? `${unfinished} not signed in` : '',
      needs ? `${needs} ${needs === 1 ? 'needs' : 'need'} attention` : '',
    ].filter(Boolean).join(' · ');
    return `<section class="data-panel service-panel" data-service="${escapeHtml(serviceId)}">
      <div class="section-head"><h2 class="service-panel-title">${serviceMark(zoom ? 'zoom' : serviceId)}<span>${escapeHtml(service?.label || serviceLabel(serviceId))}</span>${helpTip(service?.description || SERVICE_HELP[serviceId])}</h2>
      <p class="sub">${escapeHtml(meta)}</p></div>
      <ul class="service-accounts">${rows}</ul>
    </section>`;
  }).join('');
  $$('.connection-edit').forEach((button) => button.addEventListener('click', () => openEditConnection(button.dataset.connection)));
  $$('.connection-remove').forEach((button) => button.addEventListener('click', () => deleteConnection(button.dataset.connection)));
  $$('.connection-finish').forEach((button) => button.addEventListener('click', () => openZoomWebhookSetup(button.dataset.connection)));
  bindOAuthLinks();
}

/* Which live workflows and hook triggers reference the connection — the
   API computes used_in (workflow definitions + hook bindings) so every
   surface, not just the console, agrees on what a connection is for and
   whether deleting it would break something. */
function usageRefs(connection) {
  return [...new Set((connection.used_in || []).map((entry) => String(entry.ref)))];
}

/* Rows carry only the count; Manage lists the flows by name. */
function usageLabel(connection) {
  const count = usageRefs(connection).length;
  if (!count) return 'Not used by any flow';
  return `Used in ${count} flow${count === 1 ? '' : 's'}`;
}

function accountRow(connection, serviceId) {
    /* The API distinguishes automatic renewal from expiry needing consent. */
    const status = effectiveStatus(connection);
    /* Finish setup goes where setup actually finishes: OAuth consent, or —
       for a Zoom webhook, which Zoom's URL validation activates — Manage,
       opened on the callback URL to paste into the Zoom app. */
    const nextAction = usesOAuthConsent(connection) && status !== 'connected'
      ? `<a class="dk-button dk-button--secondary connection-oauth" href="/api/admin/oauth/${encodeURIComponent(connection.connection_id)}/start" data-connection="${escapeHtml(connection.connection_id)}" target="_blank" rel="noopener">${status === 'ready' ? 'Finish setup' : 'Reconnect'}</a>`
      : connection.provider === 'zoom' && status === 'ready'
        ? `<button class="dk-button dk-button--secondary connection-finish" data-connection="${escapeHtml(connection.connection_id)}" type="button">Finish setup</button>` : '';
    const identity = accountIdentity(connection);
    const unfinished = !identity && verifiesIdentity(connection);
    const title = connection.provider === 'zoom' && !scopesOf(connection).length
      ? accountLabel(connection) : identity || (unfinished ? 'Not signed in' : accountLabel(connection));
    /* One sign-in can back several services; say so quietly instead of
       naming the internal connection_id, which nobody needs to read. */
    const others = servicesFor(connection).filter((service) => service.id !== serviceId)
      .map((service) => service.label.replace(/^Google /, ''));
    const shareHtml = others.length ? escapeHtml(`Same sign-in also covers ${others.join(', ')}`) : '';
    /* An abandoned setup is clutter: offer removal right on the row. */
    const remove = unfinished
      ? `<button class="dk-button dk-button--secondary connection-remove" data-connection="${escapeHtml(connection.connection_id)}" type="button">Remove</button>` : '';
    const expires = formatTimestamp(connection.token_expires_at);
    return `<li class="service-account">
    <div class="service-account-main">
      <span class="cell-name">${escapeHtml(title)}</span>
      ${shareHtml ? `<span class="service-share">${shareHtml}</span>` : ''}
      <span class="cell-sub">${escapeHtml(identity || (connection.provider === 'zoom' && !scopesOf(connection).length) ? usageLabel(connection) : 'Sign-in was never finished')}</span>
    </div>
    <div class="service-account-status">
      ${statusLine(status, CONNECTION_STATUS_LABELS)}
      ${!connection.auto_refresh && expires && status !== 'expired' ? `<span class="cell-sub${status === 'expiring' ? ' expiring' : ''}">expires ${escapeHtml(expires)}</span>` : ''}
    </div>
    <div class="service-account-actions">${nextAction}${remove}<button class="dk-button dk-button--secondary connection-edit" data-connection="${escapeHtml(connection.connection_id)}" type="button">Manage</button></div>
  </li>`;
}

$('#connection-search')?.addEventListener('input', () => renderConnections((state.data || {}).connections || []));
$('#connection-status-filter')?.addEventListener('change', () => renderConnections((state.data || {}).connections || []));

/* A Zoom webhook stays 'ready' until Zoom validates its callback URL:
   open Manage on the event-subscription block holding that URL. */
function openZoomWebhookSetup(connectionId) {
  openEditConnection(connectionId);
  const setup = $('#edit-zoom-setup');
  if (!setup || setup.hidden) return;
  setup.scrollIntoView({ block: 'center' });
  const url = $('#edit-zoom-url');
  url.tabIndex = -1;
  url.focus();
  window.getSelection?.()?.selectAllChildren(url);
}

function bindOAuthLinks() {
  $$('.connection-oauth').forEach((link) => link.addEventListener('click', (event) => {
    event.preventDefault();
    openOAuthWindow(link.href, link.dataset.connection);
  }));
}

function confirmRevoke(title, message, confirmLabel = 'Revoke') {
  const dialog = $('#connection-confirm-dialog');
  $('#connection-confirm-title').textContent = title;
  $('#connection-confirm-message').textContent = message;
  $('#connection-confirm-yes').textContent = confirmLabel;
  dialog.returnValue = '';
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true });
    dialog.showModal();
  });
}

$('#add-connection').addEventListener('click', () => {
  addPickerOpen = $('#connect-picker').hidden;
  $('#connect-picker').hidden = !addPickerOpen;
  $('#add-connection').setAttribute('aria-expanded', String(addPickerOpen));
  if (addPickerOpen) {
    const heading = $('#connect-picker h2');
    heading.setAttribute('tabindex', '-1');
    heading.scrollIntoView({ behavior: 'instant', block: 'start' });
    heading.focus({ preventScroll: true });
  }
});

$('#edit-connection-reconnect').addEventListener('click', (event) => {
  $('#edit-connection-dialog').close();
  event.preventDefault();
  openOAuthWindow(event.currentTarget.href, $('#edit-connection-form').dataset.connectionId);
});

/* Delete removes the connection record outright — revoke only clears the
   tokens and leaves the row. Built here rather than in index.html because
   only this view needs it (the same call as the token-result dialog). The
   confirm names the referencing flows; the API re-checks server-side and
   answers 409 with the same list, so a stale view cannot delete a
   connection that just gained a reference. */
const editDeleteButton = document.getElementById('edit-connection-delete')
  || (() => {
    const button = document.createElement('button');
    button.id = 'edit-connection-delete';
    button.className = 'dk-button dk-button--danger';
    button.type = 'button';
    button.textContent = 'Delete';
    $('#edit-connection-revoke').after(button);
    return button;
  })();

/* Manage's Delete and an unfinished row's Remove share one path: confirm
   (naming the flows that would lose access), then DELETE through the API,
   which re-checks usage server-side and answers 409 for a stale view. */
async function deleteConnection(connectionId) {
  if (!connectionId) return;
  const connection = ((state.data || {}).connections || []).find((item) => item.connection_id === connectionId) || {};
  const name = accountLabel(connection);
  const refs = usageRefs(connection);
  const message = refs.length
    ? `${name} is still used by: ${refs.join(', ')}. Delete it anyway? Those flows and triggers lose access.`
    : `Delete ${name}? Its stored credential and access grants go with it. This cannot be undone.`;
  if (!await confirmRevoke('Delete connection', message, 'Delete')) return false;
  await api(`/api/admin/connections/${encodeURIComponent(connectionId)}${refs.length ? '?force=1' : ''}`, { method: 'DELETE' });
  notice(`Deleted ${name}`);
  await refreshConnections();
  return true;
}

editDeleteButton.addEventListener('click', async () => {
  editDeleteButton.disabled = true;
  editDeleteButton.textContent = 'Deleting…';
  $('#edit-connection-error').textContent = '';
  try {
    if (await deleteConnection($('#edit-connection-form').dataset.connectionId)) {
      $('#edit-connection-dialog').close();
    }
  } catch (error) {
    $('#edit-connection-error').textContent = error.message;
  } finally {
    editDeleteButton.disabled = false;
    editDeleteButton.textContent = 'Delete';
  }
});

/* Manage's details block: where the connection is used and how to address
   it from the CLI. The internal connection_id is never shown. */
function renderManageDetails(connection, refs) {
  const meta = $('#edit-connection-meta');
  let details = document.getElementById('edit-connection-details');
  if (!details) {
    details = document.createElement('div');
    details.id = 'edit-connection-details';
    details.className = 'connection-details';
    meta.after(details);
  }
  const usage = refs.length
    ? `<p class="connection-details-label">Used in ${refs.length} flow${refs.length === 1 ? '' : 's'}</p>
       <ul class="connection-usage">${refs.map((ref) => {
         const entry = (connection.used_in || []).find((item) => String(item.ref) === ref) || {};
         return entry.kind === 'workflow'
           ? `<li><a href="/workflows/${encodeURIComponent(ref)}">${escapeHtml(ref)}</a></li>`
           : `<li>${escapeHtml(ref)} <span class="sub">(${escapeHtml(entry.kind || 'trigger')})</span></li>`;
       }).join('')}</ul>`
    : '<p class="connection-details-label">Not used by any flow</p>';
  const cliRef = (connection.refs || [])[0];
  details.innerHTML = `${usage}
    ${cliRef ? `<p class="sub">CLI: <code>dapier token exec ${escapeHtml(cliRef)} --agent …</code></p>` : ''}`;
}

$('#edit-connection-revoke').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  const connectionId = $('#edit-connection-form').dataset.connectionId;
  if (!await confirmRevoke('Revoke connection access',
    `Revoke access for ${connectionId} in Dapier? You will need to connect it again.`)) return;
  button.disabled = true;
  button.textContent = 'Revoking…';
  try {
    await api(`/api/admin/connections/${encodeURIComponent(connectionId)}/tokens`, { method: 'DELETE' });
    $('#edit-connection-dialog').close();
    notice(`Access for ${connectionId} revoked in Dapier. Reconnect it to use the connection again.`);
    await refreshConnections();
  } catch (error) { $('#edit-connection-error').textContent = error.message; }
  finally { button.disabled = false; button.textContent = $('#edit-connection-form').dataset.provider === 'zoom' ? 'Disable webhook' : 'Revoke tokens'; }
});

$('#edit-connection-test').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  const connectionId = $('#edit-connection-form').dataset.connectionId;
  const result = $('#edit-connection-test-result');
  button.disabled = true;
  button.textContent = 'Testing…';
  result.hidden = true;
  try {
    const verdict = await api(`/api/admin/connections/${encodeURIComponent(connectionId)}/test`, { method: 'POST' });
    const identity = verdict.identity?.name ? ` — ${verdict.identity.name}` : '';
    result.textContent = `${verdict.ok ? 'OK' : 'Failed'} — ${verdict.detail || 'no health check for this provider'}${identity}`;
    result.hidden = false;
    result.classList.toggle('form-error', !verdict.ok);
  } catch (error) {
    result.textContent = error.message;
    result.hidden = false;
    result.classList.add('form-error');
  } finally {
    button.disabled = false;
    button.textContent = 'Test';
  }
});

/* Explore data: Zapier-style discovery over the connection's provider —
   the same api.discovery domain `dapier connections discover` drives.
   Select values are bare resource names: the admin route matches one
   [a-z0-9_-]+ segment and the domain resolves the connector itself. */
let discoverCatalog = { connectionId: '', resources: [] };

function discoverResource(key) {
  return discoverCatalog.resources.find((entry) => entry.name === key);
}

function renderDiscoverResource() {
  const resource = discoverResource($('#discover-resource').value);
  const description = $('#discover-resource-description');
  description.hidden = !resource?.description;
  description.textContent = resource?.description || '';
  const params = $('#discover-params');
  params.innerHTML = (resource?.params || []).map((param) => `
    <label>${escapeHtml(param.label || param.key)}${param.required ? '' : ' <span class="muted-cell">(optional)</span>'}<input name="param-${escapeHtml(param.key)}" class="mono-input" value="${escapeHtml(param.default || '')}" ${param.required ? 'required' : ''}>${param.help ? `<small class="field-hint">${escapeHtml(param.help)}</small>` : ''}</label>`).join('');
  params.hidden = !(resource?.params || []).length;
  const result = $('#discover-result');
  result.hidden = true;
  result.innerHTML = '';
}

async function openDiscover(connectionId) {
  discoverCatalog = { connectionId, resources: [] };
  const connection = ((state.data || {}).connections || []).find((item) => item.connection_id === connectionId);
  $('#connection-discover-title').textContent = `Explore data — ${connection ? accountLabel(connection) : connectionId}`;
  $('#connection-discover-meta').textContent = connection ? `${connection.provider} · ${connectionId}` : connectionId;
  $('#discover-error').textContent = '';
  $('#discover-result').hidden = true;
  $('#discover-result').innerHTML = '';
  $('#discover-resource').innerHTML = '<option value="">Loading…</option>';
  $('#discover-resource-description').hidden = true;
  $('#discover-params').hidden = true;
  $('#discover-params').innerHTML = '';
  $('#discover-run').disabled = true;
  $('#connection-discover-dialog').showModal();
  let catalog;
  try {
    catalog = await api(`/api/admin/connections/${encodeURIComponent(connectionId)}/discover`);
  } catch (error) {
    $('#discover-error').textContent = error.message;
    return;
  }
  discoverCatalog.resources = catalog.resources || [];
  if (!discoverCatalog.resources.length) {
    $('#discover-resource').innerHTML = '<option value="">No discoverable resources</option>';
    $('#discover-error').textContent = 'This provider exposes nothing to browse; use Test to check the connection instead.';
    return;
  }
  $('#discover-resource').innerHTML = discoverCatalog.resources.map((resource) =>
    `<option value="${escapeHtml(resource.name)}">${escapeHtml(resource.label || resource.name)}</option>`).join('');
  $('#discover-run').disabled = false;
  renderDiscoverResource();
}

function renderDiscoverItems(data) {
  const result = $('#discover-result');
  const items = data.items || [];
  const resolved = Object.entries(data.params || {}).map(([key, value]) => `${key}=${value}`).join(', ');
  const head = `<p class="sub mono">${escapeHtml(data.resource || '')}${resolved ? ` · ${escapeHtml(resolved)}` : ''} · ${items.length} item${items.length === 1 ? '' : 's'}</p>`;
  if (!items.length) {
    result.innerHTML = `${head}<p class="detail-muted">No items came back for these parameters.</p>`;
    result.hidden = false;
    return;
  }
  const columns = [...new Set(items.flatMap((item) => Object.keys(item)))].slice(0, 5);
  const cellText = (value) => {
    const text = value == null ? '' : (typeof value === 'object' ? JSON.stringify(value) : String(value));
    return text.length > 120 ? `${text.slice(0, 119)}…` : text;
  };
  const rows = items.map((item) => `<tr>${columns.map((column) =>
    `<td class="mono" data-label="${escapeHtml(column)}">${escapeHtml(cellText(item[column]))}</td>`).join('')}</tr>`).join('');
  result.innerHTML = `${head}<div class="table-wrap"><table><thead><tr>${columns.map((column) =>
    `<th>${escapeHtml(column)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;
  result.hidden = false;
}

$('#discover-resource').addEventListener('change', renderDiscoverResource);

$('#discover-run').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  const key = $('#discover-resource').value;
  const connectionId = discoverCatalog.connectionId;
  if (!connectionId || !key || button.disabled) return;
  const query = new URLSearchParams();
  for (const param of discoverResource(key)?.params || []) {
    const value = $(`#discover-params [name="param-${param.key}"]`)?.value.trim();
    if (value) query.set(param.key, value);
  }
  button.disabled = true;
  button.textContent = 'Fetching…';
  $('#discover-error').textContent = '';
  try {
    const data = await api(`/api/admin/connections/${encodeURIComponent(connectionId)}/discover/${encodeURIComponent(key)}?${query}`);
    renderDiscoverItems(data);
  } catch (error) {
    $('#discover-error').textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = 'Fetch items';
  }
});

$('#edit-connection-discover').addEventListener('click', () => {
  const connectionId = $('#edit-connection-form').dataset.connectionId;
  if (!connectionId) return;
  $('#edit-connection-dialog').close();
  void openDiscover(connectionId);
});

let shownGrants = [];

function setGrantStatus(message, error = false) {
  const status = $('#connection-grant-status');
  status.textContent = message;
  status.classList.toggle('form-error', error);
}

function renderGrantTokenChoices(selectedSubject = '') {
  const select = $('#connection-grant-token');
  select.disabled = false;
  const tokens = ((state.data || {}).api_tokens || []).filter((token) => !token.revoked_at);
  select.innerHTML = `<option value="manual">Enter another subject…</option>${tokens.map((token) => `<option value="${escapeHtml(`token:${token.token_id}`)}">${escapeHtml(token.token_id)} · ${escapeHtml(token.agent)}</option>`).join('')}`;
  select.value = tokens.some((token) => `token:${token.token_id}` === selectedSubject) ? selectedSubject : 'manual';
  $('#connection-grant-subject-field').hidden = select.value !== 'manual';
  $('#connection-grant-form').agent.readOnly = select.value !== 'manual';
  if (select.value !== 'manual') $('#connection-grant-form').subject.value = select.value;
}

$('#connection-grant-token').addEventListener('change', (event) => {
  const form = $('#connection-grant-form');
  const subject = event.currentTarget.value;
  $('#connection-grant-subject-field').hidden = subject !== 'manual';
  if (subject !== 'manual') {
    form.subject.value = subject;
    form.agent.readOnly = true;
    const tokenId = subject.slice('token:'.length);
    const token = ((state.data || {}).api_tokens || []).find((item) => item.token_id === tokenId);
    if (token) form.agent.value = token.agent;
  } else {
    form.subject.value = '';
    form.agent.readOnly = false;
    form.subject.focus();
  }
});

function resetGrantForm() {
  const form = $('#connection-grant-form');
  form.reset();
  form.subject.readOnly = false;
  form.agent.readOnly = false;
  form.dataset.grantee = '';
  $('#connection-grant-heading').textContent = 'Add grant';
  $('#connection-grant-cancel').hidden = true;
  $('#connection-grant-error').textContent = '';
  setGrantStatus('');
  renderGrantTokenChoices();
}

function localDateTime(epochSeconds) {
  if (!epochSeconds) return '';
  const date = new Date(Number(epochSeconds) * 1000);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function renderGrants() {
  $('#connection-grant-list').innerHTML = shownGrants.length ? shownGrants.map((grant) => `
    <div class="grant-row">
      <div><strong>${escapeHtml(grant.subject)}</strong><span class="grant-meta">${escapeHtml(grant.agent)} · ${escapeHtml((grant.operations || []).join(', '))}${grant.expires_at ? ` · expires ${escapeHtml(formatTimestamp(grant.expires_at))}` : ''}</span></div>
      <div class="grant-actions"><button class="dk-button dk-button--secondary grant-edit" data-grantee="${escapeHtml(grant.grantee)}" type="button">Edit</button><button class="dk-button dk-button--secondary grant-delete" data-grantee="${escapeHtml(grant.grantee)}" type="button">Revoke</button></div>
    </div>`).join('') : '<p class="detail-muted">No grants yet. Only operators can use this connection until you add one.</p>';
  $$('.grant-edit').forEach((button) => button.addEventListener('click', () => {
    const grant = shownGrants.find((item) => item.grantee === button.dataset.grantee);
    if (!grant) return;
    const form = $('#connection-grant-form');
    form.subject.value = grant.subject;
    form.agent.value = grant.agent;
    form.subject.readOnly = true;
    form.agent.readOnly = true;
    form.dataset.grantee = grant.grantee;
    renderGrantTokenChoices(grant.subject);
    // Grant identity is subject + agent. Editing permissions must retain both,
    // including manual subjects absent from the active-token picker.
    form.agent.readOnly = true;
    $('#connection-grant-token').disabled = true;
    $$('input[name="operations"]', form).forEach((input) => { input.checked = (grant.operations || []).includes(input.value); });
    form.expires_at.value = localDateTime(grant.expires_at);
    $('#connection-grant-heading').textContent = 'Edit grant';
    $('#connection-grant-cancel').hidden = false;
    $('#connection-grant-error').textContent = '';
    setGrantStatus('');
    form.scrollIntoView({ block: 'nearest' });
  }));
  $$('.grant-delete').forEach((button) => button.addEventListener('click', async () => {
    const connectionId = $('#connection-grant-form').dataset.connectionId;
    if (!await confirmRevoke('Revoke access grant',
      `Revoke ${button.dataset.grantee} on ${connectionId}?`)) return;
    button.disabled = true;
    button.textContent = 'Revoking…';
    try {
      await api(`/api/admin/grants?connection_id=${encodeURIComponent(connectionId)}&grantee=${encodeURIComponent(button.dataset.grantee)}`, { method: 'DELETE' });
      await loadGrants(connectionId);
      resetGrantForm();
      setGrantStatus('Grant revoked.');
    } catch (error) { $('#connection-grant-error').textContent = error.message; }
    finally { button.disabled = false; button.textContent = 'Revoke'; }
  }));
}

async function loadGrants(connectionId) {
  const data = await api(`/api/admin/grants?connection_id=${encodeURIComponent(connectionId)}`);
  shownGrants = data.grants || [];
  renderGrants();
}

export async function openAccessGrants(connectionId, prefill = null) {
  const connection = ((state.data || {}).connections || []).find((item) => item.connection_id === connectionId);
  resetGrantForm();
  $('#connection-grant-form').dataset.connectionId = connectionId;
  $('#connection-access-title').textContent = `Access · ${connection ? accountLabel(connection) : connectionId}`;
  await loadGrants(connectionId);
  if ($('#edit-connection-dialog').open) $('#edit-connection-dialog').close();
  $('#connection-access-dialog').showModal();
  if (prefill) {
    const form = $('#connection-grant-form');
    renderGrantTokenChoices(prefill.subject);
    form.subject.value = prefill.subject;
    form.agent.value = prefill.agent;
    $('#connection-grant-heading').textContent = `Grant ${prefill.subject}`;
    form.agent.focus();
  }
}

$('#edit-connection-access').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  const connectionId = $('#edit-connection-form').dataset.connectionId;
  button.disabled = true;
  button.textContent = 'Loading grants…';
  try { await openAccessGrants(connectionId); }
  catch (error) { $('#edit-connection-error').textContent = error.message; }
  finally { button.disabled = false; button.textContent = 'Access grants'; }
});

$('#connection-grant-cancel').addEventListener('click', resetGrantForm);

$('#connection-grant-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = $('#connection-access-dialog [type="submit"][form="connection-grant-form"]');
  if (submit.disabled) return;
  const operations = $$('input[name="operations"]:checked', form).map((input) => input.value);
  $('#connection-grant-error').textContent = '';
  if (!operations.length) {
    $('#connection-grant-error').textContent = 'Choose at least one operation.';
    return;
  }
  submit.disabled = true;
  submit.textContent = 'Saving…';
  const body = {
    connection_id: form.dataset.connectionId,
    subject: form.subject.value.trim(),
    agent: form.agent.value.trim(),
    operations,
  };
  if (form.expires_at.value) body.expires_at = Math.floor(new Date(form.expires_at.value).getTime() / 1000);
  try {
    await api('/api/admin/grants', { method: 'PUT', body: JSON.stringify(body) });
    resetGrantForm();
    await loadGrants(body.connection_id);
    setGrantStatus('Grant saved.');
  } catch (error) { $('#connection-grant-error').textContent = error.message; }
  finally { submit.disabled = false; submit.textContent = 'Save grant'; }
});

export { TOKEN_PROVIDERS, TOKEN_PROVIDER_META, renderConnections, nextConnectionId, notice };

$('#connection-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  submit.textContent = 'Connecting…';
  const provider = form.dataset.provider || 'slack';
  const meta = TOKEN_PROVIDER_META[provider] || TOKEN_PROVIDER_META.slack;
  $('#connection-error').textContent = '';
  const { id, suffix } = nextConnectionId(meta.connectionId || (provider === 'slack' ? 'slack' : 'telegram-bot'));
  const body = {
    connection_id: id,
    provider,
    token: form.token.value,
  };
  // Zoom verifies via its webhook callback, not at save time, so it keeps a
  // plain label; Slack and Telegram are verified here and take their
  // workspace / bot identity as the display name (see mark_connected).
  if (provider === 'zoom') body.display_name = suffix ? `${meta.displayName} ${suffix}` : meta.displayName;
  try {
    await api('/api/admin/connections', { method: 'PUT', body: JSON.stringify(body) });
    form.token.value = '';
    $('#connection-dialog').close();
    notice(provider === 'zoom' ? 'Zoom connection created. Add its callback URL in Zoom.' : `${meta.displayName} connected`);
    await refreshConnections();
    if (provider === 'zoom') openEditConnection(id);
  } catch (error) { $('#connection-error').textContent = error.message; }
  finally { submit.disabled = false; submit.textContent = 'Create connection'; }
});

$('#edit-connection-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  submit.textContent = 'Saving…';
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
  if (provider === 'slack') {
    const signingSecret = form.signing_secret.value.trim();
    if (signingSecret) body.signing_secret = signingSecret;
  }
  if (provider === 'dropbox') body.root_path = form.root_path.value.trim();
  const connection = ((state.data || {}).connections || []).find((item) => item.connection_id === connectionId);
  if (connection && connection.expected_account_id) body.expected_account_id = connection.expected_account_id;
  try {
    await api('/api/admin/connections', { method: 'PUT', body: JSON.stringify(body) });
    form.token.value = '';
    form.signing_secret.value = '';
    $('#edit-connection-dialog').close();
    notice('Connection updated');
    await refreshConnections();
  } catch (error) { $('#edit-connection-error').textContent = error.message; }
  finally { submit.disabled = false; submit.textContent = 'Save changes'; }
});
