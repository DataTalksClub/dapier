/* Connections view: the create-new provider grid and the accounts table. */
import { state } from '../state.js';
import { $, $$, icons, notice, providerMark } from '../ui.js';
import { api } from '../api.js';
import { detailRows, escapeHtml, formatTimestamp, statusLine } from '../format.js';
import { refresh } from './overview.js';

const CONNECT_PROVIDERS = {
  google: {
    label: 'Google Calendar',
    blurb: 'Calendar listings, event triggers, and owned-event edits for the scheduling flows.',
    connectionId: 'google-calendar',
    displayName: 'Google Calendar',
    scopes: ['https://www.googleapis.com/auth/calendar.freebusy', 'https://www.googleapis.com/auth/calendar.events.owned', 'https://www.googleapis.com/auth/calendar.readonly', 'https://www.googleapis.com/auth/userinfo.email'],
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
    scopes: ['account_info.read', 'files.metadata.read', 'files.content.read', 'files.content.write'],
  },
  zoom: {
    label: 'Zoom',
    blurb: 'Start workflows when a Zoom cloud recording video finishes processing.',
    connectionId: 'zoom',
    displayName: 'Zoom recordings',
    scopes: [],
  },
  slack: {
    label: 'Slack',
    blurb: 'Connect a Slack account for agent access with a bot or user token. Workflow Slack actions can also use the shared Service credential.',
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

/* Provider names for table group headers — wider than the connect cards
   (a "google" group holds calendar, Drive, and Sheets connections alike). */
const PROVIDER_LABELS = {
  google: 'Google',
  youtube: 'YouTube',
  dropbox: 'Dropbox',
  zoom: 'Zoom',
  slack: 'Slack',
  telegram: 'Telegram',
};

const providerLabel = (provider) => PROVIDER_LABELS[provider] || provider;

/* Token providers paste a credential instead of browser consent. */
const TOKEN_PROVIDERS = ['slack', 'telegram', 'zoom'];

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

/* Each connection is individually connected, awaiting consent, or expired
   (needs refreshing) — say so in plain words, not raw DynamoDB values. */
const CONNECTION_STATUS_LABELS = {
  ready: 'setup incomplete',
  expired: 'needs reconnection',
  revoked: 'revoked',
};

/* A stored token can expire while the record still reads 'connected' — the
   API flags that via health; treat such rows as needing reconnection
   everywhere the list aggregates, not just in the row rendering. */
const effectiveStatus = (connection) =>
  (connection.health === 'expired' && connection.status === 'connected' ? 'expired' : connection.status);
const needsAttention = (connection) => ['ready', 'expired', 'revoked'].includes(effectiveStatus(connection));

let addPickerOpen = false;

/* Server-paged accounts table: the overview snapshot clips at its scan
   limit, so once this page is open the view fetches /api/admin/connections
   (the paged list the API owns) and Load more appends the next page through
   the paging token — the same pattern as the runs view. Until the fetch
   lands, the table shows the snapshot (state.data.connections); the
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
  button.className = 'button secondary';
  button.type = 'button';
  button.hidden = true;
  button.textContent = 'Load more';
  $('#connection-table')?.closest('.table-wrap')?.after(button);
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
  $('#connect-grid').innerHTML = Object.entries(CONNECT_PROVIDERS).map(([provider, meta]) => {
    const clientProvider = provider === 'youtube' ? 'google' : provider;
    const oauthClient = ((state.data || {}).oauth_clients || []).find((item) => item.provider === clientProvider);
    const needsClient = !TOKEN_PROVIDERS.includes(provider) && oauthClient && !oauthClient.configured;
    const pending = !TOKEN_PROVIDERS.includes(provider)
      ? connections.find((connection) => connection.provider === provider && connection.status === 'ready') : null;
    const accounts = connections.filter((connection) => connection.provider === provider);
    const action = pending
      ? `<a class="button primary connection-oauth" href="/api/admin/oauth/${encodeURIComponent(pending.connection_id)}/start" data-connection="${escapeHtml(pending.connection_id)}" target="_blank" rel="noopener">Finish setup</a>
         <button class="button secondary connect-button" data-provider="${provider}" type="button">Add another account</button>`
      : `<button class="button secondary connect-button" data-provider="${provider}" type="button">${provider === 'zoom' ? 'Add Zoom app' : 'Add account'}</button>`;
    return `
    <div class="connect-card">
      <div class="connect-card-head"><span class="connect-title">${providerMark(provider)}<span class="connect-name">${meta.label}</span></span>${accounts.length > 1 ? `<span class="connect-count">${accounts.length} accounts</span>` : ''}</div>
      <p class="connect-blurb">${meta.blurb}</p>
      ${needsClient ? `<p class="connect-pending">Set up the ${escapeHtml(clientProvider)} OAuth client in <a href="/credentials">Credentials</a> before consent.</p>` : ''}
      ${pending ? `<p class="connect-pending">${escapeHtml(pending.display_name || pending.connection_id)} is waiting for setup.</p>` : ''}
      <div class="connect-card-actions">${action}</div>
    </div>`;
  }).join('');
  $$('.connect-button').forEach((button) => button.addEventListener('click', async () => {
    if (button.disabled) return;
    button.disabled = true;
    const label = button.textContent;
    button.textContent = 'Starting…';
    try { await connectProvider(button.dataset.provider); }
    finally { button.disabled = false; button.textContent = label; }
  }));
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
  const { id } = nextConnectionId(meta.connectionId);
  // Open during the click: browsers block windows opened after the PUT awaits.
  const popup = window.open('', '_blank', 'width=680,height=760');
  if (!popup) return notice('Allow pop-ups to add this connection, then try again.', true);
  popup.document.title = `Connect ${meta.label}`;
  if (popup.document.body) popup.document.body.textContent = 'Preparing connection…';
  const stopWatching = watchOAuthPopup(popup, id);
  try {
    // Provision the new record with the provider's standard scopes, then
    // bounce straight to the consent screen. No display name: once consent
    // verifies the account, the record takes the verified identity (the
    // account email) as its name — that is what tells same-provider
    // accounts apart, not a "Google Calendar 2" counter.
    await api('/api/admin/connections', {
      method: 'PUT',
      body: JSON.stringify({
        connection_id: id,
        provider,
        scopes: meta.scopes,
      }),
    });
    const startUrl = `/api/admin/oauth/${encodeURIComponent(id)}/start`;
    if (!popup.closed) {
      popup.location.assign(startUrl);
      popup.focus();
    } else {
      notice('Connection created. Use Connect in the accounts table to finish setup.');
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
  $('#edit-connection-title').textContent = `Manage ${connection.display_name || connection.connection_id}`;
  $('#edit-connection-meta').textContent = `${connection.provider} · ${connection.connection_id}${refs.length ? ` · used by ${refs.length === 1 ? '1 flow/trigger' : `${refs.length} flows/triggers`}` : ' · unused'}`;
  form.display_name.value = connection.display_name || connection.connection_id;
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
  reconnect.hidden = TOKEN_PROVIDERS.includes(connection.provider) || connection.status === 'ready';
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
  $('.table-wrap', $('[data-page=connections]')).hidden = connections.length === 0;
  const query = ($('#connection-search')?.value || '').trim().toLowerCase();
  const statusFilter = $('#connection-status-filter')?.value || 'all';
  const filtered = connections.filter((connection) => {
    if (statusFilter === 'connected' && connection.status !== 'connected') return false;
    if (statusFilter === 'attention' && !needsAttention(connection)) return false;
    if (!query) return true;
    return [connection.display_name, connection.connection_id, connection.provider,
      connection.account_title, connection.verified_account_id,
      ...(connection.used_in || []).map((entry) => entry.ref)].some((value) => String(value || '').toLowerCase().includes(query));
  });
  $('#connection-filter-empty').hidden = filtered.length > 0 || connections.length === 0;
  const withinGroup = (a, b) =>
    String(a.display_name || a.connection_id).localeCompare(String(b.display_name || b.connection_id));
  const groups = new Map();
  for (const connection of filtered) {
    groups.set(connection.provider, [...(groups.get(connection.provider) || []), connection]);
  }
  /* One section per provider — every group gets a header, so single-account
     providers read the same as multi-account ones and the table scans as a
     register of services. Rows order alphabetically; attention states surface
     through the status column, the header meta, and the summary line. */
  $('#connection-table').innerHTML = [...groups.entries()].sort(([a], [b]) =>
    providerLabel(a).localeCompare(providerLabel(b))).map(([provider, group]) => {
    const rows = [...group].sort(withinGroup).map(connectionRow).join('');
    const attention = group.filter(needsAttention).length;
    const meta = [
      group.length > 1 ? `${group.length} account${group.length === 1 ? '' : 's'}` : '',
      attention ? `${attention} ${attention === 1 ? 'needs' : 'need'} attention` : '',
    ].filter(Boolean).join(' · ');
    return `<tr class="provider-group-row"><th colspan="3" scope="colgroup">${providerMark(provider)}<span class="provider-group-name">${escapeHtml(providerLabel(provider))}</span>${meta ? `<span class="provider-group-meta">${escapeHtml(meta)}</span>` : ''}</th></tr>${rows}`;
  }).join('');
  $$('.connection-edit').forEach((button) => button.addEventListener('click', () => openEditConnection(button.dataset.connection)));
  $$('.provider-token-button').forEach((button) => button.addEventListener('click', () => issueConnectionToken(button)));
  bindOAuthLinks();
}

/* Which live workflows and hook triggers reference the connection — the
   API computes used_in (workflow definitions + hook bindings) so every
   surface, not just the console, agrees on what a connection is for and
   whether deleting it would break something. */
function usageRefs(connection) {
  return [...new Set((connection.used_in || []).map((entry) => String(entry.ref)))];
}

function usageLabel(connection) {
  const refs = usageRefs(connection);
  if (!refs.length) return 'Not used by any flow';
  const shown = refs.slice(0, 3).join(', ');
  return `Used in: ${shown}${refs.length > 3 ? ` +${refs.length - 3}` : ''}`;
}

function connectionRow(connection) {
    /* health is computed by the API from the stored token expiry; an expired
       token turns a connected row into "needs reconnection" (the label the
       status map already carried) without rewriting the stored record. */
    const status = effectiveStatus(connection);
    const nextAction = !TOKEN_PROVIDERS.includes(connection.provider) && status !== 'connected'
      ? `<a class="button ${status === 'ready' ? 'primary' : 'secondary'} connection-oauth" href="/api/admin/oauth/${encodeURIComponent(connection.connection_id)}/start" data-connection="${escapeHtml(connection.connection_id)}" target="_blank" rel="noopener">${status === 'ready' ? 'Finish setup' : 'Reconnect'}</a>` : '';
    /* Console mirror of `dapier token exec`: only OAuth connections hold a
       refreshable provider access token — token providers (slack, telegram,
       zoom) keep a pasted secret, and the shared domain call 502s for them. */
    const tokenAction = status === 'connected' && !TOKEN_PROVIDERS.includes(connection.provider)
      ? `<button class="button secondary provider-token-button" data-connection="${escapeHtml(connection.connection_id)}" type="button">Get token</button>` : '';
    const identity = connection.account_title || connection.verified_account_id;
    const expires = formatTimestamp(connection.token_expires_at);
    return `<tr>
    <td class="cell-title"><span class="cell-name">${escapeHtml(connection.display_name || connection.connection_id)}</span><span class="cell-sub">${identity ? escapeHtml(identity) : 'No account verified yet'}</span><span class="cell-sub muted-cell">${escapeHtml(usageLabel(connection))}</span></td>
    <td data-label="Status">${statusLine(status, CONNECTION_STATUS_LABELS)}${expires && status !== 'expired' ? `<span class="cell-sub muted-cell">token expires ${escapeHtml(expires)}</span>` : ''}</td>
    <td class="action-cell">${nextAction}${tokenAction}<button class="button secondary connection-edit" data-connection="${escapeHtml(connection.connection_id)}" type="button">Manage</button></td>
  </tr>`;
}

$('#connection-search')?.addEventListener('input', () => renderConnections((state.data || {}).connections || []));
$('#connection-status-filter')?.addEventListener('change', () => renderConnections((state.data || {}).connections || []));

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
    button.className = 'button danger';
    button.type = 'button';
    button.textContent = 'Delete';
    $('#edit-connection-revoke').after(button);
    return button;
  })();

