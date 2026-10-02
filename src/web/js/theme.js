/* Dark mode: a purely presentational preference, so it lives only in the UI.
   The head boot script applies the stored choice before first paint; this
   module toggles it, persists it under "dakit-theme" (the shared design
   system's key), and follows the OS preference until the operator picks a
   side. The designer app reads the same key (same origin), so its iframe
   follows the toggle via the storage event. */
const KEY = 'dakit-theme';
const LEGACY_KEY = 'dapier-theme';

function storedTheme() {
  try {
    const theme = localStorage.getItem(KEY);
    if (theme === 'dark' || theme === 'light') return theme;
    /* Pre-dakit choice: honored until the operator toggles again. */
    const legacy = localStorage.getItem(LEGACY_KEY);
    return legacy === 'dark' || legacy === 'light' ? legacy : null;
  } catch (_) {
    return null;
  }
}

export function currentTheme() {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
}

export function toggleTheme() {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem(KEY, next); } catch (_) { /* private mode: theme applies for the session only */ }
}

/* With no explicit choice, keep tracking the OS setting while open. */
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (event) => {
  if (!storedTheme()) document.documentElement.dataset.theme = event.matches ? 'dark' : 'light';
});
