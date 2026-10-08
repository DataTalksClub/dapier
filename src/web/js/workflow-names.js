/* Human workflow names. The API computes each workflow's `name` (the
   `name:` override, else "<trigger> → <main actions>") and `name_source`;
   the console only renders it. The id stays the stable key every link, run,
   and CLI command carries, so it shows as small secondary mono text. Views
   that only hold a workflow_id (runs, agent tasks, audit) map it through the
   workflow list the console already loaded — no extra API calls. */
import { state } from './state.js';
import { escapeHtml } from './format.js';

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
