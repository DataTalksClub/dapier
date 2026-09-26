/* Connections view: the create-new provider grid and the accounts table. */
import { state } from '../state.js';
import { $, $$, notice, providerMark } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, formatTimestamp, statusLine } from '../format.js';
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
    blurb: 'Paste a bot (xoxb-…) or user (xoxp-…) token from your Slack app settings. This creates an account for agent access; the shared Slack Service credential for workflow actions is configured separately under Credentials.',
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

let addPickerOpen = false;

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
    await refresh();
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
      await refresh();
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
        await refresh();
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

function openEditConnection(connectionId) {
  const connection = ((state.data || {}).connections || []).find((item) => item.connection_id === connectionId);
  if (!connection) return;
  const form = $('#edit-connection-form');
  form.reset();
  form.dataset.connectionId = connection.connection_id;
  form.dataset.provider = connection.provider;
  $('#edit-connection-title').textContent = `Manage ${connection.display_name || connection.connection_id}`;
  $('#edit-connection-meta').textContent = `${connection.provider} · ${connection.connection_id}`;
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
  const reconnect = $('#edit-connection-reconnect');
  reconnect.hidden = TOKEN_PROVIDERS.includes(connection.provider) || connection.status === 'ready';
  reconnect.href = `/api/admin/oauth/${encodeURIComponent(connectionId)}/start`;
  $('#edit-connection-revoke').hidden = !(['connected', 'expired'].includes(connection.status) ||
    (connection.provider === 'zoom' && connection.status === 'ready'));
  $('#edit-connection-revoke').textContent = connection.provider === 'zoom' ? 'Disable webhook' : 'Revoke tokens';
  $('#edit-connection-access').hidden = connection.provider === 'zoom';
  $('#edit-connection-error').textContent = '';
  $('#edit-connection-dialog').showModal();
  form.display_name.focus();
}

function renderConnections(connections) {
  renderConnectCards(connections);
  const connected = connections.filter((connection) => connection.status === 'connected').length;
  const attention = connections.filter((connection) => ['ready', 'expired', 'revoked'].includes(connection.status)).length;
  $('#connection-summary').textContent = connections.length
    ? `${connected} connected · ${attention} ${attention === 1 ? 'needs' : 'need'} attention`
    : 'No accounts connected';
  $('#connect-picker').hidden = !addPickerOpen && connections.length > 0;
  $('#add-connection').setAttribute('aria-expanded', String(!$('#connect-picker').hidden));
  $('#connection-empty').hidden = connections.length > 0;
  $('.table-wrap', $('[data-page=connections]')).hidden = connections.length === 0;
  const priority = { ready: 0, expired: 1, revoked: 2, connected: 3 };
  const query = ($('#connection-search')?.value || '').trim().toLowerCase();
  const statusFilter = $('#connection-status-filter')?.value || 'all';
  const filtered = connections.filter((connection) => {
    if (statusFilter === 'connected' && connection.status !== 'connected') return false;
    if (statusFilter === 'attention' && !['ready', 'expired', 'revoked'].includes(connection.status)) return false;
    if (!query) return true;
    return [connection.display_name, connection.connection_id, connection.provider,
      connection.account_title, connection.verified_account_id].some((value) => String(value || '').toLowerCase().includes(query));
  });
  $('#connection-filter-empty').hidden = filtered.length > 0 || connections.length === 0;
  const withinGroup = (a, b) =>
    (priority[a.status] ?? 4) - (priority[b.status] ?? 4) ||
    String(a.display_name || a.connection_id).localeCompare(String(b.display_name || b.connection_id));
  const groups = new Map();
  for (const connection of filtered) {
    groups.set(connection.provider, [...(groups.get(connection.provider) || []), connection]);
  }
  /* Accounts of one provider stay together; a header row appears only where
     it carries information (a provider with several accounts). */
  $('#connection-table').innerHTML = [...groups.entries()].sort(([a], [b]) =>
    providerLabel(a).localeCompare(providerLabel(b))).map(([provider, group]) => {
    const rows = [...group].sort(withinGroup).map(connectionRow).join('');
    if (group.length < 2) return rows;
    const attention = group.filter((connection) => ['ready', 'expired', 'revoked'].includes(connection.status)).length;
    return `<tr class="provider-group-row"><th colspan="4" scope="colgroup">${providerMark(provider)}<span class="provider-group-name">${escapeHtml(providerLabel(provider))}</span><span class="provider-group-meta">${group.length} account${group.length === 1 ? '' : 's'}${attention ? ` · ${attention} ${attention === 1 ? 'needs' : 'need'} attention` : ''}</span></th></tr>${rows}`;
  }).join('');
  $$('.connection-edit').forEach((button) => button.addEventListener('click', () => openEditConnection(button.dataset.connection)));
  bindOAuthLinks();
}

function connectionRow(connection) {
    const nextAction = !TOKEN_PROVIDERS.includes(connection.provider) && connection.status !== 'connected'
      ? `<a class="button ${connection.status === 'ready' ? 'primary' : 'secondary'} connection-oauth" href="/api/admin/oauth/${encodeURIComponent(connection.connection_id)}/start" data-connection="${escapeHtml(connection.connection_id)}" target="_blank" rel="noopener">${connection.status === 'ready' ? 'Finish setup' : 'Reconnect'}</a>` : '';
    const identity = connection.account_title || connection.verified_account_id;
    return `<tr>
    <td class="cell-title"><span class="cell-name">${escapeHtml(connection.display_name || connection.connection_id)}</span><span class="cell-sub">${identity ? escapeHtml(identity) : 'No account verified yet'}</span></td>
    <td data-label="Provider"><span class="provider-cell">${providerMark(connection.provider)}<span class="mono muted-cell">${escapeHtml(connection.provider)}</span></span></td>
    <td data-label="Status">${statusLine(connection.status, CONNECTION_STATUS_LABELS)}</td>
    <td class="action-cell">${nextAction}<button class="button secondary connection-edit" data-connection="${escapeHtml(connection.connection_id)}" type="button">Manage</button></td>
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

function confirmRevoke(title, message) {
  const dialog = $('#connection-confirm-dialog');
  $('#connection-confirm-title').textContent = title;
  $('#connection-confirm-message').textContent = message;
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
    await refresh();
  } catch (error) { $('#edit-connection-error').textContent = error.message; }
  finally { button.disabled = false; button.textContent = $('#edit-connection-form').dataset.provider === 'zoom' ? 'Disable webhook' : 'Revoke tokens'; }
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
    await refresh();
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
  finally { submit.disabled = false; submit.textContent = 'Save changes'; }
});