editDeleteButton.addEventListener('click', async () => {
  const form = $('#edit-connection-form');
  const connectionId = form.dataset.connectionId;
  if (!connectionId) return;
  const connection = ((state.data || {}).connections || []).find((item) => item.connection_id === connectionId);
  const refs = usageRefs(connection || {});
  const message = refs.length
    ? `${connectionId} is still used by: ${refs.join(', ')}. Delete it anyway? Those flows and triggers lose access.`
    : `Delete ${connectionId}? Its stored credential and access grants go with it. This cannot be undone.`;
  if (!await confirmRevoke('Delete connection', message, 'Delete')) return;
  editDeleteButton.disabled = true;
  editDeleteButton.textContent = 'Deleting…';
  $('#edit-connection-error').textContent = '';
  try {
    await api(`/api/admin/connections/${encodeURIComponent(connectionId)}${refs.length ? '?force=1' : ''}`, { method: 'DELETE' });
    $('#edit-connection-dialog').close();
    notice(`Deleted ${connectionId}`);
    await refreshConnections();
  } catch (error) {
    $('#edit-connection-error').textContent = error.message;
  } finally {
    editDeleteButton.disabled = false;
    editDeleteButton.textContent = 'Delete';
  }
});

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
    result.textContent = `${verdict.ok ? '✓' : '✗'} ${verdict.detail || 'no health check for this provider'}${identity}`;
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

