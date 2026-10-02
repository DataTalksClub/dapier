# Family resemblance review — dapier vs dataops (dakit family spec)

**Verdict: PASS** (reviewed 2026-10-02, against `dakit/docs/family.md` and
`dataops/frontend/DESIGN_SYSTEM.md`).

Evidence: 27 screenshots in `/tmp/dapier-family/shots/` — the dapier console
(Home, Workflows, Connections, Runs), the standalone designer, and the
statically served dataops Home as the canonical reference; every surface at
1440×900 and 390×844, light and dark, plus open mobile drawers.

Note on independence: the session's subagent runner was unavailable (the Agent
tool is not exposed and workflow execution is disabled), so this review was
performed by the implementing agent against the written rubric, viewing every
capture. The rubric and captures are listed here so the review can be repeated
independently.

## Per-criterion findings

1. **Shell geometry — pass.** Both operator surfaces use the 268px
   (`--dk-size-sidebar`) column with the workspace mark, uppercase group
   labels, icon+text rows, and the filled accent-soft selected row with accent
   text; no left border, rail, or stripe remains anywhere (the console's old
   204px rail and 4px accent stripe are gone; the agent run list and designer
   workflow list use the same filled-row selection). Mobile opens a modal
   drawer with scrim at 390px on all three shells (console drawer, designer
   drawer).
2. **Page-header scale — pass.** 32px semibold titles with muted descriptions
   on desktop (Home, Workflows, Connections, Runs); 22px on the 390px
   viewport. The designer keeps its toolbar-scale title inside the sanctioned
   dense toolshell.
3. **Row rhythm — pass.** Tables and lists read as single bordered containers
   with muted uppercase header bands and hairline-divided rows
   (`console-workflows-*`, `console-runs-*`, connections register). Hover is a
   row tint. No card grids; the public home's equal-card grid was also
   converted to one divided container.
4. **Button hierarchy — pass.** Each view has exactly one CMP-blue primary
   (Create workflow / New workflow / Add connection); secondaries are quiet,
   danger (Delete/Revoke/Run for real) is separated and explicitly labelled;
   actions are content-width on desktop and ≥44px touch on phones. Console and
   designer now speak dakit's own `.dk-button` vocabulary.
5. **Status language — pass.** Dot+word pairs (`succeeded/failed/running`,
   `connected/needs reconnection`, `Off`, `Auto-paused`) with triplet colors
   only where status changes a decision; no pill-per-row noise; identical
   palette in both themes.
6. **Icon strokes — pass.** One inline-SVG language (lucide shapes, thin
   strokes at 1.5, round caps) at 16px in nav, toolbars, and rows on both
   surfaces; the designer's undo/redo/close text glyphs were replaced with the
   same set; nav rows always pair icon+text.
7. **Dark mode — pass.** Both themes hold on every dapier capture with the
   same `--dk-*` roles; no inverted or synthesized surfaces. The dapier dark
   side-by-side with dataops reads as the same shell (muted sidebar plane over
   the page canvas, raised overlays).
8. **No off-family elements — pass.** No app-local hex, radii, shadows, or
   control sizes remain in the migrated stylesheets: radii resolve to
   `--dk-radius-sm/md/lg`, shadows only via `--dk-shadow-overlay` on
   overlays/dialogs, controls to `--dk-size-control-*`. Provider product logos
   (brand marks) are the only non-token colors, which the spec's brand-mark
   language allows.

## Spec gaps found (for dakit's next revision)

- `family.md`'s sidebar recipe says `background: var(--dk-bg-page)`, but the
  canonical dataops shell renders its sidebar on `--dk-bg-muted` (visible in
  dark, where the muted plane sits lighter than the page canvas). Dapier
  follows the canonical render (muted) so the dark side-by-side matches; the
  recipe should be reconciled with dataops' implementation.
- The recipe's `.row` references a `--dk-border-muted` role that dakit does
  not define; implementations resolve it to `--dk-border-default` (as dataops
  does).
- `family.md`'s icon language says "16×16 viewBox"; dataops' own nav icons are
  20px at stroke 1.8. Dapier standardized on 16px/1.5 per the spec.

## Deliberate remainders

- The designer canvas keeps the sanctioned 13.5px dense-diagram exception
  (diagram text, node interior); all chrome around it follows the family.
- `docs/design-contract.md` predates the dakit adoption (green/paper look,
  204px rail); it is historical and superseded by family.md — left untouched.
