const state = { data: null, view: 'overview' };
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'content-type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (response.status === 401) {
    showLogin();
    throw new Error(body.error || 'Authentication required');
  }
  if (response.status === 403) {
    showForbidden(body.error || 'Your account is not an operator for this console.');
    throw new Error(body.error || 'Not authorized');
  }
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function icons() {
  if (window.lucide) window.lucide.createIcons({ attrs: { 'stroke-width': 1.8 } });
}

function showLogin() {
  window.location.assign('/auth/login');
}

function showForbidden(message = 'Your account is not an operator for this console.') {
  $('#forbidden-view').hidden = false;
  $('#forbidden-message').textContent = message;
}

function showApp() {
  $('#app').hidden = false;
  icons();
}

const pad2 = (value) => String(value).padStart(2, '0');

function statusLine(status, labels) {
  const value = String(status || 'unknown');
  const kind = ['completed', 'connected', 'configured', 'enabled'].includes(value) ? 'ok'
    : ['processing', 'ready'].includes(value) ? 'run'
    : ['failed', 'error', 'missing', 'expired'].includes(value) ? 'err'
    : 'off';
  const text = (labels || {})[value] || value;
  return `<span class="status ${kind}"><span class="status-dot" aria-hidden="true"></span>${escapeHtml(text)}</span>`;
}

/* Escape, then mark separator characters as safe wrap points so long machine
   values (execution IDs, scope URLs) break at ":", ".", "/", "_" — never mid-token. */
function wrapTokens(value) {
  return escapeHtml(value).replace(/([:._/])/g, '$1<wbr>');
}

function escapeHtml(value) {
  const element = document.createElement('span');
  element.textContent = value == null ? '' : String(value);
  return element.innerHTML;
}

function triggerLabel(workflow) {
  const trigger = workflow.trigger;
  return `${trigger.connector} · ${trigger.event}`;
}

/* One timestamp family everywhere: YYYY-MM-DD HH:MM (24h, local). */
function toDate(value) {
  return /^\d+$/.test(String(value)) ? new Date(Number(value) * 1000) : new Date(value);
}

