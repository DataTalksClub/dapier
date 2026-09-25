/* Dark mode: a purely presentational preference, so it lives only in the UI.
   The head boot script applies the stored choice before first paint; this
   module toggles it, persists it under "dapier-theme", and follows the OS
   preference until the operator picks a side. The designer app reads the same
   key (same origin), so its iframe follows the toggle via the storage event. */
const KEY = 'dapier-theme';

function storedTheme() {
  try {
    const theme = localStorage.getItem(KEY);
    return theme === 'dark' || theme === 'light' ? theme : null;
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