/* Fresh provider token: the console mirror of `dapier token exec` —
   POST /api/admin/connections/{id}/token drives the same tokens domain the
   agent API uses. The value is a live credential shown once; this dialog is
   built here rather than in index.html because only this view needs it. */
let tokenResultDialog = null;

function tokenResultElements() {
  if (tokenResultDialog) return tokenResultDialog;
  const dialog = document.createElement('dialog');
  dialog.id = 'provider-token-dialog';
  dialog.setAttribute('aria-labelledby', 'provider-token-title');
  dialog.innerHTML = `
    <div class="dialog-head">
      <h2 id="provider-token-title">Provider token</h2>
      <button class="icon-button dialog-close" type="button" aria-label="Close"><i data-lucide="x"></i></button>
    </div>
    <div class="dialog-body">
      <p id="provider-token-meta" class="sub mono"></p>
      <p class="sub">This is a live credential for the provider account — treat it like a password. It is shown once; closing this dialog clears it.</p>
      <div id="provider-token-reveal" class="token-reveal" hidden>
        <code id="provider-token-value" class="mono"></code>
        <button id="provider-token-copy" class="button secondary" type="button">Copy</button>
      </div>
      <dl id="provider-token-details"></dl>
      <p id="provider-token-error" class="form-error" role="alert"></p>
    </div>
    <div class="dialog-actions">
      <button class="button secondary dialog-close" type="button">Close</button>
    </div>`;
  document.body.appendChild(dialog);
  icons();
  // main.js binds .dialog-close only at load, so this dynamic dialog binds its own.
  $$('.dialog-close', dialog).forEach((button) => button.addEventListener('click', () => dialog.close()));
  $('#provider-token-copy').addEventListener('click', async () => {
    const value = $('#provider-token-value').textContent;
    try {
      await navigator.clipboard.writeText(value);
      $('#provider-token-copy').textContent = 'Copied';
      setTimeout(() => { $('#provider-token-copy').textContent = 'Copy'; }, 2000);
    } catch (_) {
      // Clipboard refused (insecure context): fall back to manual selection.
      const range = document.createRange();
      range.selectNodeContents($('#provider-token-value'));
      const selection = getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
  });
  // The token never outlives the dialog: any close (button, Esc) wipes it.
  dialog.addEventListener('close', () => {
    $('#provider-token-value').textContent = '';
    $('#provider-token-details').innerHTML = '';
  });
  tokenResultDialog = dialog;
  return dialog;
}

/* api() surfaces only the response body's error text, not the status, so the
   route's per-status copy is matched on that text: a 409 binding mismatch
   names the provider account and 502's body is exactly the string asked of
   this view; everything else falls back to one generic line. */
function tokenIssueErrorMessage(error) {
  const message = error?.message || '';
  if (message === 'Provider token is unavailable' || /provider account/i.test(message)) return message;
  return 'Could not get a provider token. Try again.';
}

function showConnectionTokenResult(connectionId, result, error) {
  const dialog = tokenResultElements();
  const connection = ((state.data || {}).connections || []).find((item) => item.connection_id === connectionId);
  $('#provider-token-title').textContent = error ? 'Provider token unavailable' : 'Provider token';
  $('#provider-token-meta').textContent = `${connection?.provider || 'connection'} · ${connectionId}`;
  $('#provider-token-reveal').hidden = !result;
  $('#provider-token-value').textContent = result ? result.access_token : '';
  $('#provider-token-details').innerHTML = result ? detailRows([
    ['Provider account', [result.account_title, result.provider_account_id].filter(Boolean).join(' · ')],
    ['Scope', result.scope],
    ['Expires', formatTimestamp(result.expires_at)],
    ['Refreshed', result.refreshed ? 'Yes — a fresh token was fetched from the provider' : 'No — the connection\'s current token'],
  ]) : '';
  $('#provider-token-error').textContent = error ? tokenIssueErrorMessage(error) : '';
  if (!dialog.open) dialog.showModal();
}

async function issueConnectionToken(button) {
  const connectionId = button.dataset.connection;
  button.disabled = true;
  button.textContent = 'Fetching…';
  try {
    const result = await api(`/api/admin/connections/${encodeURIComponent(connectionId)}/token`, { method: 'POST' });
    showConnectionTokenResult(connectionId, result, null);
  } catch (error) {
    showConnectionTokenResult(connectionId, null, error);
  } finally {
    button.disabled = false;
    button.textContent = 'Get token';
  }
}

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
  $('#connection-discover-title').textContent = `Explore data — ${connection?.display_name || connectionId}`;
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

/* Pull a trigger sample: one POST /api/admin/discover, the same dispatch
   `dapier triggers sample` drives — a realistic event envelope (live from
   the chosen account, else the newest recorded run, else a documented
   example) or a field's option list. Read-only: nothing is stored or run.
   Connector and field-option names come from the catalog's discovery
   fragment, so the console list cannot drift from what the API accepts. */
let sampleCatalog = null;

function sampleConnectorLabel(name) {
  const entry = ((sampleCatalog || {}).connectors || []).find((connector) => connector.name === name);
  return entry?.label || name;
}

function renderSampleConnections() {
  const connections = ((state.data || {}).connections || []);
  $('#sample-connection').innerHTML = ['<option value="">(no account — example or recorded run)</option>']
    .concat(connections.map((connection) =>
      `<option value="${escapeHtml(connection.connection_id)}">${escapeHtml(connection.display_name || connection.connection_id)} (${escapeHtml(connection.provider)})</option>`))
    .join('');
  $('#sample-connection-field').hidden = !connections.length;
}

function discoveryEntry(connector, resource) {
  return ((sampleCatalog || {}).discoveries || []).find((entry) =>
    entry.connector === connector && entry.name === resource);
}

function renderSampleFields() {
  const connector = $('#sample-connector').value;
  const events = (((sampleCatalog || {}).connectors || []).find((entry) => entry.name === connector) || {}).events || [];
  const resources = (((sampleCatalog || {}).discovery || {}).options || {})[connector] || [];
  $('#sample-event-field').hidden = !events.length;
  $('#sample-event').innerHTML = ['<option value="">(default)</option>']
    .concat(events.map((event) => `<option value="${escapeHtml(event)}">${escapeHtml(event)}</option>`)).join('');
  $('#sample-resource').innerHTML = ['<option value="">(event sample)</option>']
    .concat(resources.map((resource) => `<option value="${escapeHtml(resource)}">${escapeHtml(resource)}</option>`)).join('');
  $('#sample-resource-field').hidden = !resources.length;
  renderSampleParams();
  renderSampleConnections();
  $('#sample-result').hidden = true;
  $('#sample-result').innerHTML = '';
  $('#sample-error').textContent = '';
}

/* Option listings that take parameters (an s3 bucket, a spreadsheet id)
   carry them in the discovery call's event slot, "/"-joined positionally —
   the convention listing_params parses. Render one input per param from
   the catalog's discovery metadata so the console can drive those
   resources instead of 404ing on the missing bucket/spreadsheet. */
function renderSampleParams() {
  const connector = $('#sample-connector').value;
  const resource = $('#sample-resource-field').hidden ? '' : $('#sample-resource').value;
  const entry = resource ? discoveryEntry(connector, resource) : null;
  const params = (entry?.params || []).filter((param) => param.key !== 'connection_id');
  $('#sample-params').innerHTML = params.map((param) => `
    <label>${escapeHtml(param.label || param.key)}${param.required ? '' : ' <span class="muted-cell">(optional)</span>'}<input name="sample-param-${escapeHtml(param.key)}" class="mono-input" value="${escapeHtml(param.default || '')}">${param.help ? `<small class="field-hint">${escapeHtml(param.help)}</small>` : ''}</label>`).join('');
  $('#sample-params').hidden = !params.length;
}

function sampleParamEvent(connector, resource) {
  const entry = discoveryEntry(connector, resource);
  const params = (entry?.params || []).filter((param) => param.key !== 'connection_id');
  if (!params.length) return '';
  const values = params.map((param) =>
    ($(`#sample-params [name="sample-param-${param.key}"]`)?.value || '').trim());
  if (!values[0]) {
    throw new Error(`${params[0].label || params[0].key} is required for ${resource}`);
  }
  return values.join('/');
}

async function openSamplePuller() {
  $('#sample-error').textContent = '';
  $('#sample-result').hidden = true;
  $('#sample-result').innerHTML = '';
  $('#sample-run').disabled = true;
  $('#sample-connector').innerHTML = '<option value="">Loading…</option>';
  $('#trigger-sample-dialog').showModal();
  if (!sampleCatalog) {
    try {
      sampleCatalog = await api('/api/admin/designer/catalog');
    } catch (error) {
      $('#sample-connector').innerHTML = '';
      $('#sample-error').textContent = error.message;
      return;
    }
  }
  const connectors = ((sampleCatalog || {}).discovery || {}).sample || [];
  if (!connectors.length) {
    $('#sample-connector').innerHTML = '<option value="">No sample connectors</option>';
    $('#sample-error').textContent = 'No trigger connector exposes a sample pull.';
    return;
  }
  $('#sample-connector').innerHTML = connectors.map((name) =>
    `<option value="${escapeHtml(name)}">${escapeHtml(sampleConnectorLabel(name))}</option>`).join('');
  renderSampleFields();
  $('#sample-run').disabled = false;
}

function renderSampleResult(data, resource) {
  const result = $('#sample-result');
  const origin = data.connection_id ? ` · ${escapeHtml(data.connection_id)}` : '';
  if (resource) {
    const options = data.options || [];
    const rows = options.map((option) =>
      `<tr><td class="mono" data-label="Value">${escapeHtml(String(option.value ?? ''))}</td><td data-label="Label">${escapeHtml(String(option.label ?? option.value ?? ''))}</td></tr>`).join('');
    result.innerHTML = `<p class="sub mono">${escapeHtml(data.connector || '')} · ${escapeHtml(data.resource || resource)}${origin} · ${options.length} option${options.length === 1 ? '' : 's'}</p>
      ${options.length ? `<div class="table-wrap"><table><thead><tr><th>Value</th><th>Label</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="detail-muted">No options came back.</p>'}`;
  } else {
    const sample = data.sample || {};
    result.innerHTML = `<p class="sub mono">${escapeHtml(data.connector || '')}.${escapeHtml(sample.event || '')} [${escapeHtml(data.source || '')}]${origin} · ${escapeHtml(sample.occurred_at || '')}</p>
      <pre class="sample-json mono">${escapeHtml(JSON.stringify(sample.data || {}, null, 2))}</pre>`;
  }
  result.hidden = false;
}

$('#sample-connector').addEventListener('change', renderSampleFields);
$('#sample-resource').addEventListener('change', () => {
  renderSampleParams();
  $('#sample-result').hidden = true;
  $('#sample-result').innerHTML = '';
});

$('#sample-run').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  const connector = $('#sample-connector').value;
  if (!connector || button.disabled) return;
  const resource = $('#sample-resource-field').hidden ? '' : $('#sample-resource').value;
  const body = { connector, kind: resource ? 'options' : 'sample' };
  let paramEvent = '';
  if (resource) {
    body.resource = resource;
    try {
      paramEvent = sampleParamEvent(connector, resource);
      if (paramEvent) body.event = paramEvent;
    } catch (error) {
      $('#sample-error').textContent = error.message;
      return;
    }
  }
  const eventName = $('#sample-event-field').hidden ? '' : $('#sample-event').value;
  if (eventName && !paramEvent) body.event = eventName;
  const connectionId = $('#sample-connection').value;
  if (connectionId) body.connection_id = connectionId;
  const limit = $('#sample-limit').value.trim();
  if (limit) body.limit = Number(limit);
  button.disabled = true;
  button.textContent = 'Pulling…';
  $('#sample-error').textContent = '';
  try {
    const data = await api('/api/admin/discover', { method: 'POST', body: JSON.stringify(body) });
    renderSampleResult(data, resource);
  } catch (error) {
    $('#sample-error').textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = 'Pull sample';
  }
});

