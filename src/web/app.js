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
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function icons() {
  if (window.lucide) window.lucide.createIcons({ attrs: { 'stroke-width': 1.8 } });
}

function showLogin() {
  window.location.assign('/auth/login');
}

function showApp() {
  $('#login-view').hidden = true;
  $('#app').hidden = false;
  icons();
}

function badge(status) {
  const value = String(status || 'unknown');
  const kind = value === 'completed' || value === 'connected' || value === 'configured' || value === 'enabled'
    ? 'success' : value === 'ready' || value === 'processing' ? 'warning' : value === 'missing' ? 'error' : 'neutral';
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
  return `<div class="workflow-row">
    <div class="workflow-name">${connectorIcon(workflow.trigger.connector)}<span>${escapeHtml(workflow.id)}</span></div>
    <div class="workflow-trigger">${escapeHtml(triggerLabel(workflow))}</div>
    ${badge(workflow.enabled ? 'enabled' : 'disabled')}
  </div>`;
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
    `<tr><td>${escapeHtml(execution.execution_id)}</td><td>${badge(execution.status)}</td></tr>`).join('') || emptyRow(2);
  $('#workflow-table').innerHTML = data.workflows.map((workflow) => `<tr>
    <td><div class="workflow-name">${connectorIcon(workflow.trigger.connector)}<span>${escapeHtml(workflow.id)}</span></div></td>
    <td>${escapeHtml(triggerLabel(workflow))}</td>
    <td>${workflow.actions.map((action) => escapeHtml(action.type)).join(', ')}</td>
    <td>${badge(workflow.enabled ? 'enabled' : 'disabled')}</td>
  </tr>`).join('');
  renderConnections(data.connections);
  renderCredentials(data.credentials);
  $('#run-table').innerHTML = data.executions.map((execution) => `<tr>
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
      <button class="button secondary credential-edit" data-provider="${credential.provider}"><i data-lucide="${credential.configured ? 'rotate-cw' : 'plus'}"></i><span>${credential.configured ? 'Replace' : 'Add'}</span></button>
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
  element.style.borderColor = error ? 'var(--coral)' : 'var(--green)';
  element.style.background = error ? 'var(--coral-soft)' : 'var(--green-soft)';
  element.hidden = false;
  setTimeout(() => { element.hidden = true; }, 4500);
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

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const element = event.currentTarget;
  const form = new FormData(element);
  $('#login-error').textContent = '';
  try {
    await api('/api/admin/session', { method: 'POST', body: JSON.stringify(Object.fromEntries(form)) });
    element.querySelector('[name="password"]').value = '';
    await refresh();
  } catch (error) { $('#login-error').textContent = error.message; }
});

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
    form.client_secret.value = '';
    $('#connection-dialog').close();
    notice('Connection saved');
    await refresh();
  } catch (error) { $('#connection-error').textContent = error.message; }
});

$$('.dialog-close').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
$$('.nav-item').forEach((button) => button.addEventListener('click', () => setView(button.dataset.view)));
$$('.view-link').forEach((button) => button.addEventListener('click', () => setView(button.dataset.target)));
$('#add-connection').addEventListener('click', () => { $('#connection-form').reset(); $('#connection-error').textContent = ''; $('#connection-dialog').showModal(); icons(); });
$('#refresh').addEventListener('click', refresh);
$('#menu-toggle').addEventListener('click', () => $('.sidebar').classList.toggle('open'));
$('#logout').addEventListener('click', () => { window.location.assign('/auth/logout'); });

window.addEventListener('DOMContentLoaded', async () => {
  icons();
  try { await api('/api/admin/me'); await refresh(); } catch (_) { showLogin(); }
});
