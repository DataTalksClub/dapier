/* Designer view: hosts the designer app shell (/designer/app) in an iframe.
   This is the console's workflow view — every workflow row opens it at
   /workflows/<id>. The canvas owns the viewport under the topbar: the
   topbar's h1 shows the workflow's human name (the API's generated
   "<trigger> → <actions>", or the `name:` override) with the description
   behind a (?) tip, and the stable id as small mono text under it. Clicking
   the name edits the override (empty = back to the generated name); the
   edit lands in the iframe's draft and saves with it — the id never changes
   here. The iframe reports what the title should show via designer:meta
   messages below, and the console answers with the connections snapshot the
   inspector's connection suggestions render from. */
import { state } from '../state.js';
import { $ } from '../ui.js';
import { escapeHtml } from '../format.js';
import { setView, rememberViewUrl, setViewGuard } from '../router.js';
import { workflowName, helpTip } from '../workflow-names.js';

/* Last designer:meta from the iframe: { id, saved, name, nameSource,
   description, enabled, source, editable, dirty }. */
let meta = null;
let editing = false;
let leaveSequence = 0;
const leaveRequests = new Map();

/* /workflows/<id> is the shareable URL for one workflow; "new" is the
   unsaved draft. */
function workflowUrl(ref) {
  return ref ? `/workflows/${encodeURIComponent(ref)}` : '/workflows/new';
}

function frameSrc(source) {
  const params = new URLSearchParams({ embed: '1' });
  if (source) params.set('workflow', source);
  return `/designer/app?${params}`;
}

/* The h1: the human name plus the description's (?) tip. The id stays
   off the header (the YAML tab and the list's row menu carry it). */
function paintTitle(name, description) {
  $('#view-title').innerHTML = `<span class="view-title-text">${escapeHtml(name)}</span>${helpTip(description)}`;
  $('#view-description').textContent = '';
}

function savedId() {
  return meta && meta.saved ? meta.id : null;
}

/* Paints the topbar from the iframe's live meta (name, id, renamable) and
   keeps /workflows/<id> in step so the view stays deep-linkable. */
function applyMeta() {
  if (state.view !== 'designer') return;
  const title = $('#view-title');
  title.classList.toggle('renamable', !!meta && meta.editable && !editing);
  if (meta) {
    if (!editing) paintTitle(meta.name || (meta.saved ? meta.id : 'New workflow'), meta.description);
    title.title = meta.editable
      ? (meta.nameSource === 'custom'
        ? 'Click to rename — clear the name to use the generated one'
        : 'Generated from the trigger and actions — click to rename')
      : '';
  } else {
    title.title = '';
  }
  const url = workflowUrl(savedId());
  if (`${window.location.pathname}${window.location.search}` !== url) {
    history.replaceState(null, '', url);
  }
  rememberViewUrl(url);
}

function syncHead(ref) {
  /* Runs after setView, which resets the h1 to the plain view name. A live
     iframe that already holds this exact file wins — the console's workflow
     list can be stale right after a rename. The ref is a workflow id (the
     URL form) or, for legacy /designer?workflow= links, a source file. */
  if (meta && ((meta.source ?? null) === (ref ?? null) || (meta.id ?? null) === (ref ?? null))) {
    applyMeta();
    return;
  }
  const workflow = ref
    ? (state.data?.workflows || []).find((item) => item.id === ref || item.source === ref)
    : null;
  const id = workflow ? workflow.id : ref ? ref.replace(/\.yaml$/, '') : '';
  paintTitle(workflow ? workflowName(workflow) : id || 'New workflow', workflow?.description);
  $('#view-title').title = '';
  $('#view-title').classList.remove('renamable');
}

/* Swaps the h1 for an input; Enter or blur accepts, Escape restores. The
   edit only changes the designer's draft until the workflow is saved; an
   empty name drops the override and the generated name comes back. */
const MAX_NAME_LENGTH = 80;

function startRename(event) {
  const title = $('#view-title');
  if (event?.target?.closest?.('.help-tip')) return;
  if (editing || state.view !== 'designer' || !meta?.editable) return;
  const current = meta.name || '';
  editing = true;
  title.classList.remove('renamable');
  const input = document.createElement('input');
  input.className = 'title-input';
  input.value = current;
  input.maxLength = MAX_NAME_LENGTH;
  input.placeholder = 'Name (empty = generated)';
  input.setAttribute('aria-label', 'Workflow name');
  input.spellcheck = false;
  title.textContent = '';
  title.appendChild(input);
  input.focus();
  input.select();
  let done = false;
  const finish = (commit) => {
    if (done) return;
    done = true;
    const value = input.value.replace(/\s+/g, ' ').trim();
    input.remove();
    editing = false;
    if (commit && value !== current && !(meta.nameSource !== 'custom' && !value)) {
      $('#designer-frame').contentWindow?.postMessage(
        { type: 'designer:set-name', name: value },
        window.location.origin
      );
    }
    applyMeta();
  };
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      finish(true);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      finish(false);
    }
  });
  input.addEventListener('blur', () => finish(true));
  input.addEventListener('click', (event) => event.stopPropagation());
}

