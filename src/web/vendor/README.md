# vendor/

Vendored copies of the **dakit** design system (`../dakit` in your checkout):
`dakit.css` is its built bundle (tokens, base styles, components),
`dakit-dialogs.js` is the backdrop-click dismissal helper (load once per
page), and `fonts/` the self-hosted Inter and IBM Plex Mono faces it
references (SIL OFL).

Regenerate with `make sync-dakit` after changing dakit, and commit the
result — deploys build from this repo only. The copies are served verbatim
(`/assets/vendor/…`); edit dakit, never these files.
