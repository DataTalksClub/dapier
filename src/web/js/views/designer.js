/* Designer view: hosts the designer app shell (/designer/app) in an iframe.
   This is the console's workflow view — every workflow row opens it — so the
   section head names the open workflow and links to it on GitHub. */
import { state } from '../state.js';
import { $ } from '../ui.js';
import { setView } from '../router.js';

function frameSrc(source) {
  const params = new URLSearchParams({ embed: '1' });
  if (source) params.set('workflow', source);
  return `/designer/app?${params}`;
}

function syncHead(source) {
  const workflow = source
    ? (state.data?.workflows || []).find((item) => item.source === source)
    : null;
  $('#designer-title').textContent = workflow ? workflow.id : source ? source.replace(/\.yaml$/, '') : 'Workflow designer';
  const github = $('#designer-github');
  if (workflow && state.data.workflows_edit_base) {
    github.href = `${state.data.workflows_edit_base}/${encodeURIComponent(workflow.source)}`;
    github.hidden = false;
  } else {
    github.hidden = true;
  }
}

/* Opens the designer for `source` (a workflow filename, or null for a new
   workflow) and leaves /designer?workflow=<source> as the shareable URL. */
export function openDesigner(source, push = true) {
  const frame = $('#designer-frame');
  const src = frameSrc(source);
  if (frame.getAttribute('src') !== src) frame.setAttribute('src', src);
  setView('designer', false);
  syncHead(source);
  const url = source ? `/designer?workflow=${encodeURIComponent(source)}` : '/designer';
  if (push) history.pushState(null, '', url);
}

/* Re-syncs the frame after a full-page load of, or popstate to, /designer. */
export function designerFromLocation() {
  openDesigner(new URLSearchParams(window.location.search).get('workflow'), false);
}