/* Clicking the workflow name itself starts the rename (the h1 carries the
   renamable class and cursor); startRename no-ops everywhere else. */
$('#view-title').addEventListener('click', startRename);

/* Back out of the canvas to the workflow list. setView runs the view guard,
   so an unsaved draft raises the designer's own discard prompt first. */
$('#designer-back').addEventListener('click', () => { setView('workflows'); });

/* Connections snapshot for the designer's connection suggestions: only the
   metadata the inspector renders (never secrets). Exported for
   overview.refresh, which re-posts when data lands after the iframe did. */
export function postConnectionsToDesigner() {
  const frame = $('#designer-frame').contentWindow;
  if (!frame || !state.data) return;
  const connections = (state.data.connections || []).map((connection) => ({
    connection_id: connection.connection_id,
    provider: connection.provider,
    display_name: connection.display_name || '',
    account_title: connection.account_title || '',
    status: connection.status || '',
    refs: connection.refs || [],
  }));
  frame.postMessage({ type: 'designer:set-connections', connections }, window.location.origin);
}

window.addEventListener('message', (event) => {
  if (event.origin !== window.location.origin) return;
  if (event.source !== $('#designer-frame').contentWindow) return;
  const data = event.data;
  if (data?.type === 'designer:leave-result' && typeof data.requestId === 'string') {
    const finish = leaveRequests.get(data.requestId);
    if (finish) {
      leaveRequests.delete(data.requestId);
      finish(data.allowed === true);
    }
    return;
  }
  if (!data || data.type !== 'designer:meta') return;
  meta = {
    id: typeof data.id === 'string' ? data.id : '',
    saved: data.saved === true,
    name: typeof data.name === 'string' ? data.name : '',
    nameSource: data.nameSource === 'custom' ? 'custom' : 'auto',
    description: typeof data.description === 'string' ? data.description : '',
    enabled: data.enabled !== false,
    source: typeof data.source === 'string' && data.source ? data.source : null,
    editable: data.editable === true,
    dirty: data.dirty === true
  };
  applyMeta();
  // The iframe announces itself on mount; answer with the current snapshot
  // so suggestions are ready before the first node is selected.
  postConnectionsToDesigner();
});

/* Opens the designer for `source` (a workflow filename, or null for a new
   workflow) and leaves /designer?workflow=<source> as the shareable URL. */
export async function confirmDesignerLeave() {
  if (state.view !== 'designer' || !meta?.dirty) return true;
  const frame = $('#designer-frame').contentWindow;
  if (!frame) return false;
  const requestId = `leave-${++leaveSequence}`;
  return new Promise((resolve) => {
    const timeout = setTimeout(() => {
      leaveRequests.delete(requestId);
      resolve(false);
    }, 60000);
    leaveRequests.set(requestId, (allowed) => {
      clearTimeout(timeout);
      if (allowed) {
        // Drop the iframe draft when the operator chooses Discard. Saving is
        // harmless to reload too, and ensures a later visit sees server state.
        $('#designer-frame').setAttribute('src', 'about:blank');
        meta = null;
      }
      resolve(allowed);
    });
    frame.postMessage({ type: 'designer:request-leave', requestId }, window.location.origin);
  });
}

setViewGuard((next) => next === 'designer' ? Promise.resolve(true) : confirmDesignerLeave());

/* Opens the workflow `ref` (an id, a legacy source filename, or null for a
   new workflow) and leaves /workflows/<ref> as the shareable URL. */
export async function openDesigner(ref, push = true) {
  const frame = $('#designer-frame');
  const src = frameSrc(ref);
  if (frame.getAttribute('src') !== src) {
    if (!(await confirmDesignerLeave())) return false;
    meta = null; // a fresh page posts its own meta once loaded
    frame.setAttribute('src', src);
  }
  await setView('designer', false);
  syncHead(ref);
  const url = workflowUrl(ref);
  if (push) history.pushState(null, '', url);
  rememberViewUrl(url);
  return true;
}

/* Re-syncs the frame after a full-page load of, or popstate to,
   /workflows/<id> (or the legacy /designer?workflow=<source> links). */
export function designerFromLocation() {
  const match = window.location.pathname.match(/^\/workflows\/(.+)$/);
  const ref = match ? decodeURIComponent(match[1]) : new URLSearchParams(window.location.search).get('workflow');
  return openDesigner(ref === 'new' ? null : ref, false).then((opened) => {
    if (!opened) history.pushState(null, '', workflowUrl(savedId()));
    return opened;
  });
}
