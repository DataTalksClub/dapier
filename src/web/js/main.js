/* Console bootstrap: global event wiring and the first fetch. */
import { $, $$, icons, notice, hardenSecretInputs, showForbidden } from './ui.js';
import { api } from './api.js';
import { setView, viewFromPath } from './router.js';
import { refresh, openRowFor } from './views/overview.js';

['copy', 'cut', 'dragstart'].forEach((type) => document.addEventListener(type, (event) => {
  if (event.target instanceof Element && event.target.closest('.secret-input')) event.preventDefault();
}));

document.addEventListener('click', openRowFor);
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
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
