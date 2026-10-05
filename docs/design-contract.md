# Dapier console — design contract

Dapier renders the shared DataTalksClub family design. The source of truth is
the **dakit** design system (`../dakit`): `docs/family.md` there is the rubric,
and dataops' rendered UI is the canonical reference. When this document and
dakit disagree, dakit wins and this file gets updated. (The pre-family
"operations register" contract this file once carried is retired; it described
a direction dapier deliberately left on 2026-10-02 when it joined the family.)

## How the family is consumed

- The console serves a **vendored copy** of the built bundle:
  `src/web/vendor/dakit.css` + `src/web/vendor/dakit-tokens.css` (+ fonts).
  Never link `../dakit` from the app. After changing dakit, run
  `make sync-dakit` and commit the refreshed vendor files; the copies must
  stay byte-identical to `dakit/dist`.
- All app CSS (`src/web/app.css`, `designer.css`, `public.css`, `device.css`)
  is written against `--dk-*` tokens only. No app-local hex values, radii, or
  shadows. The sanctioned exceptions are provider brand marks
  (Google/Slack/Zoom logos, full color) and the dapier remap of the token
  values in `designer.css`/`app.css`, which must match dakit's own light/dark
  values exactly.
- The designer is the family's sanctioned dense toolshell: same shell anatomy
  and tokens; the canvas may drop below the console rhythm (13.5px, the one
  density exception family.md grants dapier).

## Pinned recipes (verify against dakit/docs/family.md)

- **Shell**: 268px sidebar (`--dk-size-sidebar`) on `--dk-bg-muted` with a
  right hairline; uppercase muted group labels; icon+text nav rows; the
  selected page is the filled `--dk-accent-soft` row only — no rail, no
  stripe. Mobile ≤860px: one top bar + modal drawer (scrim, Escape, focus
  trap, restoration).
- **Page anatomy**: one 32px `h1` + one-line muted description; primary
  actions in the header band; content in one bordered container with muted
  header bands and hairline-divided rows; Home is the strip-and-panels
  anatomy, not a dashboard grid.
- **Controls**: `--dk-size-control-*` heights (34px desktop, 44px touch);
  `--dk-radius-md` (6px) static, `--dk-radius-lg` (10px) + overlay shadow for
  modal/popover surfaces; one CMP-blue primary per view; destructive actions
  quiet and separated; labels above inputs with the `.dk-form-actions` footer.
- **Focus**: the family recipe, never an app-local variant —
  `outline: 3px solid var(--dk-focus-ring)` at 2px offset on `:focus-visible`
  (full-bleed rows inset it), and text controls that swap to the accent border
  with a `0 0 0 3px` halo on `:focus`. App CSS that mirrors the base layer
  must track `dakit/css/base.css` byte-for-byte in spirit: when the vendored
  bundle updates the recipe, the mirrors update in the same commit.
- **Icons**: one language — inline SVG, 24×24 viewBox paths rendered at 20px,
  `stroke="currentColor"`, stroke-width 1.8, round caps/joins, no fills.
  The shared shapes copy dakit's canonical set (`showcase.html`); per-app
  shapes stay on the same grid. No icon fonts, no emoji, no 16-grid strokes.
- **Status**: dot+word pairs in token colors; triplet badges only where status
  changes a decision; machine values (ids, timestamps) in IBM Plex Mono.

## Deliberate departures (recorded, not drift)

- Machine values (workflow ids, timestamps, "Updated") stay mono — dapier's
  identifier texture; dataops uses sans for the same slots.
- The workflows list expresses On/Off with a switch (it is the toggle, not a
  status readout); run status comes from the dot+word "Latest run" read.
- Connections rows carry Manage (and Finish setup / Reconnect when the
  grant needs it). Provider access tokens stay on the CLI
  (`dapier token exec|write`); the console does not reveal them.
  Treatments live in [`connections-page.md`](connections-page.md).
- The standalone designer's footer shows the working-copy git state
  (branch + clean/dirty). It is local tool chrome for the operator who
  commits workflows, not a family-shell element.

## Banned tells

Any app-local focus width/halo; icons off the 24-grid geometry; raw hex
outside the token remap and provider marks; gradients; pastel status pills;
eyebrow micro-labels above content headings; icons inside labelled buttons;
dark sidebar against a light canvas; dashboard card grids; off-family brand
marks (the app identity is the accent rounded square with the bold "D" —
the favicon and public pages carry the same tile).

## Verification

Render the real HTML/CSS/JS through the stubbed-API static server and
capture every console page plus the designer at 1440×900 and 390×844, light
and dark, with dataops' home as the side-by-side reference; include the
mobile drawers, a management dialog, and keyboard focus states. An
independent judge compares against `../dakit/docs/family.md`; rounds and
verdicts are recorded in `docs/family-review.md`. Functional smoke:
navigation, picker, dialogs, grant save/revoke, token revoke, browser
console errors.
