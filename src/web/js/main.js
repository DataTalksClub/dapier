/* Console bootstrap: global event wiring and the first fetch. */
import { $, $$, icons, notice, hardenSecretInputs, showForbidden } from './ui.js';
import { api } from './api.js';
import { setView, viewFromPath } from './router.js';
import { refresh, openRowFor } from './views/overview.js';
import { openDesigner, designerFromLocation } from './views/designer.js';

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
    await api(`/api/admin/designer/workflows/${encodeURIComponent(button.dataset.file)}`, {
      method: 'PUT',
      body: JSON.stringify({ enabled: button.dataset.enabled !== 'true' }),
    });
    await refresh();
  } catch (error) {
    notice(error.message, true);
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
$$('.nav-item, .view-link').forEach((link) => link.addEventListener('click', (event) => {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
  if (link.dataset.external) return; // full-page view with its own asset bundle
  event.preventDefault();
  setView(link.dataset.view || link.dataset.target);
}));
window.addEventListener('popstate', () => {
  const view = viewFromPath(window.location.pathname);
  setView(view, false);
  if (view === 'designer') designerFromLocation();
});

/* Edit-in-designer and New workflow open the designer inside the console;
   the plain hrefs stay as the fallback for full-page loads. */
function designerClick(event) {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
  const target = event.target.closest('#workflow-edit, #new-workflow');
  if (!target) return;
  event.preventDefault();
  if (target.id === 'workflow-edit') $('#workflow-dialog').close();
  openDesigner(target.id === 'workflow-edit' ? target.dataset.source : null);
}
document.addEventListener('click', designerClick);
$('#refresh').addEventListener('click', refresh);
$('#menu-toggle').addEventListener('click', () => $('.sidebar').classList.toggle('open'));
$('#logout').addEventListener('click', () => { window.location.assign('/auth/logout'); });

window.addEventListener('DOMContentLoaded', async () => {
  icons();
  hardenSecretInputs();
  let me;
  try { me = await api('/api/admin/me'); } catch (_) { return; }
  if (!me.operator) { showForbidden(); return; }
  const initialView = viewFromPath(window.location.pathname);
  setView(initialView, false);
  if (initialView === 'designer') designerFromLocation();
  await refresh();
  /* The deep-link header sync above ran before the overview data arrived;
     now the workflow name and GitHub link can be filled in. */
  if (initialView === 'designer') designerFromLocation();
});