function formatTimestamp(value) {
  if (value == null || value === '') return null;
  const date = toDate(value);
  return Number.isNaN(date.getTime()) ? String(value)
    : `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

function formatDay(value) {
  if (value == null || value === '') return null;
  const date = toDate(value);
  return Number.isNaN(date.getTime()) ? String(value)
    : `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

function workflowRow(workflow) {
  return `<tr class="workflow-open" data-workflow="${escapeHtml(workflow.id)}" role="button" tabindex="0">
    <td class="cell-title mono"><span class="cell-name">${escapeHtml(workflow.id)}</span></td>
    <td class="mono muted-cell" data-label="Trigger">${escapeHtml(triggerLabel(workflow))}</td>
    <td data-label="Status">${statusLine(workflow.enabled ? 'enabled' : 'disabled')}</td>
  </tr>`;
}

function configRows(config) {
  const entries = Object.entries(config).filter(([key]) => key !== 'id' && key !== 'type');
  if (!entries.length) return '<p class="detail-muted">No parameters.</p>';
  return `<dl class="detail-list">${entries.map(([key, value]) => {
    const text = typeof value === 'object' && value !== null ? JSON.stringify(value, null, 2) : String(value);
    return `<div><dt>${escapeHtml(key)}</dt><dd><pre>${escapeHtml(text)}</pre></dd></div>`;
  }).join('')}</dl>`;
}

function openWorkflow(id) {
  const workflow = (state.data.workflows || []).find((item) => item.id === id);
  if (!workflow) return;
  const filters = workflow.trigger.filters || {};
  $('#workflow-title').textContent = workflow.id;
  $('#workflow-detail').innerHTML = `
    <div class="detail-summary">
      ${statusLine(workflow.enabled ? 'enabled' : 'disabled')}
      ${workflow.source ? `<code>workflows/${escapeHtml(workflow.source)}</code>` : ''}
    </div>
    <section class="detail-block">
      <h3>Trigger</h3>
      <p class="detail-trigger">${escapeHtml(triggerLabel(workflow))}</p>
      ${Object.keys(filters).length ? `<h4>Filters</h4>${configRows(filters)}` : ''}
    </section>
    <section class="detail-block">
      <h3>Actions</h3>
      ${(workflow.actions || []).map((action) => `<div class="detail-action">
        <div class="detail-action-head"><span class="action-id">${escapeHtml(action.id)}</span><span class="action-type">${escapeHtml(action.type)}</span></div>
        ${configRows(action)}
      </div>`).join('')}
    </section>`;
  const edit = $('#workflow-edit');
  if (workflow.source && state.data.workflows_edit_base) {
    edit.href = `${state.data.workflows_edit_base}/${encodeURIComponent(workflow.source)}`;
    edit.hidden = false;
  } else {
    edit.hidden = true;
  }
  $('#workflow-dialog').showModal();
  icons();
}

function detailRows(rows) {
  const entries = rows.filter(([, value]) => value !== undefined && value !== null && value !== '');
  if (!entries.length) return '<p class="detail-muted">No details recorded.</p>';
  return `<dl class="detail-list">${entries.map(([label, value]) =>
    `<div><dt>${escapeHtml(label)}</dt><dd><pre>${escapeHtml(String(value))}</pre></dd></div>`).join('')}</dl>`;
}

function openRun(executionId) {
  const run = (state.data.executions || []).find((item) => item.execution_id === executionId);
  if (!run) return;
  const parts = String(executionId).split(':');
  const workflowId = run.workflow_id || parts[0];
  const actionId = run.action_id || parts[1];
  $('#run-title').textContent = workflowId || 'Run';
  $('#run-detail').innerHTML = `
    <div class="detail-summary">${statusLine(run.status)}<code>${wrapTokens(executionId)}</code></div>
    ${run.error ? `<div class="detail-error"><i data-lucide="alert-triangle"></i><span>${escapeHtml(run.error)}</span></div>` : ''}
    <section class="detail-block">${detailRows([
      ['Workflow', workflowId],
      ['Action', actionId],
      ['Connector', run.connector],
      ['Event', run.event_type],
      ['Correlation ID', run.correlation_id],
      ['Started', formatTimestamp(run.started_at)],
      ['Finished', formatTimestamp(run.finished_at)],
      ['Retention until', formatTimestamp(run.expires_at)],
    ])}</section>`;
  $('#run-dialog').showModal();
  icons();
}

function render() {
  const data = state.data;
  if (!data) return;
  const enabled = data.workflows.filter((workflow) => workflow.enabled);
  const configured = data.credentials.filter((credential) => credential.configured).length;
  $('#metric-workflows').textContent = enabled.length;
  $('#metric-connections').textContent = data.connections.length;
  $('#metric-runs').textContent = data.executions.filter((execution) => execution.status === 'completed').length;
  $('#metric-credentials').textContent = `${configured}/${data.credentials.length}`;
  $('#overview-workflows').innerHTML = enabled.slice(0, 5).map(workflowRow).join('');
  $('#overview-runs').innerHTML = data.executions.slice(0, 6).map((execution) =>
    `<tr class="run-open" data-run="${escapeHtml(execution.execution_id)}" role="button" tabindex="0"><td class="mono">${wrapTokens(execution.execution_id)}</td><td data-label="Status">${statusLine(execution.status)}</td></tr>`).join('') || emptyRow(2);
  $('#workflow-table').innerHTML = data.workflows.map((workflow) => `<tr class="workflow-open" data-workflow="${escapeHtml(workflow.id)}" role="button" tabindex="0">
    <td class="cell-title mono"><span class="cell-name">${escapeHtml(workflow.id)}</span></td>
    <td class="mono muted-cell" data-label="Trigger">${escapeHtml(triggerLabel(workflow))}</td>
    <td class="mono muted-cell" data-label="Actions">${workflow.actions.map((action) => escapeHtml(action.type)).join(', ')}</td>
    <td data-label="Status">${statusLine(workflow.enabled ? 'enabled' : 'disabled')}</td>
  </tr>`).join('');
  renderConnections(data.connections);
  renderCredentials(data.credentials);
  renderOAuthClients(data.oauth_clients || []);
  $('#run-table').innerHTML = data.executions.map((execution) => `<tr class="run-open" data-run="${escapeHtml(execution.execution_id)}" role="button" tabindex="0">
    <td class="cell-title mono"><span class="cell-name">${wrapTokens(execution.execution_id)}</span></td>
    <td data-label="Status">${statusLine(execution.status)}</td>
    <td class="mono muted-cell" data-label="Retention">${execution.expires_at ? wrapTokens(formatDay(execution.expires_at)) : '—'}</td>
  </tr>`).join('') || emptyRow(3);
  const now = new Date();
  $('#last-updated').textContent = `Updated ${pad2(now.getHours())}:${pad2(now.getMinutes())}`;
  icons();
}

function emptyRow(columns) {
  return `<tr><td colspan="${columns}" style="color:var(--muted)">No activity</td></tr>`;
}

function renderConnections(connections) {
  renderConnectCards();
  $('#connection-empty').hidden = connections.length > 0;
  $('.table-wrap', $('[data-page=connections]')).hidden = connections.length === 0;
  $('#connection-table').innerHTML = connections.map((connection) => {
    const reconnect = connection.provider === 'slack' ? ''
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

function renderCredentials(credentials) {
  const labels = {
    slack: { name: 'Slack bot', secret: 'credential: slack' },
    mailchimp: { name: 'Mailchimp', secret: 'credential: mailchimp' },
  };
  $('#credential-list').innerHTML = credentials.map((credential) => {
    const item = labels[credential.provider];
    const status = credential.configured ? 'configured' : 'missing';
    return `<tr>
      <td class="cell-title"><span class="cell-name">${item.name}</span><span class="cell-sub">${item.secret}</span></td>
      <td class="mono muted-cell" data-label="Updated">${credential.updated_at ? formatTimestamp(credential.updated_at) : '—'}</td>
      <td data-label="Status">${statusLine(status)}</td>
      <td class="action-cell"><button class="button secondary credential-edit" data-provider="${credential.provider}">${credential.configured ? 'Replace' : 'Add'}</button></td>
    </tr>`;
  }).join('');
  $$('.credential-edit').forEach((button) => button.addEventListener('click', () => openCredential(button.dataset.provider)));
}

async function refresh() {
  $('#loading').hidden = false;
  try {
    state.data = await api('/api/admin/overview');
    showApp();
    render();
  } catch (error) {
    if (!$('#app').hidden) notice(error.message, true);
  } finally {
    $('#loading').hidden = true;
  }
}

const VIEWS = ['overview', 'workflows', 'connections', 'credentials', 'runs'];

function viewFromPath(path) {
  const name = path.replace(/^\/+|\/+$/g, '');
  return VIEWS.includes(name) ? name : 'overview';
}

function setView(view, push = true) {
  state.view = view;
  $$('.nav-item').forEach((item) => {
    item.classList.toggle('active', item.dataset.view === view);
    if (item.dataset.view === view) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  $$('.view').forEach((page) => page.classList.toggle('active', page.dataset.page === view));
  $('#view-title').textContent = view[0].toUpperCase() + view.slice(1);
  $('.sidebar').classList.remove('open');
  if (push) history.pushState(null, '', view === 'overview' ? '/' : `/${view}`);
  window.scrollTo(0, 0);
}

function notice(message, error = false) {
  const element = $('#notice');
  element.textContent = message;
  element.classList.toggle('error', error);
  element.hidden = false;
  setTimeout(() => { element.hidden = true; }, 4500);
}

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
};

/* Canonical brand glyphs (same simple-icons paths the designer palette uses)
   so cards and rows are identifiable by product mark, not just text. */
const PROVIDER_MARKS = {
  slack: '<svg class="brand-mark" width="16" height="16" viewBox="0 0 24 24" role="img" aria-label="Slack"><path fill="#E01E5A" d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313z"/><path fill="#36C5F0" d="M8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312z"/><path fill="#2EB67D" d="M18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312z"/><path fill="#ECB22E" d="M15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/></svg>',
  google: '<svg class="brand-mark" width="16" height="16" viewBox="0 0 24 24" role="img" aria-label="Google"><path fill="#4285F4" d="M23.49 12.27c0-.79-.07-1.54-.19-2.27H12v4.51h6.47c-.29 1.48-1.14 2.73-2.4 3.58v3h3.86c2.26-2.09 3.56-5.17 3.56-8.82z"/><path fill="#34A853" d="M12 24c3.24 0 5.95-1.08 7.93-2.91l-3.86-3c-1.08.72-2.45 1.16-4.07 1.16-3.13 0-5.78-2.11-6.73-4.96H1.29v3.09C3.26 21.3 7.31 24 12 24z"/><path fill="#FBBC05" d="M5.27 14.29c-.25-.72-.38-1.49-.38-2.29s.14-1.57.38-2.29V6.62H1.29C.47 8.24 0 10.06 0 12s.47 3.76 1.29 5.38l3.98-3.09z"/><path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.42-3.42C17.95 1.19 15.24 0 12 0 7.31 0 3.26 2.7 1.29 6.62l3.98 3.09C6.22 6.86 8.87 4.75 12 4.75z"/></svg>',
  youtube: '<svg class="brand-mark" width="16" height="16" viewBox="0 0 24 24" role="img" aria-label="YouTube"><path fill="#FF0000" d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/></svg>',
  dropbox: '<svg class="brand-mark" width="16" height="16" viewBox="0 0 24 24" role="img" aria-label="Dropbox"><path fill="#0061FF" d="M6 1.807 0 5.629l6 3.822 6.001-3.822L6 1.807zM18 1.807l-6 3.822 6 3.822 6-3.822-6-3.822zM0 13.274l6 3.822 6.001-3.822L6 9.452l-6 3.822zM18 9.452l-6 3.822 6 3.822 6-3.822-6-3.822zM6 18.371l6.001 3.822 6-3.822-6-3.822L6 18.371z"/></svg>',
};

function providerMark(provider) {
  return PROVIDER_MARKS[provider] || '';
}

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
  if (provider === 'slack') return openSlackDialog();
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

function openSlackDialog() {
  const form = $('#connection-form');
  form.reset();
  $('#connection-error').textContent = '';
  $('#connection-dialog').showModal();
  form.slack_token.focus();
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
  $('#edit-scopes-field').hidden = connection.provider === 'slack';
  form.token.value = '';
  $('#edit-token-field').hidden = connection.provider !== 'slack';
  $('#edit-connection-error').textContent = '';
  $('#edit-connection-dialog').showModal();
  form.display_name.focus();
}

function openCredential(provider) {
  const form = $('#credential-form');
  form.reset();
  form.provider.value = provider;
  $('#credential-title').textContent = provider === 'slack' ? 'Slack bot token' : 'Mailchimp API key';
  $('#credential-field-label').firstChild.textContent = provider === 'slack' ? 'Bot token' : 'API key';
  $('#credential-error').textContent = '';
  $('#credential-dialog').showModal();
  form.value.focus();
}

$('#credential-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const provider = form.provider.value;
  const field = provider === 'slack' ? 'token' : 'api_key';
  $('#credential-error').textContent = '';
  try {
    await api(`/api/admin/credentials/${provider}`, { method: 'PUT', body: JSON.stringify({ [field]: form.value.value }) });
    form.value.value = '';
    $('#credential-dialog').close();
    notice('Credential saved');
    await refresh();
  } catch (error) { $('#credential-error').textContent = error.message; }
});

function renderOAuthClients(clients) {
  const names = { google: 'Google (also YouTube)', dropbox: 'Dropbox' };
  $('#oauth-client-list').innerHTML = clients.map((client) => `<tr>
      <td class="cell-title"><span class="cell-name">${names[client.provider] || escapeHtml(client.provider)}</span><span class="cell-sub">oauth client: ${escapeHtml(client.provider)}</span></td>
      <td class="mono muted-cell" data-label="Client ID">${client.client_id ? wrapTokens(client.client_id) : '—'}</td>
      <td class="mono muted-cell" data-label="Source">${escapeHtml(client.source)}</td>
      <td data-label="Status">${statusLine(client.configured ? 'configured' : 'missing')}</td>
      <td class="action-cell"><button class="button secondary oauth-client-edit" data-provider="${escapeHtml(client.provider)}" type="button">${client.configured ? 'Replace' : 'Set up'}</button></td>
    </tr>`).join('');
  $$('.oauth-client-edit').forEach((button) => button.addEventListener('click', () => openOAuthClient(button.dataset.provider)));
}

function openOAuthClient(provider) {
  const form = $('#oauth-client-form');
  form.reset();
  form.dataset.provider = provider;
  $('#oauth-client-title').textContent = provider === 'google' ? 'Google OAuth client' : 'Dropbox OAuth client';
  $('#oauth-client-error').textContent = '';
  $('#oauth-client-dialog').showModal();
  form.client_id.focus();
}

$('#oauth-client-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  $('#oauth-client-error').textContent = '';
  try {
    await api(`/api/admin/oauth-clients/${form.dataset.provider}`, {
      method: 'PUT',
      body: JSON.stringify({ client_id: form.client_id.value, client_secret: form.client_secret.value }),
    });
    form.client_secret.value = '';
    $('#oauth-client-dialog').close();
    notice('OAuth client saved — Connect buttons are live');
    await refresh();
  } catch (error) { $('#oauth-client-error').textContent = error.message; }
});

$('#connection-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  $('#connection-error').textContent = '';
  const { id, suffix } = nextConnectionId('slack');
  const body = {
    connection_id: id,
    provider: 'slack',
    display_name: suffix ? `DataTalks Slack ${suffix}` : 'DataTalks Slack',
    token: form.slack_token.value,
  };
  try {
    await api('/api/admin/connections', { method: 'PUT', body: JSON.stringify(body) });
    form.slack_token.value = '';
    $('#connection-dialog').close();
    notice('Slack connected');
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
  if (provider !== 'slack') {
    body.scopes = form.scopes.value.split(/\s+/).filter(Boolean);
  } else {
    const token = form.token.value.trim();
    if (token) body.token = token;
  }
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

/* Secret fields render masked bullets without being real password inputs where
   the engine can draw them itself (-webkit-text-security): the browser's
   password manager never sees a password field, so it neither autofills saved
   credentials nor offers to save the pasted token. Copy, cut, and drag out of
   a secret field are blocked so a pasted token can't leave it. */
function hardenSecretInputs(root = document) {
  $$('.secret-input', root).forEach((input) => {
    if (input.type === 'password' && CSS.supports('-webkit-text-security', 'disc')) {
      input.type = 'text';
      input.classList.add('masked-input');
      input.setAttribute('autocomplete', 'off');
    } else {
      input.setAttribute('autocomplete', 'new-password');
    }
  });
}

['copy', 'cut', 'dragstart'].forEach((type) => document.addEventListener(type, (event) => {
  if (event.target instanceof Element && event.target.closest('.secret-input')) event.preventDefault();
}));

function handleRowActivate(event) {
  const workflowRow = event.target.closest('.workflow-open');
  if (workflowRow) return openWorkflow(workflowRow.dataset.workflow);
  const runRow = event.target.closest('.run-open');
  if (runRow) return openRun(runRow.dataset.run);
}
document.addEventListener('click', handleRowActivate);
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  if (!event.target.closest('.workflow-open, .run-open')) return;
  event.preventDefault();
  handleRowActivate(event);
});
$$('.dialog-close').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
$$('.nav-item, .view-link').forEach((link) => link.addEventListener('click', (event) => {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
  event.preventDefault();
  setView(link.dataset.view || link.dataset.target);
}));
window.addEventListener('popstate', () => setView(viewFromPath(window.location.pathname), false));
$('#refresh').addEventListener('click', refresh);
$('#menu-toggle').addEventListener('click', () => $('.sidebar').classList.toggle('open'));
$('#logout').addEventListener('click', () => { window.location.assign('/auth/logout'); });

window.addEventListener('DOMContentLoaded', async () => {
  icons();
  hardenSecretInputs();
  let me;
  try { me = await api('/api/admin/me'); } catch (_) { return; }
  if (!me.operator) { showForbidden(); return; }
  setView(viewFromPath(window.location.pathname), false);
  await refresh();
});
