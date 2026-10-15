/* Hash-free routing: one path segment per view, history API driven. */
import { state } from './state.js';
import { $, $$ } from './ui.js';

const VIEWS = ['overview', 'workflows', 'designer', 'connections', 'emails', 'schedules', 'credentials', 'tokens', 'audit', 'costs', 'runs', 'agents', 'workers'];
const WORKFLOW_TABS = new Set(['list', 'hooks', 'polls']);
const EMAIL_TABS = new Set(['received', 'addresses']);
const NAV_FOR = {
  designer: 'workflows',
  schedules: 'workflows',
  credentials: 'connections',
};
const TAB_FAMILY = {
  workflows: 'workflows',
  schedules: 'workflows',
  connections: 'connections',
  credentials: 'connections',
  emails: 'emails',
};
let viewGuard = null;
let rememberedUrl = `${window.location.pathname}${window.location.search}`;

export function setViewGuard(guard) { viewGuard = guard; }
export function rememberViewUrl(url) { rememberedUrl = url; }

export function viewFromPath(path) {
  const name = path.replace(/^\/+|\/+$/g, '');
  if (name === 'inbox' || name === 'usage') return 'runs'; // compatibility with saved inbox links
  if (name === 'triggers') return 'workflows'; // old Triggers page → workflow start methods
  if (name === 'storage') return 'workflows'; // old Data page → a workflow's Stored data panel
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

function pathFor(view, workflowTab, emailTab) {
  if (view === 'overview') return '/';
  if (view === 'workflows' && (workflowTab === 'hooks' || workflowTab === 'polls')) {
    return `/workflows?tab=${workflowTab}`;
  }
  if (view === 'emails' && emailTab === 'addresses') {
    return '/emails?tab=addresses';
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

function emailTabFor(view, push, options) {
  if (view !== 'emails') return null;
  const requested = options.tab;
  if (requested && EMAIL_TABS.has(requested)) return requested;
  if (!push) {
    const tab = new URLSearchParams(window.location.search).get('tab');
    return EMAIL_TABS.has(tab) ? tab : 'received';
  }
  return 'received';
}

/* Family tabs swap the body under the strip: the Received / Addresses
   split works like the Workflows / Hooks / Polls split — one route per
   tab (?tab=addresses for the second), the header above the strip stays
   still, and back/forward re-reads the tab from the URL. */
export function applyEmailTab(tab) {
  const view = $('[data-page="emails"]');
  if (!view) return;
  $$('[data-email-panel]', view).forEach((panel) => {
    panel.hidden = panel.dataset.emailPanel !== tab;
  });
  $$('[data-email-tools]', view).forEach((tools) => {
    tools.hidden = tools.dataset.emailTools !== tab;
  });
}

function syncPageTabs(view, workflowTab, emailTab) {
  const tabs = $('#page-tabs');
  if (!tabs) return;
  const family = TAB_FAMILY[view];
  tabs.hidden = !family;
  $$('nav[data-family]', tabs).forEach((set) => {
    set.hidden = set.dataset.family !== family;
  });
  $$('a', tabs).forEach((link) => {
    const sameView = link.dataset.view === view;
    let sameTab = true;
    if (view === 'workflows') sameTab = (link.dataset.tab || 'list') === workflowTab;
    else if (view === 'emails') sameTab = (link.dataset.tab || 'received') === emailTab;
    if (sameView && sameTab) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  const current = $('a[aria-current="page"]', tabs);
  if (current && !tabs.hidden) globalThis.dakitTabs?.reveal(current);
}

export async function setView(view, push = true, options = {}) {
  if (view !== state.view && viewGuard && !(await viewGuard(view))) {
    if (!push) history.pushState(null, '', rememberedUrl);
    return false;
  }
  const workflowTab = workflowTabFor(view, push, options);
  const emailTab = emailTabFor(view, push, options);
  state.view = view;
  document.body.dataset.view = view; // CSS hooks (designer full-height canvas)
  if (workflowTab) document.body.dataset.workflowTab = workflowTab;
  else delete document.body.dataset.workflowTab;
  if (emailTab) document.body.dataset.emailTab = emailTab;
  else delete document.body.dataset.emailTab;
  const navView = NAV_FOR[view] || view;
  $$('.dk-sidebar .dk-nav-item').forEach((item) => {
    if (item.dataset.view === navView) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  $$('.view').forEach((page) => page.classList.toggle('active', page.dataset.page === view));
  if (view === 'workflows') applyWorkflowTab(workflowTab);
  if (view === 'emails') applyEmailTab(emailTab);
  syncPageTabs(view, workflowTab, emailTab);
  /* A list–detail split on an inactive view (or tab) carries [hidden], so
     dakit's height rules (.dk-main:has(.dk-split:not([hidden]))) apply only
     while one is on screen. */
  $$('.view .dk-split').forEach((split) => {
    split.hidden = !split.closest('.view.active') || (split.dataset.emailPanel && split.dataset.emailPanel !== emailTab);
  });
  /* Family tabs swap the body under the strip, not the page header above
     it. Child views (App setup, Hooks…) keep their own routes; the
     title stays the parent page. */
  const FAMILY_META = {
    workflows: ['Workflows', 'Published workflows and the triggers that start them.'],
    connections: ['Connections', 'The connected accounts workflows act through.'],
    emails: ['Emails', 'Mail Dapier received, and which workflow ran.'],
  };
  const VIEW_META = {
    overview: ['Home', 'Automations and their latest results at a glance.'],
    runs: ['Runs', 'Every trigger, run, and failure — newest first.'],
    agents: ['Agents', 'Agent tasks and the runs behind them.'],
    workers: ['Workers', 'Machines that run agent tasks.'],
    tokens: ['Access', 'Machine tokens for API access.'],
    costs: ['Costs', 'Recorded paid API charges and token usage.'],
    audit: ['Audit', 'Who changed what — newest first.'],
  };
  const family = TAB_FAMILY[view];
  const [title, description] = (family && FAMILY_META[family])
    || VIEW_META[view]
    || [view[0].toUpperCase() + view.slice(1), ''];
  $('#view-title').textContent = title;
  $('#view-description').textContent = description;
  if (!push && ['/inbox', '/usage'].includes(window.location.pathname)) history.replaceState(null, '', `/runs${window.location.search}`);
  if (!push && window.location.pathname === '/triggers') history.replaceState(null, '', '/workflows?tab=hooks');
  if (!push && window.location.pathname === '/storage') history.replaceState(null, '', '/workflows');
  if (push) history.pushState(null, '', pathFor(view, workflowTab, emailTab));
  rememberedUrl = `${window.location.pathname}${window.location.search}`;
  /* .dk-main is the shell's only scroller (the page itself never scrolls). */
  const main = $('#main-content');
  if (main) main.scrollTop = 0;
  return true;
}
