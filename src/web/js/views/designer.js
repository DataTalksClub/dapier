/* Designer view: hosts the designer app shell (/designer/app) in an iframe.
   This is the console's workflow view — every workflow row opens it. The
   canvas owns the viewport under the topbar: the topbar's h1 names the open
   workflow and the adjacent Rename button edits the name (the edit
   lands in the iframe's draft and commits with "Save to git"). The iframe
   reports what the title should show via designer:meta messages below. */
import { state } from '../state.js';
import { $ } from '../ui.js';
import { setView, rememberViewUrl, setViewGuard } from '../router.js';

/* Last designer:meta from the iframe: { id, enabled, source, editable }. */
let meta = null;
let editing = false;
let leaveSequence = 0;
const leaveRequests = new Map();

function frameSrc(source) {
  const params = new URLSearchParams({ embed: '1' });
  if (source) params.set('workflow', source);
  return `/designer/app?${params}`;
}

function syncGithub(source) {
  const github = $('#designer-github');
  if (source && state.data?.workflows_edit_base) {
    github.href = `${state.data.workflows_edit_base}/${encodeURIComponent(source)}`;
    github.hidden = false;
  } else {
    github.hidden = true;
  }
}

/* Paints the topbar from the iframe's live meta (id, file, renamable) and
   keeps /designer?workflow=… in step so the view stays deep-linkable. */
function applyMeta() {
  if (state.view !== 'designer') return;
  const title = $('#view-title');
  const rename = $('#designer-rename');
  title.classList.toggle('renamable', !!meta && meta.editable && !editing);
  rename.hidden = !meta?.editable || editing;
  if (meta) {
    if (!editing) title.textContent = meta.id || 'Designer';
    title.title = '';
  } else {
    title.title = '';
  }
  syncGithub(meta ? meta.source : null);
  const url = meta && meta.source ? `/designer?workflow=${encodeURIComponent(meta.source)}` : '/designer';
  if (`${window.location.pathname}${window.location.search}` !== url) {
    history.replaceState(null, '', url);
  }
  rememberViewUrl(url);
}

function syncHead(source) {
  /* Runs after setView, which resets the h1 to the plain view name. A live
     iframe that already holds this exact file wins — the console's workflow
     list can be stale right after a rename. */
  if (meta && (meta.source ?? null) === (source ?? null)) {
    applyMeta();
    return;
  }
  const workflow = source
    ? (state.data?.workflows || []).find((item) => item.source === source)
    : null;
  $('#view-title').textContent = workflow ? workflow.id : source ? source.replace(/\.yaml$/, '') : 'Designer';
  $('#view-title').title = '';
  $('#view-title').classList.remove('renamable');
  $('#designer-rename').hidden = true;
  syncGithub(workflow ? workflow.source : null);
}

/* Swaps the h1 for an input; Enter or blur commits, Escape restores. The
   commit only edits the designer's draft — it publishes via "Save to git". */
function startRename() {
  const title = $('#view-title');
  if (editing || state.view !== 'designer' || !meta?.editable) return;
  const current = title.textContent;
  editing = true;
  $('#designer-rename').hidden = true;
  title.classList.remove('renamable');
  const input = document.createElement('input');
  input.className = 'title-input';
  input.value = current;
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
    const value = input.value.trim();
    input.remove();
    editing = false;
    title.textContent = commit && value ? value : current;
    if (commit && value && value !== current) {
      $('#designer-frame').contentWindow?.postMessage(
        { type: 'designer:set-id', id: value },
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

$('#designer-rename').addEventListener('click', startRename);

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
    enabled: data.enabled !== false,
    source: typeof data.source === 'string' && data.source ? data.source : null,
    editable: data.editable === true,
    dirty: data.dirty === true
  };
  applyMeta();
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

export async function openDesigner(source, push = true) {
  const frame = $('#designer-frame');
  const src = frameSrc(source);
  if (frame.getAttribute('src') !== src) {
    if (!(await confirmDesignerLeave())) return false;
    meta = null; // a fresh page posts its own meta once loaded
    frame.setAttribute('src', src);
  }
  await setView('designer', false);
  syncHead(source);
  const url = source ? `/designer?workflow=${encodeURIComponent(source)}` : '/designer';
  if (push) history.pushState(null, '', url);
  rememberViewUrl(url);
  return true;
}

/* Re-syncs the frame after a full-page load of, or popstate to, /designer. */
export function designerFromLocation() {
  return openDesigner(new URLSearchParams(window.location.search).get('workflow'), false).then((opened) => {
    if (!opened) history.pushState(null, '', meta?.source ? `/designer?workflow=${encodeURIComponent(meta.source)}` : '/designer');
    return opened;
  });
}
