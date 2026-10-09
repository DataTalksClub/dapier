/* Human workflow names. The API computes each workflow's `name` (the
   `name:` override, else "<trigger> → <main actions>") and `name_source`;
   the console only renders it. The id stays the stable key every link, run,
   and CLI command carries; the Workflows list and the designer header keep
   it out of sight (search still matches it, the row menu copies it, the
   YAML tab shows it). Views that only hold a workflow_id (runs, agent
   tasks, audit) map it through the workflow list the console already
   loaded — no extra API calls. */
import { state } from './state.js';
import { escapeHtml } from './format.js';
import { providerMark } from './ui.js';
import { icon } from './icons.js';
import { eventLabel, actionLabel, connectorLabel } from './catalog-labels.js';

function workflowById(id) {
  return (state.data?.workflows || []).find((workflow) => workflow.id === id) || null;
}

/* The display name for a workflow object or id; falls back to the id. */
export function workflowName(workflowOrId) {
  const workflow = typeof workflowOrId === 'string' ? workflowById(workflowOrId) : workflowOrId;
  const id = typeof workflowOrId === 'string' ? workflowOrId : workflow?.id;
  return (workflow && workflow.name) || id || '';
}

/* The (?) hover tip the Connections page uses (.help-tip in app.css). */
export function helpTip(text) {
  if (!text) return '';
  const safe = escapeHtml(text);
  return `<button type="button" class="help-tip" aria-label="${safe}" data-tip="${safe}">?</button>`;
}

/* "Name" plus the id underneath when the name is not just the id. */
export function workflowIdLine(workflowOrId) {
  const id = typeof workflowOrId === 'string' ? workflowOrId : workflowOrId?.id;
  if (!id || workflowName(workflowOrId) === id) return '';
  return `<span class="workflow-id mono">${escapeHtml(id)}</span>`;
}

/* Name text + secondary id for cells that only know the workflow_id. */
export function workflowLabelHtml(id, fallback = 'Run') {
  if (!id) return escapeHtml(fallback);
  return `${escapeHtml(workflowName(id))}${workflowIdLine(id)}`;
}

/* --- The Workflows list's Apps strip ------------------------------------------
   Zapier-style: the apps a flow touches, in order (trigger first), each as
   its brand mark in a small bordered tile. Purely presentational — read
   from the workflow's own trigger and steps with the catalog's labels. */

/* Action type prefix → app (the designer's appPrefixes, designer/src/catalog.ts). */
const APP_PREFIXES = [
  [/^slack(_|$)/, 'slack'], [/^telegram(_|$)/, 'telegram'], [/^gmail(_|$)/, 'gmail'],
  [/^email(_|$)/, 'email'], [/^dropbox(_|$)/, 'dropbox'], [/^s3(_|$)/, 's3'],
  [/^mailchimp(_|$)/, 'mailchimp'], [/^drive(_|$)/, 'google-drive'], [/^youtube(_|$)/, 'youtube'],
  [/^calendar(_|$)/, 'google-calendar'], [/^sheets(_|$)/, 'google-sheets'], [/^zoom(_|$)/, 'zoom'],
];
/* Steps that shape data rather than reach an app: no tile of their own
   unless they are all the flow has. */
const PLUMBING = new Set(['date_time', 'code', 'js', 'filter', 'condition', 'paths', 'delay',
  'for_each', 'digest', 'digest_add', 'digest_flush', 'formatter', 'storage_get', 'storage_set',
  'storage_delete', 'storage_find', 'csv_parse', 'csv_format']);
const BRANDS = new Set(['slack', 'dropbox', 'telegram', 'youtube', 'zoom']);
const GOOGLE = new Set(['gmail', 'google-drive', 'google-sheets', 'google-calendar', 'google']);
/* Family stroke icons for app-less steps and triggers (icons.js names);
   anything else gets the neutral plug. */