$('#pull-trigger-sample').addEventListener('click', () => void openSamplePuller());

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
      <div class="grant-actions"><button class="button secondary grant-edit" data-grantee="${escapeHtml(grant.grantee)}" type="button">Edit</button><button class="button secondary grant-delete" data-grantee="${escapeHtml(grant.grantee)}" type="button">Revoke</button></div>
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
  $('#connection-access-title').textContent = `Access · ${connection?.display_name || connectionId}`;
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

/* Import: bring an existing provider credential over through the same
   operator endpoint the CLI's `connections import` uses — token providers
   paste their token, OAuth providers paste the authorized-user JSON whose
   refresh_token is transferred (and verified) server-side. */
const IMPORT_PROVIDERS = {
  google: { kind: 'oauth', label: 'Google' },
  youtube: { kind: 'oauth', label: 'YouTube' },
  dropbox: { kind: 'oauth', label: 'Dropbox', rootPath: true },
  slack: { kind: 'token', label: 'Token', signingSecret: true },
  telegram: { kind: 'token', label: 'Bot token' },
  zoom: { kind: 'token', label: 'Secret Token' },
};

const IMPORT_CREDENTIAL_PLACEHOLDERS = {
  google: '{"refresh_token": "1//…"}',
  youtube: '{"refresh_token": "1//…"}',
  dropbox: '{"refresh_token": "…"}',
  slack: 'xoxb-…',
  telegram: '123456:ABC-…',
  zoom: 'Secret Token from Zoom Marketplace',
};

