/* Hash-free routing: one path segment per view, history API driven. */
import { state } from './state.js';
import { $, $$ } from './ui.js';

const VIEWS = ['overview', 'workflows', 'designer', 'connections', 'emails', 'credentials', 'tokens', 'runs'];
let viewGuard = null;
let rememberedUrl = `${window.location.pathname}${window.location.search}`;

export function setViewGuard(guard) { viewGuard = guard; }
export function rememberViewUrl(url) { rememberedUrl = url; }

export function viewFromPath(path) {
  const name = path.replace(/^\/+|\/+$/g, '');
  if (name.startsWith('workflows/')) return 'designer'; // /workflows/<id> opens the canvas
  return VIEWS.includes(name) ? name : 'overview';
}

export async function setView(view, push = true) {
  if (view !== state.view && viewGuard && !(await viewGuard(view))) {
    if (!push) history.pushState(null, '', rememberedUrl);
    return false;
  }
  state.view = view;
  document.body.dataset.view = view; // CSS hooks (designer full-height canvas)
  $$('.nav-item').forEach((item) => {
    item.classList.toggle('active', item.dataset.view === (view === 'designer' ? 'workflows' : view));
    if (item.dataset.view === (view === 'designer' ? 'workflows' : view)) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  $$('.view').forEach((page) => page.classList.toggle('active', page.dataset.page === view));
  $('#view-title').textContent = ({ runs: 'Run history', tokens: 'API tokens', emails: 'Emails' })[view] || view[0].toUpperCase() + view.slice(1);
  $('.sidebar').classList.remove('open');
  $('#menu-toggle')?.setAttribute('aria-expanded', 'false');
  if (push) history.pushState(null, '', view === 'overview' ? '/' : `/${view}`);
  rememberedUrl = `${window.location.pathname}${window.location.search}`;
  window.scrollTo(0, 0);
  return true;
}
