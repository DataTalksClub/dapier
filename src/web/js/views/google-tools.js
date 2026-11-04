/* Console client for the same Google handlers used by dapier gmail/gdrive/calendar. */
import { api } from '../api.js';
import { escapeHtml } from '../format.js';

// Labels, routes and example parameters only; validation and writes belong to the API.
const OPERATIONS = {
  gmail: [
    ['List messages', 'GET', 'messages', {max: 25}],
    ['Search messages', 'GET', 'messages', {q: 'is:unread', max: 25}],
    ['Read message', 'GET', 'messages/{message_id}', {}],
    ['Download attachment', 'GET', 'messages/{message_id}/attachments/{attachment_id}', {}],
    ['List threads', 'GET', 'threads', {max: 25}],
    ['Read thread', 'GET', 'threads/{thread_id}', {}],
    ['List labels', 'GET', 'labels', {}],
    ['Change labels or status', 'POST', 'messages/{message_id}/modify', {mark_read: true}],
    ['Create draft', 'POST', 'drafts', {to: '', subject: '', text: ''}],
    ['Update draft', 'PUT', 'drafts/{draft_id}', {text: ''}],
    ['List drafts', 'GET', 'drafts', {max: 25}],
    ['Read draft', 'GET', 'drafts/{draft_id}', {}],
    ['Delete draft', 'DELETE', 'drafts/{draft_id}', {}],
    ['Send draft', 'POST', 'drafts/{draft_id}/send', {}],
    ['Send email', 'POST', 'send', {to: '', subject: '', text: ''}],
    ['Reply directly', 'POST', 'reply', {message_id: '', text: '', reply_all: false}],
    ['Create reply draft', 'POST', 'reply', {message_id: '', text: '', reply_all: false, draft: true}],
  ],
  gdrive: [
    ['Search or list files', 'GET', 'files', {max: 25}],
    ['File metadata', 'GET', 'files/{file_id}', {}],
    ['Download or export', 'GET', 'files/{file_id}/content', {}],
    ['Upload file', 'POST', 'files', {name: ''}],
    ['Update file', 'PATCH', 'files/{file_id}', {name: ''}],
    ['Create folder', 'POST', 'folders', {name: ''}],
    ['Copy file', 'POST', 'files/{file_id}/copy', {name: ''}],
    ['Move file', 'POST', 'files/{file_id}/move', {add_parent: '', remove_parent: ''}],
    ['Share file', 'POST', 'files/{file_id}/share', {role: 'reader', share_type: 'user', email_address: ''}],
    ['Trash file', 'POST', 'files/{file_id}/trash', {permanent: false}],
    ['Restore file', 'POST', 'files/{file_id}/restore', {}],
  ],
  calendar: [
    ['List calendars', 'GET', 'calendars', {max: 25}],
    ['Search or list events', 'GET', 'events', {time_min: '', time_max: '', max: 25}],
    ['Read event', 'GET', 'events/{event_id}', {}],
    ['Free / busy', 'POST', 'freebusy', {calendar_ids: ['primary'], time_min: '', time_max: ''}],
    ['Create event', 'POST', 'events', {summary: '', start: '', end: '', send_updates: 'none'}],
    ['Update event', 'PATCH', 'events/{event_id}', {summary: '', send_updates: 'none'}],
    ['Delete event', 'DELETE', 'events/{event_id}', {send_updates: 'none'}],
  ],
};

function bytes64(bytes) {
  let text = '';
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text);
}

