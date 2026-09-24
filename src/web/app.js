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

function statusLine(status) {
  const value = String(status || 'unknown');
  const kind = ['completed', 'connected', 'configured', 'enabled'].includes(value) ? 'ok'
    : ['processing', 'ready'].includes(value) ? 'run'
    : ['failed', 'error', 'missing'].includes(value) ? 'err'
    : 'off';
  return `<span class="status ${kind}"><span class="status-dot" aria-hidden="true"></span>${escapeHtml(value)}</span>`;
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
  $('#connection-empty').hidden = connections.length > 0;
  $('.table-wrap', $('[data-page=connections]')).hidden = connections.length === 0;
  $('#connection-table').innerHTML = connections.map((connection) => {
    const action = connection.provider === 'slack'
      ? '<span class="action-type">token</span>'
      : `<a class="button secondary" href="/api/admin/oauth/${encodeURIComponent(connection.connection_id)}/start">${connection.status === 'connected' ? 'Reconnect' : 'Connect'}</a>`;
    return `<tr>
    <td class="cell-title"><span class="cell-name">${escapeHtml(connection.display_name)}</span><span class="cell-sub">${wrapTokens(connection.connection_id)}</span></td>
    <td class="mono muted-cell" data-label="Provider">${escapeHtml(connection.provider)}</td>
    <td class="mono muted-cell" data-label="Scopes">${wrapTokens((connection.scopes || []).join(', ') || 'Default')}</td>
    <td data-label="Status">${statusLine(connection.status)}</td>
    <td class="action-cell"${connection.provider === 'slack' ? ' data-label="Auth"' : ''}>${action}</td>
  </tr>`;
  }).join('');
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

function setView(view) {
  state.view = view;
  $$('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.view === view));
  $$('.view').forEach((page) => page.classList.toggle('active', page.dataset.page === view));
  $('#view-title').textContent = view[0].toUpperCase() + view.slice(1);
  $('.sidebar').classList.remove('open');
}

function notice(message, error = false) {
  const element = $('#notice');
  element.textContent = message;
  element.classList.toggle('error', error);
  element.hidden = false;
  setTimeout(() => { element.hidden = true; }, 4500);
}

const PROVIDER_SPECS = {
  dropbox: {
    connectionId: 'team-dropbox',
    displayName: 'Team Dropbox',
    scopes: 'files.metadata.read files.content.read',
    console: 'https://www.dropbox.com/developers/apps',
    consoleLabel: 'Dropbox App Console',
    scopeHint: 'Space-separated Dropbox scopes. Leave as-is unless your app needs more.',
  },
  google: {
    connectionId: 'calendar-alexey',
    displayName: 'Calendar — Alexey',
    scopes: 'https://www.googleapis.com/auth/calendar.freebusy https://www.googleapis.com/auth/calendar.events.owned https://www.googleapis.com/auth/userinfo.email',
    console: 'https://console.cloud.google.com/apis/credentials',
    consoleLabel: 'Google Cloud Console',
    scopeHint: 'Google requires at least one scope — the prefilled three cover free/busy, owned-event edits, and account verification.',
  },
  youtube: {
    connectionId: 'channel-youtube',
    displayName: 'Channel YouTube',
    scopes: 'https://www.googleapis.com/auth/youtube.readonly',
    console: 'https://console.cloud.google.com/apis/credentials',
    consoleLabel: 'Google Cloud Console',
    scopeHint: 'Google requires at least one scope — leave this filled in or YouTube sign-in fails.',
  },
  slack: {
    connectionId: 'slack',
    displayName: 'DataTalks Slack',
    scopes: '',
    console: 'https://api.slack.com/apps',
    consoleLabel: 'Slack App Directory',
    scopeHint: 'Slack tokens carry their scopes from the app installation.',
  },
};

function applyProviderSpec(provider) {
  const spec = PROVIDER_SPECS[provider] || PROVIDER_SPECS.dropbox;
  const form = $('#connection-form');
  const tokenProvider = provider === 'slack';
  $('#connection-oauth-callout').hidden = tokenProvider;
  $('#connection-client-id-field').hidden = tokenProvider;
  $('#connection-client-secret-field').hidden = tokenProvider;
  $('#connection-scopes-field').hidden = tokenProvider;
  $('#connection-token-field').hidden = !tokenProvider;
  form.client_id.required = !tokenProvider;
  form.client_secret.required = !tokenProvider;
  form.slack_token.required = tokenProvider;
  form.connection_id.placeholder = spec.connectionId;
  form.display_name.placeholder = spec.displayName;
  form.scopes.placeholder = spec.scopes;
  if (tokenProvider) {
    form.scopes.value = '';
    delete form.scopes.dataset.autofill;
  } else if (!form.scopes.value.trim() || form.scopes.dataset.autofill === '1') {
    form.scopes.value = spec.scopes;
    form.scopes.dataset.autofill = '1';
  }
  $('#connection-scope-hint').textContent = spec.scopeHint;
  const link = $('#connection-console');
  link.href = spec.console;
  link.textContent = spec.consoleLabel;
  $('#connection-redirect').textContent = `${window.location.origin}/oauth/callback`;
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

$('#connection-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  values.scopes = values.scopes.trim() ? values.scopes.trim().split(/\s+/) : [];
  if (values.provider === 'slack') {
    values.token = values.slack_token;
    delete values.slack_token;
  }
  $('#connection-error').textContent = '';
  try {
    await api('/api/admin/connections', { method: 'PUT', body: JSON.stringify(values) });
    form.client_secret.value = '';
    form.slack_token.value = '';
    $('#connection-dialog').close();
    notice('Connection saved');
    await refresh();
  } catch (error) { $('#connection-error').textContent = error.message; }
});

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
$$('.nav-item').forEach((button) => button.addEventListener('click', () => setView(button.dataset.view)));
$$('.view-link').forEach((button) => button.addEventListener('click', () => setView(button.dataset.target)));
$('#add-connection').addEventListener('click', () => {
  const form = $('#connection-form');
  form.reset();
  form.scopes.dataset.autofill = '1';
  $('#connection-error').textContent = '';
  applyProviderSpec(form.provider.value);
  $('#connection-dialog').showModal();
  icons();
});
$('#connection-form').provider.addEventListener('change', (event) => applyProviderSpec(event.target.value));
$('#connection-form').scopes.addEventListener('input', (event) => { event.target.dataset.autofill = ''; });
$('#connection-copy').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($('#connection-redirect').textContent);
    notice('Redirect URI copied');
  } catch (_) { notice('Copy failed — select the URI manually', true); }
});
$('#refresh').addEventListener('click', refresh);
$('#menu-toggle').addEventListener('click', () => $('.sidebar').classList.toggle('open'));
$('#logout').addEventListener('click', () => { window.location.assign('/auth/logout'); });

window.addEventListener('DOMContentLoaded', async () => {
  icons();
  let me;
  try { me = await api('/api/admin/me'); } catch (_) { return; }
  if (!me.operator) { showForbidden(); return; }
  await refresh();
});
