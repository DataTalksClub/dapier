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

function badge(status) {
  const value = String(status || 'unknown');
  const kind = value === 'completed' || value === 'connected' || value === 'configured' || value === 'enabled'
    ? 'success' : value === 'ready' || value === 'processing' ? 'warning'
    : value === 'failed' || value === 'error' || value === 'missing' ? 'error' : 'neutral';
  return `<span class="badge ${kind}">${escapeHtml(value)}</span>`;
}

function escapeHtml(value) {
  const element = document.createElement('span');
  element.textContent = value == null ? '' : String(value);
  return element.innerHTML;
}

function connectorIcon(connector) {
  const names = { email: 'mail', youtube: 'youtube', renderer: 'file-output', custom: 'webhook', dropbox: 'package-open' };
  return `<span class="connector-icon"><i data-lucide="${names[connector] || 'workflow'}"></i></span>`;
}

function triggerLabel(workflow) {
  const trigger = workflow.trigger;
  return `${trigger.connector} · ${trigger.event}`;
}

function workflowRow(workflow) {
  return `<div class="workflow-row workflow-open" data-workflow="${escapeHtml(workflow.id)}" role="button" tabindex="0">
    <div class="workflow-name">${connectorIcon(workflow.trigger.connector)}<span>${escapeHtml(workflow.id)}</span></div>
    <div class="workflow-trigger">${escapeHtml(triggerLabel(workflow))}</div>
    ${badge(workflow.enabled ? 'enabled' : 'disabled')}
  </div>`;
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
      ${badge(workflow.enabled ? 'enabled' : 'disabled')}
      ${workflow.source ? `<code>workflows/${escapeHtml(workflow.source)}</code>` : ''}
    </div>
    <section class="detail-block">
      <h3>Trigger</h3>
      <p class="detail-trigger">${connectorIcon(workflow.trigger.connector)}<span>${escapeHtml(triggerLabel(workflow))}</span></p>
      ${Object.keys(filters).length ? `<h4>Filters</h4>${configRows(filters)}` : ''}
    </section>
    <section class="detail-block">
      <h3>Actions</h3>
      ${(workflow.actions || []).map((action) => `<div class="detail-action">
        <div class="detail-action-head">${connectorIcon(action.type)}<strong>${escapeHtml(action.id)}</strong><span class="badge neutral">${escapeHtml(action.type)}</span></div>
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

function formatTimestamp(value) {
  if (value == null || value === '') return null;
  const date = /^\d+$/.test(String(value)) ? new Date(Number(value) * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function openRun(executionId) {
  const run = (state.data.executions || []).find((item) => item.execution_id === executionId);
  if (!run) return;
  const parts = String(executionId).split(':');
  const workflowId = run.workflow_id || parts[0];
  const actionId = run.action_id || parts[1];
  $('#run-title').textContent = workflowId || 'Run';
  $('#run-detail').innerHTML = `
    <div class="detail-summary">${badge(run.status)}<code>${escapeHtml(executionId)}</code></div>
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
    `<tr class="run-open" data-run="${escapeHtml(execution.execution_id)}" role="button" tabindex="0"><td>${escapeHtml(execution.execution_id)}</td><td>${badge(execution.status)}</td></tr>`).join('') || emptyRow(2);
  $('#workflow-table').innerHTML = data.workflows.map((workflow) => `<tr class="workflow-open" data-workflow="${escapeHtml(workflow.id)}" role="button" tabindex="0">
    <td><div class="workflow-name">${connectorIcon(workflow.trigger.connector)}<span>${escapeHtml(workflow.id)}</span></div></td>
    <td>${escapeHtml(triggerLabel(workflow))}</td>
    <td>${workflow.actions.map((action) => escapeHtml(action.type)).join(', ')}</td>
    <td>${badge(workflow.enabled ? 'enabled' : 'disabled')}</td>
  </tr>`).join('');
  renderConnections(data.connections);
  renderCredentials(data.credentials);
  $('#run-table').innerHTML = data.executions.map((execution) => `<tr class="run-open" data-run="${escapeHtml(execution.execution_id)}" role="button" tabindex="0">
    <td>${escapeHtml(execution.execution_id)}</td><td>${badge(execution.status)}</td>
    <td>${execution.expires_at ? new Date(Number(execution.expires_at) * 1000).toLocaleDateString() : '—'}</td>
  </tr>`).join('') || emptyRow(3);
  $('#last-updated').textContent = `Updated ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
  icons();
}

function emptyRow(columns) {
  return `<tr><td colspan="${columns}" style="color:var(--muted)">No activity</td></tr>`;
}

function renderConnections(connections) {
  $('#connection-empty').hidden = connections.length > 0;
  $('.table-wrap', $('[data-page=connections]')).hidden = connections.length === 0;
  $('#connection-table').innerHTML = connections.map((connection) => `<tr>
    <td><strong>${escapeHtml(connection.display_name)}</strong><br><code>${escapeHtml(connection.connection_id)}</code></td>
    <td>${escapeHtml(connection.provider)}</td>
    <td>${escapeHtml((connection.scopes || []).join(', ') || 'Default')}</td>
    <td>${badge(connection.status)}</td>
    <td class="action-cell"><a class="button secondary" href="/api/admin/oauth/${encodeURIComponent(connection.connection_id)}/start">${connection.status === 'connected' ? 'Reconnect' : 'Connect'}</a></td>
  </tr>`).join('');
}

function renderCredentials(credentials) {
  const labels = {
    slack: { name: 'Slack bot', secret: 'credential: slack', icon: 'message-square' },
    mailchimp: { name: 'Mailchimp', secret: 'credential: mailchimp', icon: 'send' },
  };
  $('#credential-list').innerHTML = credentials.map((credential) => {
    const item = labels[credential.provider];
    const status = credential.configured ? 'configured' : 'missing';
    return `<div class="credential-row">
      <div class="credential-provider"><span class="connector-icon"><i data-lucide="${item.icon}"></i></span><div><strong>${item.name}</strong><span>${item.secret}</span></div></div>
      <div>${badge(status)}${credential.updated_at ? `<br><small>${new Date(credential.updated_at).toLocaleString()}</small>` : ''}</div>
      <button class="button secondary credential-edit" data-provider="${credential.provider}"><span>${credential.configured ? 'Replace' : 'Add'}</span></button>
    </div>`;
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
    scopeHint: 'Space-separated Dropbox scopes. Leave as-is unless your app needs more.',
  },
  google: {
    connectionId: 'calendar-alexey',
    displayName: 'Calendar — Alexey',
    scopes: 'https://www.googleapis.com/auth/calendar.freebusy https://www.googleapis.com/auth/calendar.events.owned https://www.googleapis.com/auth/userinfo.email',
    scopeHint: 'Google requires at least one scope — the prefilled three cover free/busy, owned-event edits, and account verification.',
  },
  youtube: {
    connectionId: 'channel-youtube',
    displayName: 'Channel YouTube',
    scopes: 'https://www.googleapis.com/auth/youtube.readonly',
    scopeHint: 'Google requires at least one scope — leave this filled in or YouTube sign-in fails.',
  },
};

function applyProviderSpec(provider) {
  const spec = PROVIDER_SPECS[provider] || PROVIDER_SPECS.dropbox;
  const form = $('#connection-form');
  form.connection_id.placeholder = spec.connectionId;
  form.display_name.placeholder = spec.displayName;
  form.scopes.placeholder = spec.scopes;
  if (!form.scopes.value.trim() || form.scopes.dataset.autofill === '1') {
    form.scopes.value = spec.scopes;
    form.scopes.dataset.autofill = '1';
  }
  $('#connection-scope-hint').textContent = spec.scopeHint;
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
  $('#connection-error').textContent = '';
  try {
    await api('/api/admin/connections', { method: 'PUT', body: JSON.stringify(values) });
    // Saving immediately continues into the provider consent flow; the
    // callback returns to the console with the connection marked connected.
    window.location.assign(`/api/admin/oauth/${encodeURIComponent(values.connection_id.trim().toLowerCase())}/start`);
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
