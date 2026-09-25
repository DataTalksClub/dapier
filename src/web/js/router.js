/* Hash-free routing: one path segment per view, history API driven. */
import { state } from './state.js';
import { $, $$ } from './ui.js';

const VIEWS = ['overview', 'workflows', 'designer', 'connections', 'credentials', 'tokens', 'runs'];

export function viewFromPath(path) {
  const name = path.replace(/^\/+|\/+$/g, '');
  return VIEWS.includes(name) ? name : 'overview';
}

export function setView(view, push = true) {
  state.view = view;
  document.body.dataset.view = view; // CSS hooks (designer full-height canvas)
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
