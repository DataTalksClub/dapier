import { renderAgentTasks } from './views/agents.js';
import { renderWorkers } from './views/workers.js';
/* Console bootstrap: global event wiring and the first fetch. */
import { $, $$, icons, notice, hardenSecretInputs, showForbidden, showStartupError } from './ui.js';
import { api } from './api.js';
import { setView, viewFromPath } from './router.js';
import { refresh, openRowFor, openWorkflow, openVersions, restoreVersion } from './views/overview.js';
import { renderRuns, openRun } from './views/runs.js';
import { openDesigner, designerFromLocation, confirmDesignerLeave } from './views/designer.js';
import { showOAuthResult, openEditConnection } from './views/connections.js';
import './views/storage.js';
import { refreshAudit } from './views/audit.js';
import { toggleTheme } from './theme.js';

['copy', 'cut', 'dragstart'].forEach((type) => document.addEventListener(type, (event) => {
  if (event.target instanceof Element && event.target.closest('.secret-input')) event.preventDefault();
}));

document.addEventListener('click', openRowFor);

/* Enable/disable publishes live through the designer API, then refreshes. */
document.addEventListener('click', async (event) => {
  const button = event.target.closest('.workflow-toggle');
  if (!button || !button.dataset.file || button.disabled) return;
  button.disabled = true;
  try {
    const result = await api(`/api/admin/designer/workflows/${encodeURIComponent(button.dataset.file)}`, {
      method: 'PUT',
      body: JSON.stringify({ enabled: button.dataset.enabled !== 'true' }),
    });
    await refresh();
    if (result.git_sync_error) notice(`Workflow is ${result.enabled ? 'On' : 'Off'}, but Git sync failed: ${result.git_sync_error}`, true);
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
});

/* Tags and Delete live in views/overview.js (dialog-based flows there). */

document.addEventListener('click', (event) => {
  const link = event.target.closest('.workflow-edit');
  if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
  event.preventDefault();
  void openDesigner(link.dataset.workflow);
});

document.addEventListener('click', (event) => {
  const button = event.target.closest('.workflow-detail');
  if (button) openWorkflow(button.dataset.workflow);
});

document.addEventListener('click', (event) => {
  const button = event.target.closest('.workflow-versions');
  if (!button || button.disabled) return;
  void openVersions(button.dataset.workflow);
});

/* Download YAML: the canonical text the API renders from the stored
   definition — the same bytes `dapier workflows export` writes — as a
   Blob download named after the workflow file. */
document.addEventListener('click', async (event) => {
  const button = event.target.closest('#workflow-download-yaml');
  if (!button || !button.dataset.file || button.disabled) return;
  button.disabled = true;
  try {
    const data = await api(`/api/admin/designer/workflows/${encodeURIComponent(button.dataset.file)}`);
    const blob = new Blob([data.yaml || ''], { type: 'text/yaml' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = button.dataset.file;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
});

/* Import YAML: a local file goes through the existing save endpoint — the
   same commit-and-publish path the designer's save uses — so an export from
   one deployment lands live on another (or restores here). */
$('#import-yaml').addEventListener('click', () => $('#import-yaml-input').click());
$('#import-yaml-input').addEventListener('change', async (event) => {
  const input = event.target;
  const file = input.files && input.files[0];
  input.value = '';
  if (!file) return;
  let yaml;
  try {
    yaml = await file.text();
  } catch (readError) {
    notice(`Cannot read ${file.name}.`, true);
    return;
  }
  try {
    const data = await api('/api/admin/designer/workflows', {
      method: 'PUT',
      body: JSON.stringify({ yaml }),
    });
    notice(`Imported ${data.file || file.name}${data.published ? ' — live now' : ' as a draft — publish it from the designer to go live'}.`);
    await refresh();
  } catch (error) {
    notice(error.message, true);
  }
});

document.addEventListener('click', (event) => {
  const button = event.target.closest('.version-restore');
  if (!button || button.disabled) return;
  void restoreVersion(button);
});

document.addEventListener('click', async (event) => {
  const button = event.target.closest('.workflow-runs, .workflow-run-link');
  if (!button) return;
  if (button.classList.contains('workflow-run-link')) return void openRun(button.dataset.run);
  if (!await setView('runs')) return;
  $('#runs-workflow-filter').value = button.dataset.workflow;
  $('#runs-status-filter').value = button.dataset.status || '';
  $('#runs-date-filter').value = '';
  renderRuns();
});

/* Replay re-injects a run's original trigger event; the rerun lands in the
   list once the worker picks it up, so refresh without closing the dialog. */
document.addEventListener('click', async (event) => {
  const button = event.target.closest('.run-replay');
  if (!button || !button.dataset.run || button.disabled) return;
  const dialog = $('#replay-confirm-dialog');
  const workflow = $('#run-title').textContent || 'this workflow';
  $('#replay-confirm-message').textContent = `Replay ${workflow} run ${button.dataset.run}? This re-injects its original trigger. Workflow actions may send messages or change external data a second time.`;
  dialog.returnValue = '';
  dialog.showModal();
  const confirmed = await new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true }));
  if (!confirmed) return;
  button.disabled = true;
  try {
    const data = await api(`/api/admin/runs/${encodeURIComponent(button.dataset.run)}/replay`, {
      method: 'POST',
      body: '{}',
    });
    const result = $('#run-replay-result');
    result.classList.remove('error');
    result.replaceChildren(document.createTextNode(`Replay queued as ${data.run_id || 'a new run'}. `));
    if (data.run_id) {
      const link = document.createElement('a');
      link.href = `/runs?run=${encodeURIComponent(data.run_id)}`;
      link.textContent = 'Open new run';
      result.append(link);
    }
    result.hidden = false;
    await refresh();
  } catch (error) {
    const result = $('#run-replay-result');
    result.textContent = error.message;
    result.classList.add('error');
    result.hidden = false;
  } finally {
    button.disabled = false;
  }
});
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  if (event.target.closest('button, a, input, select, textarea')) return; // native activation
  if (!event.target.closest('.workflow-open, .run-open')) return;
  event.preventDefault();
  openRowFor(event);
});
$$('.dialog-close').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
function closeMobileMenu(restoreFocus = false) {
  const sidebar = $('.sidebar');
  const wasOpen = sidebar.classList.contains('open');
  sidebar.classList.remove('open');
  sidebar.removeAttribute('role');
  sidebar.removeAttribute('aria-modal');
  sidebar.removeAttribute('aria-label');
  $('#sidebar-backdrop').hidden = true;
  $('main').inert = false;
  $('#menu-toggle').setAttribute('aria-expanded', 'false');
  $('#menu-toggle').setAttribute('aria-label', 'Open menu');
  if (wasOpen && restoreFocus) $('#menu-toggle').focus();
}