function syncImportFields() {
  const form = $('#import-connection-form');
  const meta = IMPORT_PROVIDERS[form.provider.value] || IMPORT_PROVIDERS.google;
  $('#import-credential-label').firstChild.textContent = meta.label;
  form.credential.placeholder = IMPORT_CREDENTIAL_PLACEHOLDERS[form.provider.value] || '';
  $('#import-credential-hint').textContent = meta.kind === 'oauth'
    ? 'The authorized-user JSON with the refresh_token to transfer.'
    : 'Pasted once, verified server-side, never logged.';
  $('#import-signing-field').hidden = !meta.signingSecret;
  $('#import-scopes-field').hidden = meta.kind !== 'oauth';
  $('#import-root-field').hidden = !meta.rootPath;
}

function openImportDialog() {
  const form = $('#import-connection-form');
  form.reset();
  form.provider.value = 'google';
  $('#import-connection-error').textContent = '';
  syncImportFields();
  $('#import-connection-dialog').showModal();
  form.connection_id.focus();
}

$('#import-connection').addEventListener('click', openImportDialog);
$('#import-connection-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  submit.textContent = 'Importing…';
  $('#import-connection-error').textContent = '';
  try {
    const provider = form.provider.value;
    const meta = IMPORT_PROVIDERS[provider] || IMPORT_PROVIDERS.google;
    const body = {
      connection_id: form.connection_id.value.trim(),
      provider,
    };
    if (form.display_name.value.trim()) body.display_name = form.display_name.value.trim();
    if (meta.kind === 'token') {
      body.token = form.credential.value.trim();
      if (meta.signingSecret && form.signing_secret.value.trim()) body.signing_secret = form.signing_secret.value.trim();
    } else {
      let authorized;
      try { authorized = JSON.parse(form.credential.value); } catch { throw new Error('The credential must be the authorized-user JSON with a refresh_token.'); }
      if (!authorized || typeof authorized !== 'object' || !authorized.refresh_token) throw new Error('The authorized-user JSON has no refresh_token.');
      body.authorized_user = { refresh_token: String(authorized.refresh_token) };
      if (form.client_id.value.trim()) body.client_id = form.client_id.value.trim();
      if (form.client_secret.value.trim()) body.client_secret = form.client_secret.value.trim();
      if (form.expected_account_id.value.trim()) body.expected_account_id = form.expected_account_id.value.trim();
      const scopes = form.scopes.value.trim().split(/[\s,]+/).filter(Boolean);
      if (scopes.length) body.scopes = scopes;
      if (meta.rootPath && form.root_path.value.trim()) body.root_path = form.root_path.value.trim();
    }
    await api('/api/admin/connections/import', { method: 'POST', body: JSON.stringify(body) });
    form.credential.value = '';
    form.signing_secret.value = '';
    form.client_secret.value = '';
    $('#import-connection-dialog').close();
    notice(`Imported ${body.connection_id}`);
    await refreshConnections();
  } catch (error) { $('#import-connection-error').textContent = error.message; }
  finally { submit.disabled = false; submit.textContent = 'Import connection'; }
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
