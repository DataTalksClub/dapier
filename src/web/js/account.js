/* Family account chrome: identity, appearance, and sign-out live in a
   sidebar-footer popover. Theme still persists under dakit-theme; sign-out
   still goes through /auth/logout after the designer leave prompt. */
import { $, $$ } from './ui.js';
import { currentTheme, toggleTheme } from './theme.js';
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

function emailFromUsername(username) {
  const raw = String(username || '').trim();
  return raw.includes('@') ? raw : '';
}

export function syncThemeToggle() {
  const button = $('#account-theme-toggle');
  if (!button) return;
  const dark = currentTheme() === 'dark';
  const label = button.querySelector('.account-theme-label');
  button.setAttribute('aria-pressed', String(dark));
  button.title = dark ? 'Switch to light mode' : 'Switch to dark mode';
  button.setAttribute('aria-label', button.title);
  if (label) label.textContent = dark ? 'Light mode' : 'Dark mode';
}

export function applyAccountIdentity(me) {
  const username = me && me.username;
  const name = displayNameFromUsername(username);
  const email = emailFromUsername(username);
  const glyph = accountInitials(name);
  $$('[data-account-avatar]').forEach((node) => { node.textContent = glyph; });
  $$('[data-account-name]').forEach((node) => { node.textContent = name; });
  const actorName = $('[data-account-actor-name]');
  const actorAvatar = $('[data-account-actor-avatar]');
  const actorEmail = $('[data-account-actor-email]');
  if (actorName) actorName.textContent = name;
  if (actorAvatar) actorAvatar.textContent = glyph;
  if (actorEmail) {
    actorEmail.textContent = email;
    actorEmail.hidden = !email;
  }
}

export function bindAccountChrome() {
  const buttons = $$('.account-button');
  const menu = $('#account-menu');
  const close = $('#account-menu-close');
  const theme = $('#account-theme-toggle');
  const signOut = $('#account-sign-out');
  let opener = null;

  function isOpen() {
    return Boolean(menu) && !menu.hidden;
  }

  function closeMenu() {
    if (!menu || menu.hidden) return;
    menu.hidden = true;
    for (const trigger of buttons) trigger.setAttribute('aria-expanded', 'false');
    if (opener && opener.isConnected) opener.focus();
    opener = null;
  }

  function openMenu() {
    opener = document.activeElement instanceof HTMLElement ? document.activeElement : buttons[0];
    syncThemeToggle();
    menu.hidden = false;
    for (const trigger of buttons) trigger.setAttribute('aria-expanded', 'true');
    close.focus();
  }

  for (const trigger of buttons) {
    trigger.addEventListener('click', () => { if (isOpen()) closeMenu(); else openMenu(); });
  }
  close.addEventListener('click', closeMenu);
  theme.addEventListener('click', (event) => {
    event.stopPropagation();
    toggleTheme();
    syncThemeToggle();
  });
  signOut.addEventListener('click', async () => {
    if (await confirmDesignerLeave()) window.location.assign('/auth/logout');
  });
  document.addEventListener('click', (event) => {
    if (!isOpen()) return;
    if (menu.contains(event.target) || buttons.some((trigger) => trigger.contains(event.target))) return;
    closeMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !isOpen()) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    closeMenu();
  }, true);
  syncThemeToggle();
  return { closeMenu, isOpen };
}