export function openGoogleTools(connection) {
  document.getElementById('google-tools-dialog')?.remove();
  const dialog = document.createElement('dialog');
  dialog.id = 'google-tools-dialog';
  dialog.className = 'dk-dialog';
  dialog.setAttribute('aria-labelledby', 'google-tools-title');
  dialog.innerHTML = `<div class="dialog-head dk-dialog__head"><h2 id="google-tools-title">Google tools</h2></div>
    <div class="dialog-body dk-dialog__body">
      <p>${escapeHtml(connection.verified_account_id || connection.connection_id)}</p>
      <label>Service<select name="service"><option value="gmail">Gmail</option><option value="gdrive">Drive</option><option value="calendar">Calendar</option></select></label>
      <label>Operation<select name="operation"></select></label>
      <div data-ids></div>
      <label>Parameters (JSON)<textarea name="parameters" rows="9" spellcheck="false"></textarea></label>
      <label data-files>Files<input name="files" type="file" multiple></label>
      <p class="sub">Use Preview to review parameters. Apply runs the selected operation on this account. Calendar notifications default to none; choose send_updates explicitly. List responses include next_page_token; pass it as page_token to continue.</p>
      <p class="form-error" role="alert"></p><pre data-result></pre>
    </div>
    <div class="dialog-actions dk-dialog__foot"><button class="dk-button dk-button--secondary" data-close>Close</button><button class="dk-button dk-button--secondary" data-preview>Preview</button><button class="dk-button dk-button--primary" data-run>Fetch</button></div>`;
  document.body.append(dialog);
  const find = (selector) => dialog.querySelector(selector);
  const service = find('[name=service]');
  const operation = find('[name=operation]');
  const parameters = find('[name=parameters]');
  const result = find('[data-result]');
  const error = find('[role=alert]');
  const run = find('[data-run]');
  const selected = () => OPERATIONS[service.value][Number(operation.value)];
  function chooseOperation() {
    const [, method, path, example] = selected();
    parameters.value = JSON.stringify(example, null, 2);
    find('[data-ids]').innerHTML = [...path.matchAll(/\{(\w+)\}/g)].map((m) =>
      `<label>${escapeHtml(m[1].replaceAll('_', ' '))}<input data-key="${m[1]}" required></label>`).join('');
    find('[name=files]').value = '';
    find('[data-files]').hidden = !(service.value === 'gmail' && ['send', 'reply', 'drafts', 'drafts/{draft_id}'].includes(path) && method !== 'GET')
      && !(service.value === 'gdrive' && ['POST', 'PATCH'].includes(method) && ['files', 'files/{file_id}'].includes(path));
    run.textContent = method === 'GET' || path === 'freebusy' ? 'Fetch' : 'Apply';
    result.textContent = '';
    error.textContent = '';
  }
  function chooseService() {
    operation.innerHTML = OPERATIONS[service.value].map(([label], i) => `<option value="${i}">${escapeHtml(label)}</option>`).join('');
    chooseOperation();
  }
  async function request() {
    const [, method, template] = selected();
    let path = template;
    for (const input of dialog.querySelectorAll('[data-key]')) {
      if (!input.reportValidity()) throw new Error('Fill in the resource ID.');
      path = path.replace(`{${input.dataset.key}}`, encodeURIComponent(input.value));
    }
    const fields = JSON.parse(parameters.value);
    if (!fields || typeof fields !== 'object' || Array.isArray(fields)) throw new Error('Parameters must be a JSON object.');
    const body = {...fields, account: connection.connection_id};
    if (!find('[data-files]').hidden) {
      const files = Array.from(find('[name=files]').files);
      if (service.value === 'gmail' && files.length) {
        body.attachments = await Promise.all(files.map(async (f) => ({filename: f.name, mime_type: f.type,
          content_b64: bytes64(new Uint8Array(await f.arrayBuffer()))})));
      } else if (service.value === 'gdrive' && files.length) {
        if (files.length !== 1) throw new Error('Choose one Drive file.');
        body.content_b64 = bytes64(new Uint8Array(await files[0].arrayBuffer()));
        body.mime_type = files[0].type || 'application/octet-stream';
      }
    }
    const url = `/api/admin/${service.value}/${path}`;
    return method === 'GET' || method === 'DELETE'
      ? {method, url: `${url}?${new URLSearchParams(body)}`, ...(method === 'DELETE' ? {body} : {})}
      : {method, url, body};
  }
  async function execute(preview) {
    error.textContent = '';
    try {
      const req = await request();
      if (preview) {
        const {content_b64, attachments, ...fields} = req.body || {};
        result.textContent = JSON.stringify({...req, body: req.body ? {...fields,
          ...(content_b64 !== undefined ? {content_b64: '(file bytes)'} : {}),
          ...(attachments ? {attachments: attachments.map(({content_b64: _bytes, ...a}) => a)} : {})} : undefined}, null, 2);
        return;
      }
      run.disabled = true;
      const data = await api(req.url, {method: req.method, ...(req.body ? {body: JSON.stringify(req.body)} : {})});
      if (data.data_b64 !== undefined) {
        const binary = atob(data.data_b64.replaceAll('-', '+').replaceAll('_', '/'));
        const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
        const url = URL.createObjectURL(new Blob([bytes], {type: data.mime_type || 'application/octet-stream'}));
        const link = document.createElement('a');
        link.href = url;
        link.download = (data.name || 'attachment').split(/[\\/]/).pop();
        link.textContent = 'Download file';
        result.replaceChildren(link);
        link.addEventListener('click', () => setTimeout(() => URL.revokeObjectURL(url), 1000), {once: true});
      } else result.textContent = JSON.stringify(data, null, 2);
    } catch (exc) { error.textContent = exc.message; }
    finally { run.disabled = false; }
  }
  service.addEventListener('change', chooseService);
  operation.addEventListener('change', chooseOperation);
  find('[data-preview]').addEventListener('click', () => execute(true));
  run.addEventListener('click', () => execute(false));
  find('[data-close]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => dialog.remove());
  chooseService();
  dialog.showModal();
}
