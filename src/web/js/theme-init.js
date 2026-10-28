/* Apply the stored theme before first paint. The console CSP allows scripts
   from this origin, so this boot code lives in a separate asset. The choice
   lives under the shared design system's key ("dakit-theme"). */
(function () {
  var theme = null;
  try { theme = localStorage.getItem('dakit-theme'); } catch (_) {}
  if (theme !== 'dark' && theme !== 'light') {
    theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  var forced = new URLSearchParams(location.search).get('theme');
  if (forced === 'dark' || forced === 'light') theme = forced;
  document.documentElement.dataset.theme = theme;
})();
