/* Hash-free routing: one path segment per view, history API driven. */
import { state } from './state.js';
import { $, $$ } from './ui.js';

const VIEWS = ['overview', 'workflows', 'designer', 'connections', 'emails', 'schedules', 'credentials', 'tokens', 'storage', 'audit', 'runs', 'agents', 'workers'];
let viewGuard = null;
let rememberedUrl = `${window.location.pathname}${window.location.search}`;

export function setViewGuard(guard) { viewGuard = guard; }
export function rememberViewUrl(url) { rememberedUrl = url; }

/* The title row has an empty middle. Each view's .page-tools fills it, and
   comes back to the view before the next one is mounted so inactive pages
   keep their controls. */
let mountedTools = null;
function mountPageTools() {
  if (mountedTools) {
    mountedTools.home.prepend(mountedTools.node);
    mountedTools = null;
  }
  const view = document.querySelector('.view.active');
  /* Only a page's primary actions join the topbar (the family's header
     pattern: title left, actions right). Filter and search bars stay in the
     content column, above the register they control. */
  const tools = view?.querySelector(':scope > .page-tools.primary-tools');
  const slot = $('#topbar-tools');
  if (!tools || !slot) return;
  mountedTools = { node: tools, home: view };
  slot.replaceChildren(tools);
}

export function viewFromPath(path) {
  const name = path.replace(/^\/+|\/+$/g, '');
  if (name === 'inbox' || name === 'usage') return 'runs'; // compatibility with saved inbox links
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
  const VIEW_META = {
    overview: ['Home', 'Automations and their latest results at a glance.'],
    workflows: ['Workflows', 'Published workflows and the triggers that start them.'],
    runs: ['Runs', 'Every trigger, run, and failure — newest first.'],
    connections: ['Connections', 'The connected accounts workflows act through.'],
    agents: ['Agents', 'Agent tasks and the runs behind them.'],
    workers: ['Workers', 'Worker check-ins and capacity.'],
    credentials: ['Credentials', 'Provider keys and OAuth clients.'],
    tokens: ['API tokens', 'Machine tokens for API access.'],
    storage: ['Data store', 'Key-value data shared with workflows.'],
    schedules: ['Schedules', 'Cron schedules that run workflows on a clock.'],
    emails: ['Emails', 'Email addresses that trigger workflows.'],
    audit: ['Audit log', 'Who changed what — newest first.'],
  };
  const [title, description] = VIEW_META[view] || [view[0].toUpperCase() + view.slice(1), ''];
  $('#view-title').textContent = title;
  $('#view-description').textContent = description;
  $('.sidebar').classList.remove('open');
  $('#menu-toggle')?.setAttribute('aria-expanded', 'false');
  if (!push && ['/inbox', '/usage'].includes(window.location.pathname)) history.replaceState(null, '', `/runs${window.location.search}`);
  if (push) history.pushState(null, '', view === 'overview' ? '/' : `/${view}`);
  rememberedUrl = `${window.location.pathname}${window.location.search}`;
  mountPageTools();
  window.scrollTo(0, 0);
  return true;
}