$$('.nav-item, .view-link').forEach((link) => link.addEventListener('click', async (event) => {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
  if (link.dataset.external) return; // full-page view with its own asset bundle
  if (!link.dataset.view && !link.dataset.target) return;
  event.preventDefault();
  // The designer's leave prompt lives in main. Remove the drawer's inert
  // layer before the asynchronous guard can show that prompt.
  closeMobileMenu(true);
  const target = link.dataset.view || link.dataset.target;
  const tab = link.dataset.tab;
  if (!await setView(target, true, tab ? { tab } : {})) return;
  if (target === 'agents') renderAgentTasks();
  if (target === 'workers') renderWorkers();
  if (target === 'audit') refreshAudit();
}));
$('#overview-attention').addEventListener('click', async (event) => {
  const connection = event.target.closest('.home-connection');
  if (connection) {
    if (await setView('connections')) openEditConnection(connection.dataset.connection);
    return;
  }

  const link = event.target.closest('.view-link');
  if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
  event.preventDefault();
  closeMobileMenu(true);
  if (!await setView(link.dataset.target)) return;
  if (link.dataset.runStatus) {
    $('#runs-status-filter').value = link.dataset.runStatus;
    $('#runs-status-filter').dispatchEvent(new Event('change'));
  }
  if (link.dataset.connectionStatus) {
    $('#connection-status-filter').value = link.dataset.connectionStatus;
    $('#connection-status-filter').dispatchEvent(new Event('change'));
  }
});
window.addEventListener('popstate', async () => {
  const view = viewFromPath(window.location.pathname);
  closeMobileMenu(true);
  if (await setView(view, false)) {
    if (view === 'designer') await designerFromLocation();
    if (view === 'agents') renderAgentTasks();
    if (view === 'workers') renderWorkers();
    if (view === 'audit') refreshAudit();
  }
});

