/* The one fetch client: same-origin credentials, JSON bodies, auth screens. */
import { showLogin, showForbidden } from './ui.js';

export async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'content-type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (response.status === 401) {
    showLogin();
    throw new Error(body.error || 'Authentication required');
  }
  if (response.status === 403) {
    showForbidden(body.error || 'Your account is not an operator for this console.');
    throw new Error(body.error || 'Not authorized');
  }
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