const GLYPHS = {
  webhook: 'webhook', custom: 'webhook', email: 'mail', mailchimp: 'mail', schedule: 'clock',
  poll: 'refresh-cw', agent: 'bot', code: 'code-2', js: 'code-2', dataops: 'database',
  s3: 'database', renderer: 'file-text', render_html_to_pdf: 'file-text', run_workflow: 'workflow',
};

/* Every step in run order, branches included. */
function flattenSteps(steps, out = []) {
  for (const step of steps || []) {
    if (!step || typeof step !== 'object') continue;
    if (step.type) out.push(String(step.type));
    for (const key of ['then', 'else', 'actions', 'default']) {
      if (Array.isArray(step[key])) flattenSteps(step[key], out);
    }
    if (Array.isArray(step.paths)) step.paths.forEach((branch) => flattenSteps(branch?.actions, out));
  }
  return out;
}

function stepApp(type) {
  const hit = APP_PREFIXES.find(([pattern]) => pattern.test(type));
  return hit ? hit[1] : null;
}

function workflowTriggers(workflow) {
  const many = Array.isArray(workflow.triggers) && workflow.triggers.length ? workflow.triggers : [workflow.trigger];
  return many.filter((trigger) => trigger && trigger.connector);
}

function keyLabel(key) {
  const words = String(key || '').replace(/[-_.]+/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/* The flow in words, for hovers: "Webhook request → Code (Python) → Agent". */
export function workflowFlowText(workflow) {
  const [trigger] = workflowTriggers(workflow);
  const extra = (workflow.triggerCount || 1) - 1;
  const when = trigger
    ? `${eventLabel(trigger.connector, trigger.event)}${extra > 0 ? ` +${extra} more` : ''}`
    : 'No trigger yet';
  const steps = [...new Set(flattenSteps(workflow.actions).map((type) => actionLabel(type)))];
  return [when, ...steps].join(' → ');
}

/* [{key, label}]: the trigger apps, then the apps the steps reach, deduped. */
export function workflowApps(workflow) {
  const apps = [];
  const add = (key, label) => {
    if (key && !apps.some((app) => app.key === key)) apps.push({ key, label });
  };
  workflowTriggers(workflow).forEach((trigger) =>
    add(trigger.connector, connectorLabel(trigger.connector) || keyLabel(trigger.connector)));
  const steps = flattenSteps(workflow.actions);
  const reaching = steps.filter((type) => stepApp(type) || !PLUMBING.has(type));
  (reaching.length ? reaching : steps).forEach((type) => {
    const app = stepApp(type);
    add(app || type, app ? (connectorLabel(app) || keyLabel(app)) : actionLabel(type));
  });
  return apps;
}

function appMark(key) {
  if (BRANDS.has(key)) return providerMark(key);
  if (GOOGLE.has(key)) return providerMark('google');
  return GLYPHS[key] ? icon(GLYPHS[key], 'brand-mark') : providerMark(key);
}

/* The strip: up to three tiles; more collapse the middle to "+N". Its
   hover reads the whole flow. */
export function appStripHtml(workflow, title) {
  const apps = workflowApps(workflow);
  const tile = (app) => `<span class="app-tile">${appMark(app.key)}</span>`;
  let tiles = apps.map(tile).join('');
  if (apps.length > 3) {
    const hidden = apps.slice(1, -1);
    tiles = `${tile(apps[0])}<span class="app-tile app-tile--more">+${hidden.length}</span>${tile(apps[apps.length - 1])}`;
  }
  const names = apps.map((app) => app.label).join(', ') || 'No apps yet';
  return `<span class="app-strip" role="img" aria-label="Apps: ${escapeHtml(names)}" title="${escapeHtml(title || names)}">${tiles}</span>`;
}

/* The name's lead icon: what kind of trigger starts the flow. */
export function triggerKindIcon(workflow) {
  const connector = workflow.trigger?.connector || '';
  const glyph = { webhook: 'webhook', custom: 'webhook', email: 'mail', gmail: 'mail', schedule: 'clock', poll: 'refresh-cw' }[connector] || 'zap';
  return icon(glyph, 'trigger-kind');
}
