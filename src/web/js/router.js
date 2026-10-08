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
};
const TAB_FAMILY = {
  workflows: 'workflows',
  emails: 'workflows',
  schedules: 'workflows',
  connections: 'connections',
  credentials: 'connections',
};
let viewGuard = null;
let rememberedUrl = `${window.location.pathname}${window.location.search}`;

export function setViewGuard(guard) { viewGuard = guard; }
export function rememberViewUrl(url) { rememberedUrl = url; }

/* The title row has an empty middle. A standalone view's .page-tools fills
   it, and comes back to the view before the next one is mounted so inactive
   pages keep their controls. Family tabs (Connections, Workflows)
   keep their actions in the body — the header above the tabs stays still. */
let mountedTools = null;
function mountPageTools() {
  if (mountedTools) {
    mountedTools.home.prepend(mountedTools.node);
    mountedTools = null;
  }
  const view = document.querySelector('.view.active');
  if (TAB_FAMILY[view?.dataset.page]) return;
  /* Only a page's primary actions join the topbar (title left, actions
     right). Filter and search bars stay in the content column, above the
     register they control. */
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

/* A tab strip wider than the screen scrolls; it carries .is-clipped (a
   trailing fade) while tabs remain hidden past its right edge. */
function syncTabOverflow() {
  const tabs = $('#page-tabs');
  if (!tabs || tabs.hidden) return;
  const hiddenRight = tabs.scrollWidth - tabs.clientWidth - tabs.scrollLeft > 1;
  tabs.classList.toggle('is-clipped', hiddenRight);
}
window.addEventListener('resize', syncTabOverflow);
document.addEventListener('scroll', (event) => {
  if (event.target && event.target.id === 'page-tabs') syncTabOverflow();
}, true);

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
  const current = $('.page-tabs a[aria-current="page"]');
  if (current && !tabs.hidden) current.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  syncTabOverflow();
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
  /* Family tabs swap the body under the strip, not the page header above
     it. Child views (App setup, Emails, Hooks…) keep their own
     routes; the title stays the parent page. */
  const FAMILY_META = {
    workflows: ['Workflows', 'Published workflows and the triggers that start them.'],
    connections: ['Connections', 'The connected accounts workflows act through.'],
  };
  const VIEW_META = {
    overview: ['Home', 'Automations and their latest results at a glance.'],
    runs: ['Runs', 'Every trigger, run, and failure — newest first.'],
    agents: ['Agents', 'Agent tasks and the runs behind them.'],
    workers: ['Workers', 'Machines that run agent tasks. Active means a check-in within two minutes; tasks stay queued until a worker is running.'],
    tokens: ['Access', 'Machine tokens for API access.'],
    storage: ['Data', 'Key-value data shared with workflows.'],
    audit: ['Audit', 'Who changed what — newest first.'],
  };
  const family = TAB_FAMILY[view];
  const [title, description] = (family && FAMILY_META[family])
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
