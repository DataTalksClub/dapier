# vendor/

Vendored copy of the **dakit** design system (`../dakit` in your checkout),
written by dakit's own `sync.mjs` (`make sync-dakit`) and pinned by
`dakit.lock.json` (version, source commit, SHA-256 of every file):

- `dakit.css` — the built bundle (tokens, base styles, components); its
  `@font-face` URLs point at `./fonts/` (self-hosted Inter and IBM Plex Mono,
  SIL OFL).
- `shell.js` — app shell behaviour (sidebar collapse/rail, mobile drawer,
  account popover, theme switch, copy buttons).
- `split.js` — list–detail split (`.dk-split`): row keyboard, Back focus.
- `dialogs.js`, `tabs.js`, `collection.js` — dialog dismissal, segmented
  control keyboard, collection-header create button and `c` shortcut.
- `device-code.js` — ES module: device-login code normalising/formatting.
- `check-borders.mjs`, `check-colors.mjs`, `check-scale.mjs`, `sync.mjs` —
  the checkers `make check-dakit` / `make test` run, and the lock verifier.

Regenerate with `make sync-dakit` after changing dakit, rebuild the designer
bundle (`make designer-console`, it inlines `dakit.css`), and commit the
result — deploys build from this repo only. The copies are served verbatim
(`/assets/vendor/…`); edit dakit, never these files.
