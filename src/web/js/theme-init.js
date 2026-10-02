/* Apply the stored theme before first paint. The console CSP allows scripts
   from this origin, so this boot code lives in a separate asset. The choice
   lives under the shared design system's key ("dakit-theme"); the pre-dakit
   "dapier-theme" key is still honored so an operator's pick survives the
   migration (theme.js writes only the new key from now on). */
(function () {
  var theme = null;
  try { theme = localStorage.getItem('dakit-theme'); } catch (_) {}
  if (theme !== 'dark' && theme !== 'light') {
    try { theme = localStorage.getItem('dapier-theme'); } catch (_) {}
  }
  if (theme !== 'dark' && theme !== 'light') {
    theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  var forced = new URLSearchParams(location.search).get('theme');
  if (forced === 'dark' || forced === 'light') theme = forced;
  document.documentElement.dataset.theme = theme;
})();
