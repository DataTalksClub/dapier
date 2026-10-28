/* Dark mode: a purely presentational preference, so it lives only in the UI.
   The head boot script applies the stored choice before first paint; the
   account menu's switch (dakit shell.js) toggles it and persists it under
   "dakit-theme" (the shared design system's key); this module follows the
   OS preference until the operator picks a side. The designer app reads the same key (same origin), so its iframe
   follows the toggle via the storage event. */
const KEY = 'dakit-theme';

function storedTheme() {
  try {
    const theme = localStorage.getItem(KEY);
    return theme === 'dark' || theme === 'light' ? theme : null;
  } catch (_) {
    return null;
  }
}

/* With no explicit choice, keep tracking the OS setting while open, and
   keep the account menu's Dark mode switch (shell.js) in step. */
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (event) => {
  if (storedTheme()) return;
  document.documentElement.dataset.theme = event.matches ? 'dark' : 'light';
  document.querySelectorAll('[data-dk-theme-toggle]').forEach((toggle) => {
    toggle.setAttribute('aria-checked', String(event.matches));
  });
});
