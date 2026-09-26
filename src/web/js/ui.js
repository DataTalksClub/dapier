/* DOM helpers, screens, notices, and brand marks. */
export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

export function icons() {
  if (window.lucide) window.lucide.createIcons({ attrs: { 'stroke-width': 1.8 } });
}

export function showLogin() {
  const destination = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  window.location.assign(`/auth/login?next=${encodeURIComponent(destination)}`);
}

export function showForbidden(message = 'Your account is not an operator for this console.') {
  $$('dialog[open]').forEach((dialog) => dialog.close());
  $('#app').hidden = true;
  $('#startup-loading').hidden = true;
  $('#startup-error').hidden = true;
  $('#forbidden-view').hidden = false;
  $('#forbidden-message').textContent = message;
}

export function showApp() {
  $('#startup-loading').hidden = true;
  $('#startup-error').hidden = true;
  $('#app').hidden = false;
  icons();
}

export function showStartupError(message) {
  $$('dialog[open]').forEach((dialog) => dialog.close());
  $('#app').hidden = true;
  $('#startup-loading').hidden = true;
  $('#startup-error-message').textContent = message || 'The console could not reach its API. Check your connection and try again.';
  $('#startup-error').hidden = false;
  $('#startup-retry').focus();
}

export function notice(message, error = false) {
  const element = $('#notice');
  element.textContent = message;
  element.classList.toggle('error', error);
  element.hidden = false;
  setTimeout(() => { element.hidden = true; }, 4500);
}

/* Canonical brand glyphs (same simple-icons paths the designer palette uses)
   so cards and rows are identifiable by product mark, not just text. */
const PROVIDER_MARKS = {
  zoom: '<svg class="brand-mark" width="16" height="16" viewBox="0 0 24 24" role="img" aria-label="Zoom"><rect width="24" height="24" rx="5" fill="#2D8CFF"/><path d="M5 8h10v8H5zM16 10l4-2v8l-4-2z" fill="#fff"/></svg>',
  slack: '<svg class="brand-mark" width="16" height="16" viewBox="0 0 24 24" role="img" aria-label="Slack"><path fill="#E01E5A" d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313z"/><path fill="#36C5F0" d="M8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.527 2.527 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312z"/><path fill="#2EB67D" d="M18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312z"/><path fill="#ECB22E" d="M15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/></svg>',
  google: '<svg class="brand-mark" width="16" height="16" viewBox="0 0 24 24" role="img" aria-label="Google"><path fill="#4285F4" d="M23.49 12.27c0-.79-.07-1.54-.19-2.27H12v4.51h6.47c-.29 1.48-1.14 2.73-2.4 3.58v3h3.86c2.26-2.09 3.56-5.17 3.56-8.82z"/><path fill="#34A853" d="M12 24c3.24 0 5.95-1.08 7.93-2.91l-3.86-3c-1.08.72-2.45 1.16-4.07 1.16-3.13 0-5.78-2.11-6.73-4.96H1.29v3.09C3.26 21.3 7.31 24 12 24z"/><path fill="#FBBC05" d="M5.27 14.29c-.25-.72-.38-1.49-.38-2.29s.14-1.57.38-2.29V6.62H1.29C.47 8.24 0 10.06 0 12s.47 3.76 1.29 5.38l3.98-3.09z"/><path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.42-3.42C17.95 1.19 15.24 0 12 0 7.31 0 3.26 2.7 1.29 6.62l3.98 3.09C6.22 6.86 8.87 4.75 12 4.75z"/></svg>',
  youtube: '<svg class="brand-mark" width="16" height="16" viewBox="0 0 24 24" role="img" aria-label="YouTube"><path fill="#FF0000" d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/></svg>',
  dropbox: '<svg class="brand-mark" width="16" height="16" viewBox="0 0 24 24" role="img" aria-label="Dropbox"><path fill="#0061FF" d="M6 1.807 0 5.629l6 3.822 6.001-3.822L6 1.807zM18 1.807l-6 3.822 6 3.822 6-3.822-6-3.822zM0 13.274l6 3.822 6.001-3.822L6 9.452l-6 3.822zM18 9.452l-6 3.822 6 3.822 6-3.822-6-3.822zM6 18.371l6.001 3.822 6-3.822-6-3.822L6 18.371z"/></svg>',
  telegram: '<svg class="brand-mark" width="16" height="16" viewBox="0 0 24 24" role="img" aria-label="Telegram"><path fill="#26A5E4" d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/></svg>',
};

export function providerMark(provider) {
  return PROVIDER_MARKS[provider] || '';
}

/* Secret fields render masked bullets without being real password inputs where
   the engine can draw them itself (-webkit-text-security): the browser's
   password manager never sees a password field, so it neither autofills saved
   credentials nor offers to save the pasted token. Copy, cut, and drag out of
   a secret field are blocked so a pasted token can't leave it. */
export function hardenSecretInputs(root = document) {
  $$('.secret-input', root).forEach((input) => {
    if (input.type === 'password' && CSS.supports('-webkit-text-security', 'disc')) {
      input.type = 'text';
      input.classList.add('masked-input');
      input.setAttribute('autocomplete', 'off');
    } else {
      input.setAttribute('autocomplete', 'new-password');
    }
  });
}