/* Edit-in-designer and New workflow open the designer inside the console;
   the plain hrefs stay as the fallback for full-page loads. */
function designerClick(event) {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
  const target = event.target.closest('#workflow-edit, #new-workflow, [data-new-workflow]');
  if (!target) return;
  event.preventDefault();
  if (target.id === 'workflow-edit') $('#workflow-dialog').close();
  void openDesigner(target.id === 'workflow-edit' ? target.dataset.workflow : null);
}
document.addEventListener('click', designerClick);
$('#refresh').addEventListener('click', refresh);
$('#startup-retry').addEventListener('click', async () => {
  $('#startup-retry').disabled = true;
  try {
    const me = await api('/api/admin/me');
    if (!me.operator) { $('#startup-error').hidden = true; showForbidden(); return; }
    await refresh();
  } catch (error) {
    if ($('#forbidden-view').hidden) showStartupError(error.message);
  } finally {
    $('#startup-retry').disabled = false;
  }
});
$('#menu-toggle').addEventListener('click', () => {
  const open = $('.sidebar').classList.toggle('open');
  if (open) {
    $('.sidebar').setAttribute('role', 'dialog');
    $('.sidebar').setAttribute('aria-modal', 'true');
    $('.sidebar').setAttribute('aria-label', 'Main menu');
  } else {
    $('.sidebar').removeAttribute('role');
    $('.sidebar').removeAttribute('aria-modal');
    $('.sidebar').removeAttribute('aria-label');
  }
  $('#sidebar-backdrop').hidden = !open;
  $('main').inert = open;
  $('#menu-toggle').setAttribute('aria-expanded', String(open));
  $('#menu-toggle').setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
  if (open) $('.sidebar .nav-item').focus();
});
$('#sidebar-backdrop').addEventListener('click', () => closeMobileMenu(true));
$('#sidebar-close').addEventListener('click', () => closeMobileMenu(true));
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && $('.sidebar').classList.contains('open')) closeMobileMenu(true);
  if (event.key === 'Tab' && $('.sidebar').classList.contains('open')) {
    const items = $$('.sidebar a[href], .sidebar button:not([disabled])').filter((item) => !item.hidden && getComputedStyle(item).display !== 'none');
    const first = items[0], last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
window.matchMedia('(min-width: 861px)').addEventListener('change', (event) => { if (event.matches) closeMobileMenu(); });
$('#logout').addEventListener('click', async () => {
  if (await confirmDesignerLeave()) window.location.assign('/auth/logout');
});
$('#theme-toggle').addEventListener('click', toggleTheme);

window.addEventListener('DOMContentLoaded', async () => {
  icons();
  hardenSecretInputs();
  let me;
  try { me = await api('/api/admin/me'); } catch (error) {
    if ($('#forbidden-view').hidden) showStartupError(error.message);
    return;
  }
  if (!me.operator) { showForbidden(); return; }
  const initialView = viewFromPath(window.location.pathname);
  await setView(initialView, false);
  if (initialView === 'designer') await designerFromLocation();
  const initialRefresh = refresh();
  if (initialView === 'runs') {
    const runId = new URLSearchParams(window.location.search).get('run');
    if (runId) await openRun(runId);
  }
  if (initialView === 'audit') refreshAudit();
  if (initialView === 'agents') renderAgentTasks();
  if (initialView === 'workers') renderWorkers();
  const oauth = new URLSearchParams(window.location.search);
  if (initialView === 'connections' && oauth.has('oauth')) {
    showOAuthResult(oauth.get('oauth'), oauth.get('connection'));
  }
  /* The deep-link header sync above ran before the overview data arrived;
     now the workflow name can be filled in. */
  await initialRefresh;
  if (initialView === 'designer') await designerFromLocation();
});
