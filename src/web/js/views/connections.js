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
    blurb: 'Paste a bot (xoxb-…) or user (xoxp-…) token from your Slack app settings. It is verified against Slack and stored as the connection\'s secret.',
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
  ready: 'setup incomplete',
  expired: 'needs reconnection',
  revoked: 'revoked',
};

let addPickerOpen = false;

function renderConnectCards(connections) {
  $('#connect-grid').innerHTML = Object.entries(CONNECT_PROVIDERS).map(([provider, meta]) => {
    const pending = !TOKEN_PROVIDERS.includes(provider)
      ? connections.find((connection) => connection.provider === provider && connection.status === 'ready') : null;
    const action = pending
      ? `<a class="button primary connection-oauth" href="/api/admin/oauth/${encodeURIComponent(pending.connection_id)}/start" target="_blank" rel="noopener">Finish setup</a>
         <button class="button secondary connect-button" data-provider="${provider}" type="button">Add another account</button>`
      : `<button class="button secondary connect-button" data-provider="${provider}" type="button">Add account</button>`;
    return `
    <div class="connect-card">
      <div class="connect-card-head"><span class="connect-title">${providerMark(provider)}<span class="connect-name">${meta.label}</span></span></div>
      <p class="connect-blurb">${meta.blurb}</p>
      ${pending ? `<p class="connect-pending">${escapeHtml(pending.display_name || pending.connection_id)} is waiting for setup.</p>` : ''}
      <div class="connect-card-actions">${action}</div>
    </div>`;
  }).join('');
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
  // Open during the click: browsers block windows opened after the PUT awaits.
  const popup = window.open('', '_blank', 'width=680,height=760');
  if (!popup) return notice('Allow pop-ups to add this connection, then try again.', true);
  popup.document.title = `Connect ${meta.label}`;
  if (popup.document.body) popup.document.body.textContent = 'Preparing connection…';
  const stopWatching = watchOAuthPopup(popup);
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

function watchOAuthPopup(popup) {
  const timer = setInterval(() => {
    if (popup.closed) {
      clearInterval(timer);
      notice('Consent window closed. Checking the connection status…');
      refresh();
      return;
    }
    try {
      // The callback redirects to /connections?oauth=... after consent.
      const result = new URLSearchParams(popup.location.search).get('oauth');
      if (popup.location.pathname === '/connections' && result) {
        clearInterval(timer);
        if (result === 'connected') popup.close();
        else notice(`Connection failed: ${result}`, true);
        refresh();
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
  const reconnect = $('#edit-connection-reconnect');
  reconnect.hidden = TOKEN_PROVIDERS.includes(connection.provider) || connection.status === 'ready';
  reconnect.href = `/api/admin/oauth/${encodeURIComponent(connectionId)}/start`;
  $('#edit-connection-revoke').hidden = !['connected', 'expired'].includes(connection.status);
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
  const ordered = [...connections].sort((a, b) =>
    (priority[a.status] ?? 4) - (priority[b.status] ?? 4));
  $('#connection-table').innerHTML = ordered.map((connection) => {
    const nextAction = !TOKEN_PROVIDERS.includes(connection.provider) && connection.status !== 'connected'
      ? `<a class="button ${connection.status === 'ready' ? 'primary' : 'secondary'} connection-oauth" href="/api/admin/oauth/${encodeURIComponent(connection.connection_id)}/start" target="_blank" rel="noopener">${connection.status === 'ready' ? 'Finish setup' : 'Reconnect'}</a>` : '';
    const identity = connection.account_title || connection.verified_account_id;
    return `<tr>
    <td class="cell-title"><span class="cell-name">${escapeHtml(connection.display_name || connection.connection_id)}</span><span class="cell-sub">${identity ? escapeHtml(identity) : 'No account verified yet'}</span></td>
    <td data-label="Provider"><span class="provider-cell">${providerMark(connection.provider)}<span class="mono muted-cell">${escapeHtml(connection.provider)}</span></span></td>
    <td data-label="Status">${statusLine(connection.status, CONNECTION_STATUS_LABELS)}</td>
    <td class="action-cell">${nextAction}<button class="button secondary connection-edit" data-connection="${escapeHtml(connection.connection_id)}" type="button">Manage</button></td>
  </tr>`;
  }).join('');
  bindOAuthLinks();
  $$('.connection-edit').forEach((button) => button.addEventListener('click', () => openEditConnection(button.dataset.connection)));
}

function bindOAuthLinks() {
  $$('.connection-oauth').forEach((link) => link.addEventListener('click', (event) => {
    const popup = window.open(link.href, '_blank', 'width=680,height=760');
    if (!popup) {
      event.preventDefault();
      notice('Allow pop-ups to connect this account, then try again.', true);
      return;
    }
    event.preventDefault();
    popup.focus();
    watchOAuthPopup(popup);
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
});

$('#edit-connection-reconnect').addEventListener('click', (event) => {
  $('#edit-connection-dialog').close();
  const popup = window.open(event.currentTarget.href, '_blank', 'width=680,height=760');
  if (!popup) {
    event.preventDefault();
    notice('Allow pop-ups to reconnect this account, then try again.', true);
    return;
  }
  event.preventDefault();
  popup.focus();
  watchOAuthPopup(popup);
});

$('#edit-connection-revoke').addEventListener('click', async () => {
  const connectionId = $('#edit-connection-form').dataset.connectionId;
  if (!await confirmRevoke('Revoke connection access',
    `Revoke access for ${connectionId} in Dapier? You will need to connect it again.`)) return;
  try {
    await api(`/api/admin/connections/${encodeURIComponent(connectionId)}/tokens`, { method: 'DELETE' });
    $('#edit-connection-dialog').close();
    notice(`Access for ${connectionId} revoked in Dapier. Reconnect it to use the connection again.`);
    await refresh();
  } catch (error) { $('#edit-connection-error').textContent = error.message; }
});

let shownGrants = [];

function resetGrantForm() {
  const form = $('#connection-grant-form');
  form.reset();
  form.subject.readOnly = false;
  form.agent.readOnly = false;
  form.dataset.grantee = '';
  $('#connection-grant-heading').textContent = 'Add grant';
  $('#connection-grant-cancel').hidden = true;
  $('#connection-grant-error').textContent = '';
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
    $$('input[name="operations"]', form).forEach((input) => { input.checked = (grant.operations || []).includes(input.value); });
    form.expires_at.value = localDateTime(grant.expires_at);
    $('#connection-grant-heading').textContent = 'Edit grant';
    $('#connection-grant-cancel').hidden = false;
    $('#connection-grant-error').textContent = '';
    form.scrollIntoView({ block: 'nearest' });
  }));
  $$('.grant-delete').forEach((button) => button.addEventListener('click', async () => {
    const connectionId = $('#connection-grant-form').dataset.connectionId;
    if (!await confirmRevoke('Revoke access grant',
      `Revoke ${button.dataset.grantee} on ${connectionId}?`)) return;
    try {
      await api(`/api/admin/grants?connection_id=${encodeURIComponent(connectionId)}&grantee=${encodeURIComponent(button.dataset.grantee)}`, { method: 'DELETE' });
      resetGrantForm();
      notice('Grant revoked');
      await loadGrants(connectionId);
    } catch (error) { $('#connection-grant-error').textContent = error.message; }
  }));
}

async function loadGrants(connectionId) {
  const data = await api(`/api/admin/grants?connection_id=${encodeURIComponent(connectionId)}`);
  shownGrants = data.grants || [];
  renderGrants();
}

$('#edit-connection-access').addEventListener('click', async () => {
  const connectionId = $('#edit-connection-form').dataset.connectionId;
  const connection = ((state.data || {}).connections || []).find((item) => item.connection_id === connectionId);
  resetGrantForm();
  $('#connection-grant-form').dataset.connectionId = connectionId;
  $('#connection-access-title').textContent = `Access · ${connection?.display_name || connectionId}`;
  try {
    await loadGrants(connectionId);
    $('#edit-connection-dialog').close();
    $('#connection-access-dialog').showModal();
  } catch (error) { $('#edit-connection-error').textContent = error.message; }
});

$('#connection-grant-cancel').addEventListener('click', resetGrantForm);

$('#connection-grant-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const operations = $$('input[name="operations"]:checked', form).map((input) => input.value);
  $('#connection-grant-error').textContent = '';
  if (!operations.length) {
    $('#connection-grant-error').textContent = 'Choose at least one operation.';
    return;
  }
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
    notice('Grant saved');
  } catch (error) { $('#connection-grant-error').textContent = error.message; }
});

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
