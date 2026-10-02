# Family resemblance review — dapier vs dataops (dakit family spec)

**Verdict: PASS** (reviewed 2026-10-02 against `dakit/docs/family.md` with
dataops' rendered home as the canonical reference; three review rounds, the
latest recorded below).

## What landed

- The family shell: 268px sidebar (`--dk-size-sidebar`) on `--dk-bg-muted`
  with a right hairline, uppercase muted group labels, 32px icon+text nav
  rows, selected row = accent-soft fill + accent text with no rail or stripe;
  mobile ≤860px turns it into a modal drawer with scrim, Escape, focus trap,
  and focus restoration.
- The family page anatomy (cherry-picked from the judge-verified redesign
  branch, 61903ed..314f354): every page reads in the centered measure (56rem;
  70rem for wide registers) with the dataops header (32px semibold title +
  one-line muted description, primary actions right, one hairline under the
  block); Home is the strip-and-panels anatomy (segmented 4-segment status
  strip, neutral banded "Needs attention" panel with severity dots, bordered
  panels with muted header bands instead of a two-column dashboard);
  secondary sections (Hooks, API polls, Task usage, Incoming events, Failed
  runs) are the same banded panels; the quota editor follows the
  label-above-control form recipe with human month copy. A parallel
  session's interim Home-panels commit (10c8849) is subsumed by this pass.
- Designer status language aligned with the console: the save button is
  primary only when there is something to save, the On/Off control is a 34px
  switch, and the rail's state chips carry the family dot (muted for Off,
  warning for Draft/Edited). The designer canvas keeps its sanctioned 13.5px
  density exception; all chrome around it speaks the family.
- Console overlays rounded to the family overlay surface: `dialog` and the
  workflow popover use `--dk-radius-lg` + `--dk-shadow-overlay` (with
  matched head/foot corner bands); static containers stay at radius-md.
- Connection-test verdicts read as words ("OK — …" / "Failed — …", the
  credentials notice flips to its error state on failure) instead of ✓/✗
  dingbats, keeping one icon language.
- Vendored dakit refreshed to current dist (adds the `.dk-form-actions`
  form-footer primitive); the rebuilt designer bundle absorbs the same block.

## Verification (final round, 2026-10-02 evening)

- `make test`: 3747 passed, 42 subtests passed, 2 warnings (mini-racer
  deprecation only).
- `make designer-build` (tsc + vite) clean; `make designer-console` rebuild
  reproduces the committed bundle byte-for-byte apart from the refreshed
  dakit block — no source/bundle drift.
- Screenshots: 27 captures in `/tmp/dapier-family-verify/shots` — console
  Home / Workflows / Runs / Connections, the standalone designer, and the
  statically served dataops Home as the canonical reference; every surface
  at 1440×900 and 390×844, light and dark, plus open mobile drawers.
  Harness: stubbed-API static server (fixture data is placeholder content,
  not a design signal).
- Review: performed by the implementing agent viewing every capture against
  the family rubric (shell geometry, header scale, row rhythm, button
  hierarchy, status language, icon strokes, palette in both themes). No
  independent subagent runner was available in this session; the rubric and
  captures are listed so the review can be repeated. The previous rounds'
  findings (designer save-button weight, accent-tinted attention panel,
  missing h1 descriptions, mobile touch scale, strip density, missing
  severity dots) were re-checked and confirmed fixed in the captures.
  Checked side-by-side with dataops: desktop light and dark match on shell
  geometry, nav treatment, header anatomy, segmented summary strip, banded
  panels, row rhythm, button hierarchy, and dark palette; mobile matches on
  64px top bar, single-row strip, stacked labelled rows, and drawer
  treatment; the designer reads as family chrome around the dense canvas.

## Deliberate remainders

- Provider brand marks (Google/Slack/Zoom logos) stay full-color: family.md
  allows brand marks as the only non-token color.
- Machine values (timestamps, workflow ids, "Updated") stay mono; dataops
  uses sans — kept as dapier's identifier texture.
- The workflows list expresses On/Off with a switch control (it is the
  toggle, not a status readout); status reads come from "Latest run".
- Connections rows may show two row actions (Get token + Manage); token
  retrieval is a distinct operator task. Candidate for a follow-up menu.
- Mobile page header keeps the 22px title inside the top bar block (with
  description) instead of repeating it in the canvas — same anatomy, one
  header instance; the workflow-count metadata may wrap to two lines in the
  panel band on narrow phones.
- Dapier keeps global refresh / updated-at in the page header row rather
  than a separate slim global toolbar strip; with only two global controls
  the dedicated strip felt like chrome for its own sake.
- The public marketing callout keeps its accent left edge on the
  single-purpose public pages (no operator shell there).
