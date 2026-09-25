/* Designer view: hosts the designer app shell (/designer/app) in an iframe,
   so Edit-in-designer and New workflow open the canvas inside the console. */
import { $ } from '../ui.js';
import { setView } from '../router.js';

function frameSrc(source) {
  const params = new URLSearchParams({ embed: '1' });
  if (source) params.set('workflow', source);
  return `/designer/app?${params}`;
}

/* Opens the designer for `source` (a workflow filename, or null for a new
   workflow) and leaves /designer?workflow=<source> as the shareable URL. */
export function openDesigner(source, push = true) {
  const frame = $('#designer-frame');
  const src = frameSrc(source);
  if (frame.getAttribute('src') !== src) frame.setAttribute('src', src);
  setView('designer', false);
  const url = source ? `/designer?workflow=${encodeURIComponent(source)}` : '/designer';
  if (push) history.pushState(null, '', url);
}

/* Re-syncs the frame after a full-page load of, or popstate to, /designer. */
export function designerFromLocation() {
  openDesigner(new URLSearchParams(window.location.search).get('workflow'), false);
}
