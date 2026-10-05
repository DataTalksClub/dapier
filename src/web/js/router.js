/* Hash-free routing: one path segment per view, history API driven. */
import { state } from './state.js';
import { $, $$ } from './ui.js';

const VIEWS = ['overview', 'workflows', 'designer', 'connections', 'emails', 'schedules', 'credentials', 'tokens', 'storage', 'audit', 'runs', 'agents', 'workers'];
const WORKFLOW_TABS = new Set(['list', 'hooks', 'polls']);
const NAV_FOR = {
  designer: 'workflows',
  emails: 'workflows',
  schedules: 'workflows',
  credentials: 'connections',
  workers: 'agents',
};
const TAB_FAMILY = {
  workflows: 'workflows',
  emails: 'workflows',
  schedules: 'workflows',
  connections: 'connections',
  credentials: 'connections',
  agents: 'agents',
  workers: 'agents',
};
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
  const tools = view?.querySelector(':scope > .page-tools.primary-tools:not([hidden])');
  const slot = $('#topbar-tools');
  if (!tools || !slot) return;
  mountedTools = { node: tools, home: view };
  slot.replaceChildren(tools);
}

export function viewFromPath(path) {
  const name = path.replace(/^\/+|\/+$/g, '');
  if (name === 'inbox' || name === 'usage') return 'runs'; // compatibility with saved inbox links
  if (name === 'triggers') return 'workflows'; // old Triggers page → workflow start methods
  if (name.startsWith('workflows/')) return 'designer'; // /workflows/<id> opens the canvas
  return VIEWS.includes(name) ? name : 'overview';
}

function workflowTabFor(view, push, options) {
  if (view !== 'workflows') return null;
  const requested = options.tab;
  if (requested && WORKFLOW_TABS.has(requested)) return requested;
  if (!push) {
    if (window.location.pathname === '/triggers') return 'hooks';
    const tab = new URLSearchParams(window.location.search).get('tab');
    return WORKFLOW_TABS.has(tab) ? tab : 'list';
  }
  return 'list';
}

function pathFor(view, workflowTab) {
  if (view === 'overview') return '/';
  if (view === 'workflows' && (workflowTab === 'hooks' || workflowTab === 'polls')) {
    return `/workflows?tab=${workflowTab}`;
  }
  return `/${view}`;
}

function applyWorkflowTab(tab) {
  const view = $('[data-page="workflows"]');
  if (!view) return;
  $$('[data-workflow-panel]', view).forEach((panel) => {
    panel.hidden = panel.dataset.workflowPanel !== tab;
  });
  $$('[data-panel-tools]', view).forEach((tools) => {
    tools.hidden = tools.dataset.panelTools !== tab;
  });
}

function syncPageTabs(view, workflowTab) {
  const tabs = $('#page-tabs');
  if (!tabs) return;
  const family = TAB_FAMILY[view];
  tabs.hidden = !family;
  $$('.page-tab-set', tabs).forEach((set) => {
    set.hidden = set.dataset.family !== family;
  });
  $$('.page-tabs a').forEach((link) => {
    const sameView = link.dataset.view === view;
    const sameTab = view !== 'workflows' || (link.dataset.tab || 'list') === workflowTab;
    if (sameView && sameTab) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}

export async function setView(view, push = true, options = {}) {
  if (view !== state.view && viewGuard && !(await viewGuard(view))) {
    if (!push) history.pushState(null, '', rememberedUrl);
    return false;
  }
  const workflowTab = workflowTabFor(view, push, options);
  state.view = view;
  document.body.dataset.view = view; // CSS hooks (designer full-height canvas)
  if (workflowTab) document.body.dataset.workflowTab = workflowTab;
  else delete document.body.dataset.workflowTab;
  const navView = NAV_FOR[view] || view;
  $$('.nav-item').forEach((item) => {
    item.classList.toggle('active', item.dataset.view === navView);
    if (item.dataset.view === navView) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  $$('.view').forEach((page) => page.classList.toggle('active', page.dataset.page === view));
  if (view === 'workflows') applyWorkflowTab(workflowTab);
  syncPageTabs(view, workflowTab);
  const VIEW_META = {
    overview: ['Home', 'Automations and their latest results at a glance.'],
    workflows: ['Workflows', 'Published workflows and the triggers that start them.'],
    runs: ['Runs', 'Every trigger, run, and failure — newest first.'],
    connections: ['Connections', 'The connected accounts workflows act through.'],
    credentials: ['App setup', 'OAuth clients and provider keys.'],
    agents: ['Agents', 'Agent tasks and the runs behind them.'],
    workers: ['Workers', 'Worker check-ins and capacity.'],
    tokens: ['Access', 'Machine tokens for API access.'],
    storage: ['Data', 'Key-value data shared with workflows.'],
    schedules: ['Schedules', 'Cron schedules that run workflows on a clock.'],
    emails: ['Emails', 'Email addresses that trigger workflows.'],
    audit: ['Audit', 'Who changed what — newest first.'],
  };
  const workflowTitles = {
    hooks: ['Hooks', 'Webhook and Telegram triggers that start workflows.'],
    polls: ['Polls', 'API polls that start workflows when new items appear.'],
  };
  const [title, description] = (view === 'workflows' && workflowTitles[workflowTab])
    || VIEW_META[view]
    || [view[0].toUpperCase() + view.slice(1), ''];
  $('#view-title').textContent = title;
  $('#view-description').textContent = description;
  $('.sidebar').classList.remove('open');
  $('#menu-toggle')?.setAttribute('aria-expanded', 'false');
  if (!push && ['/inbox', '/usage'].includes(window.location.pathname)) history.replaceState(null, '', `/runs${window.location.search}`);
  if (!push && window.location.pathname === '/triggers') history.replaceState(null, '', '/workflows?tab=hooks');
  if (push) history.pushState(null, '', pathFor(view, workflowTab));
  rememberedUrl = `${window.location.pathname}${window.location.search}`;
  mountPageTools();
  window.scrollTo(0, 0);
  return true;
}
