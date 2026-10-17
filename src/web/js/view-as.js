/* Read-only "view as" for a full operator. The choice filters workflows,
   runs, and agent tasks to one person. It does not change the signed-in
   account, and write controls stay hidden while it is on. */
import { $ } from './ui.js';
import { escapeHtml } from './format.js';

const KEY = 'dapier.viewAs';

export function viewAsAlias() {
  try {
    return sessionStorage.getItem(KEY) || '';
  } catch (error) {
    return '';
  }
}

export function viewAsReadOnly() {
  return Boolean(viewAsAlias());
}

export function viewAsIdentities() {
  const alias = viewAsAlias();
  if (!alias) return null;
  const option = document.querySelector(`#view-as option[value="${CSS.escape(alias)}"]`);
  const raw = (option && option.dataset.identities) || alias;
  return new Set(String(raw).split(',').map((part) => part.trim().toLowerCase()).filter(Boolean));
}

export function withOwner(params) {
  const alias = viewAsAlias();
  if (alias) params.set('owner', alias);
  return params;
}

function syncBody() {
  const alias = viewAsAlias();
  if (alias) document.body.dataset.viewAs = alias;
  else delete document.body.dataset.viewAs;
}

const WRITE_SELECTOR = [
  '.run-replay', '.run-cancel', '.run-resolve', '.run-replay-from-step',
  '#runs-replay-failed', '.workflow-toggle', '.workflow-resume',
  '.workflow-tags', '.workflow-folder', '.workflow-duplicate', '.workflow-delete',
  '#import-yaml', '#workflow-bulk-bar', '[data-new-workflow]',
  '#new-workflow', '#home-new-workflow',
].join(', ');

export function bindViewAs(me) {
  const label = $('#view-as-label');
  const select = $('#view-as');
  if (!label || !select || select.dataset.bound === '1') {
    syncBody();
    return;
  }
  const people = (me && me.view_as) || [];
  if (me && me.role) document.body.dataset.role = me.role;
  if (!people.length) {
    label.hidden = true;
    syncBody();
    return;
  }
  label.hidden = false;
  select.innerHTML = `<option value="">Everyone</option>${people.map((person) => {
    const alias = escapeHtml(person.alias || '');
    const identities = escapeHtml((person.identities || []).join(','));
    const name = escapeHtml(person.label || person.alias || '');
    return `<option value="${alias}" data-identities="${identities}">${name}</option>`;
  }).join('')}`;
  const current = viewAsAlias();
  if (current && [...select.options].some((option) => option.value === current)) {
    select.value = current;
  } else {
    select.value = '';
    try { sessionStorage.removeItem(KEY); } catch (error) { /* private mode */ }
  }
  syncBody();
  select.dataset.bound = '1';
  select.addEventListener('change', () => {
    try {
      if (select.value) sessionStorage.setItem(KEY, select.value);
      else sessionStorage.removeItem(KEY);
    } catch (error) { /* private mode: the choice lasts for this paint only */ }
    syncBody();
    window.dispatchEvent(new CustomEvent('dapier-view-as'));
  });
  document.addEventListener('click', (event) => {
    if (!viewAsReadOnly()) return;
    const write = event.target.closest(WRITE_SELECTOR);
    if (!write) return;
    event.preventDefault();
    event.stopPropagation();
  }, true);
}
