/* The trigger family's shared vocabulary (Emails, Schedules, Hooks, Polls).

   Every tab reads the same way: filter chips and the tab's actions in one
   toolbar; the activity register (what arrived and what happened to it) as
   the main column; the tab's sources (addresses, schedules, endpoints,
   polls) beside it. Each source of activity maps its own outcome codes onto
   ONE vocabulary, so "Handled", "Failed", "Refused" and "No workflow" mean
   the same on every tab. Rendering only; every tab keeps its own API calls. */
import { escapeHtml, formatTimestamp } from '../format.js';
import { workflowName } from '../workflow-names.js';

export const OUTCOMES = {
  handled: { kind: 'ok', label: 'Handled' },
  failed: { kind: 'err', label: 'Failed' },
  refused: { kind: 'warn', label: 'Refused' },
  none: { kind: 'off', label: 'No workflow' },
  processing: { kind: 'run', label: 'Processing' },
  filtered: { kind: 'off', label: 'Filtered' },
  ignored: { kind: 'off', label: 'Ignored' },
  paused: { kind: 'off', label: 'Paused' },
  resumed: { kind: 'off', label: 'Resumed' },
};

/* The chips every tab offers, in this order; a tab adds Refused when it
   has a sender list (Emails). */
export const OUTCOME_FILTERS = [
  ['all', 'All'],
  ['handled', 'Handled'],
  ['failed', 'Failed'],
  ['none', 'No workflow'],
];

export function filterChips(filters, active, attribute) {
  return filters.map(([value, label]) => `<button type="button" class="dk-filter-chip" data-${attribute}="${escapeHtml(value)}" aria-pressed="${value === active}">${escapeHtml(label)}</button>`).join('');
}

/* "4 min ago" for scanning; the exact time rides in the title. */
export function ago(value) {
  if (!value) return '';
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return String(value);
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days} days ago`;
  return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export function whenHtml(value, empty = '—') {
  if (!value) return `<span class="muted-cell">${escapeHtml(empty)}</span>`;
  return `<time datetime="${escapeHtml(value)}" title="${escapeHtml(formatTimestamp(value) || '')}">${escapeHtml(ago(value))}</time>`;
}

/* The workflows a row reached, by name (ids in the title). Plain text: the
   row is the one target, and its detail opens each run. */
export function workflowNames(ids) {
  const list = (ids || []).filter(Boolean);
  if (!list.length) return '';
  const first = escapeHtml(workflowName(list[0]));
  const more = list.length > 1 ? ` +${list.length - 1} more` : '';
  return `<span title="${escapeHtml(list.join(', '))}">${first}${more}</span>`;
}

export function runLinks(runs) {
  return workflowNames((runs || []).map((run) => run.workflow_id || run.workflow || ''));
}

/* The outcome cell: dot + one word, then at most one muted line — who
   handled it, or what went wrong (two lines, full text in the title). */
export function outcomeCell(key, { by = '', note = '', error = '' } = {}) {
  const outcome = OUTCOMES[key] || { kind: 'off', label: key || 'Unknown' };
  const line = by ? `<span class="activity-note">by ${by}</span>`
    : note ? `<span class="activity-note">${note}</span>` : '';
  return `<span class="status ${outcome.kind}"><span class="status-dot" aria-hidden="true"></span>${escapeHtml(outcome.label)}</span>${line}${error
    ? `<span class="activity-error" title="${escapeHtml(error)}">${escapeHtml(error)}</span>` : ''}`;
}

/* One activity row: when | what (primary + secondary line) | outcome. The
   row opens its detail; links inside the outcome act on their own. */
export function activityRow({ attrs = '', label = '', when, primary, secondary = '', outcome }) {
  return `<li class="activity-row" ${attrs} tabindex="0" role="button" aria-label="${escapeHtml(label)}">
    <span class="activity-when">${when}</span>
    <span class="activity-main"><span class="activity-primary">${primary}</span>${secondary ? `<span class="activity-secondary">${secondary}</span>` : ''}</span>
    <span class="activity-outcome">${outcome}</span>
  </li>`;
}

/* One source row (an address, endpoint, poll or schedule): name with its
   state, then short lines. `open` names the button that opens it. */
export function sourceRow({ attrs = '', name, nameAttrs = '', nameClass = '', nameTitle = '', mono = true, status = '', lines = [], tools = '', selected = false }) {
  return `<li class="source-row${selected ? ' is-selected' : ''}" ${attrs}>
    <div class="source-head">
      <button type="button" class="source-name${mono ? ' mono' : ''}${nameClass ? ` ${nameClass}` : ''}" ${nameAttrs} title="${escapeHtml(nameTitle || '')}">${name}</button>
      ${status ? `<span class="source-status">${status}</span>` : ''}
      ${tools}
    </div>
    ${lines.filter(Boolean).map((line) => (typeof line === 'string'
    ? `<div class="source-line">${line}</div>`
    : `<div class="source-line" title="${escapeHtml(line.title || '')}">${line.html}</div>`)).join('')}
  </li>`;
}
