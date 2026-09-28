/* Templates view: the gallery of managed workflows flagged template:true —
   the same rows `dapier templates list` prints, over
   /api/admin/designer/templates. "Use template" forks one into a new
   workflow (the template stays); publishing and unpublishing live on the
   workflow rows under Workflows and on the gallery rows here. */
import { state } from '../state.js';
import { $, notice } from '../ui.js';
import { api } from '../api.js';
import { escapeHtml, statusLine } from '../format.js';
import { refresh } from './overview.js';
import { openDesigner } from './designer.js';

let templates = null;
let fetching = false;

function templateTrigger(template) {
  const base = `${template.connector || '?'} · ${template.event || '?'}`;
  const extra = (template.triggerCount || 1) - 1;
  return extra > 0 ? `${base} +${extra} more` : base;
}

function templateRow(template) {
  const id = escapeHtml(template.id);
  const file = escapeHtml(template.source || '');
  const description = template.description
    ? `<div class="cell-tags">${escapeHtml(template.description)}</div>` : '';
  /* Everything in the gallery is a published, live workflow — the flag only
     decides gallery membership — so the state column says so. */
  return `<tr>
    <td class="cell-title"><span class="cell-name mono">${id}</span>${description}</td>
    <td class="mono muted-cell" data-label="Trigger">${escapeHtml(templateTrigger(template))}</td>
    <td class="mono muted-cell" data-label="Steps">${Number(template.actionCount) || 0}</td>
    <td class="mono muted-cell" data-label="Source">${file || '—'}</td>
    <td data-label="State">${statusLine('active', { active: 'Published' })}</td>
    <td class="action-cell" data-label="Manage">
      <button type="button" class="button primary template-use" data-file="${file}" data-workflow="${id}">Use template</button>
      <button type="button" class="button secondary template-unpublish" data-file="${file}" data-workflow="${id}" title="Remove from the Templates gallery">Unpublish</button>
    </td>
  </tr>`;
}

function renderRows() {
  const list = templates || [];
  $('#template-table').innerHTML = list.map(templateRow).join('');
  $('#template-empty').hidden = list.length > 0;
  $('#template-table-wrap').hidden = list.length === 0;
}

export async function fetchTemplates() {
  if (fetching) return;
  fetching = true;
  try {
    const data = await api('/api/admin/designer/templates');
    templates = data.templates || [];
  } catch (error) {
    notice(error.message, true);
  } finally {
    fetching = false;
  }
  renderRows();
}

function openApplyDialog(button) {
  const dialog = $('#template-apply-dialog');
  $('#template-apply-title').textContent = `Use template — ${button.dataset.workflow}`;
  $('#template-apply-blurb').textContent = `Fork ${button.dataset.file} into a new workflow. The template itself stays in the gallery.`;
  $('#template-apply-name').value = '';
  $('#template-apply-error').hidden = true;
  dialog.dataset.file = button.dataset.file;
  dialog.showModal();
}

$('#template-table').addEventListener('click', async (event) => {
  const use = event.target.closest('.template-use');
  if (use) {
    if (!use.dataset.file) return;
    openApplyDialog(use);
    return;
  }
  const unpublish = event.target.closest('.template-unpublish');
  if (!unpublish || unpublish.disabled || !unpublish.dataset.file) return;
  unpublish.disabled = true;
  try {
    const result = await api(`/api/admin/designer/workflows/${encodeURIComponent(unpublish.dataset.file)}/template`, {
      method: 'PUT',
      body: JSON.stringify({ template: false }),
    });
    const workflowId = result.workflow_id || unpublish.dataset.workflow;
    notice(result.git_sync_error
      ? `${workflowId} was removed from the gallery, but Git sync failed: ${result.git_sync_error}`
      : `${workflowId} removed from the template gallery.`, !!result.git_sync_error);
    await refresh(); // the Workflows list shows the flag on each row
    await fetchTemplates();
  } catch (error) {
    notice(error.message, true);
    unpublish.disabled = false;
  }
});

$('#template-apply-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const dialog = $('#template-apply-dialog');
  const file = dialog.dataset.file;
  const use = $('#template-apply-save');
  const error = $('#template-apply-error');
  const name = $('#template-apply-name').value.trim();
  use.disabled = true;
  try {
    const data = await api(`/api/admin/designer/templates/${encodeURIComponent(file)}/apply`, {
      method: 'POST',
      body: JSON.stringify(name ? { name } : {}),
    });
    dialog.close();
    const newId = String(data.file || file).replace(/\.yaml$/, '');
    notice(`Applied ${file} as ${newId}${data.published ? ' — live now' : ''}.`);
    await refresh(); // the copy is an ordinary workflow now; the list shows it
    void openDesigner(newId);
  } catch (caught) {
    error.textContent = caught.message;
    error.hidden = false;
  } finally {
    use.disabled = false;
  }
});

/* Entering the view (nav click, back/forward, deep link) fetches the fresh
   gallery; main.js handles the deep-link call on first load. */
document.addEventListener('click', (event) => {
  if (event.target.closest('.nav-item[data-view="templates"]')) void fetchTemplates();
});
window.addEventListener('popstate', () => {
  if (state.view === 'templates') void fetchTemplates();
});
