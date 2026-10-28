/* dakit account chrome (docs/shell.md §6): the account row at the foot of
   the sidebar opens an upward disclosure with identity, the Dark mode
   switch, the region, and Sign out. Theme persists under dakit-theme
   (shell.js; theme.js keeps following the OS until the operator picks);
   sign-out goes through /auth/logout after the designer leave prompt. */
import { $, $$ } from './ui.js';
import './theme.js';
import { confirmDesignerLeave } from './views/designer.js';

export function accountInitials(name) {
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  return parts.slice(0, 2).map((part) => part[0]).join('').toUpperCase();
}

export function displayNameFromUsername(username) {
  const raw = String(username || '').trim();
  if (!raw) return 'Account';
  const local = raw.includes('@') ? raw.slice(0, raw.indexOf('@')) : raw;
  const words = local.split(/[._-]+/).filter(Boolean);
  if (!words.length) return 'Account';
  return words.map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
}

/* A session subject (a Cognito sub UUID) is not a name; never print it. */
function isOpaqueId(value) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
    || /^[0-9a-f-]{20,}$/i.test(value);
}

/* /api/admin/me carries the ID token's name and the sign-in email. The
   chrome shows a person:
   the name, else one derived from the email, else a plain "Operator". */
export function accountIdentity(me) {
  const username = String((me && me.username) || '').trim();
  const email = String((me && me.email) || '').trim();
  let name = String((me && me.name) || '').trim();
  if (!name) {
    if (email) name = displayNameFromUsername(email);
    else if (username && !isOpaqueId(username)) name = displayNameFromUsername(username);
    else name = 'Operator';
  }
  return { name, email, initials: accountInitials(name) };
}

export function applyAccountIdentity(me) {
  const { name, email, initials: glyph } = accountIdentity(me);
  $$('[data-account-avatar]').forEach((node) => { node.textContent = glyph; });
  $$('[data-account-name]').forEach((node) => { node.textContent = name; });
  const trigger = $('#account-button');
  if (trigger) trigger.setAttribute('aria-label', `Account, ${name}`);
  const actorName = $('[data-account-actor-name]');
  const actorEmail = $('[data-account-actor-email]');
  if (actorName) actorName.textContent = name;
  if (actorEmail) {
    actorEmail.textContent = email;
    actorEmail.hidden = !email;
  }
}

/* The disclosure itself (open/close, Esc, outside click, focus, arrows) and
   the Dark mode switch are dakit's shell.js (data-dk-popover-trigger,
   data-dk-theme-toggle); the console adds only sign-out, which must pass
   the designer's unsaved-changes prompt first. */
export function bindAccountChrome() {
  $('#account-sign-out').addEventListener('click', async () => {
    globalThis.dakitShell?.closePopovers({ restoreFocus: false });
    if (await confirmDesignerLeave()) window.location.assign('/auth/logout');
  });
}
