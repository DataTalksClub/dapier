# Family resemblance review — dapier vs dataops (dakit family spec)

**Verdict: PASS** (reviewed 2026-10-02, judge-verified in three rounds against
`dakit/docs/family.md` with dataops' rendered home as the canonical reference).

## What this branch carries

- The six family-adoption commits that previously sat unpushed on the old
  main (family shell + dk-button vocabulary, designer chrome, dakit console
  class assertions, flow-label wrapping, copilot removal, canonical sidebar
  plane) were re-based onto current `origin/main` via cherry-picks, keeping
  the parallel sessions' newer work (hook-backed read-only workflows,
  checkbox geometry, connection scopes cleanup, vendored dakit 11ae542).
- A deeper anatomy pass to close the remaining family gaps: every page reads
  in the family's centered measure (56rem; 70rem for wide registers) with
  the dataops header anatomy (32px semibold title + one-line muted
  description, primary actions right, one hairline under the block; filter
  bars stay in the content column); the home page is the family's
  strip-and-panels anatomy (segmented 4-segment status strip, neutral
  banded "Needs attention" panel with severity dots, bordered panels with
  muted header bands instead of a two-column dashboard); secondary sections
  (Hooks, API polls, Task usage, Incoming events, Failed runs) became the
  same banded panels; the quota editor follows the label-above-control form
  recipe with human month copy.
- Designer status language aligned with the console: the save button is
  primary only when there is something to save, the On/Off control is a
  34px switch, and the rail's state chips carry the family dot (muted for
  Off, warning for Draft/Edited).

## Verification

3747 pytest tests + 42 subtests green; `tsc` clean; both designer bundles
rebuilt from source. Screenshots: 27 captures — console Home / Workflows /
Runs / Connections, the standalone designer, and dataops Home as the
canonical reference; every surface at 1440×900 and 390×844, light and dark,
plus open mobile drawers. Harness: stubbed-API static server (fixture data
is placeholder content, not a design signal).

Judge passes (independent subagents, general-purpose runner; the
specialized visual-judge agent type was unavailable in this session's
provider registry):

- Round 1 (full set): 23 pass / 5 fail. Failures were the designer "Saved"
  status button styled as a filled primary, the accent-tinted attention
  panel, missing h1 descriptions, mobile touch scale + strip density.
- Round 2 (changed surfaces): 7 of 8 checks confirmed fixed by pixel
  measurement (header anatomy, neutral attention band, descriptions, quota
  form, designer switch/rail dots, mobile 44px targets and single-row
  strip). The one miss — no danger dot on the attention rows — was fixed
  (severity dots via the `.status` triplet roles) and re-verified.
- Round 5 (delta at the pushed tip, after the round-4 repairs and the four
  later commits): 22 fresh captures from the stub harness — public/legal
  dark mode, console views + sidebar close-ups + 390px, designer list and
  390px, light and dark. Verdict: PASS with no blocking issues. The four
  later commits verified: the designer mobile drawer is a modal dialog with
  scrim and focus trap (code-verified; drawer closed in shots), "New
  workflow" reads secondary below the single primary, the vendored dakit
  bundle carries the form-actions primitive, favicon committed. Designer
  list meta shot as "undefined/undefined" was traced to the harness stub
  missing the backend `_summary()` flat fields — stub now serves the
  contract shape (plus a no-trigger row exercising "No trigger yet") and
  the list was re-shot. Non-blocking notes kept: quota form verified in
  source only (capture next round), console mobile keeps the h1 below the
  64px bar, 32px nav rows and 2px focus ring are deliberate round-2
  decisions.
- Independent re-judge of the full set after the round-5 record (26 fresh
  captures re-rendered from the tip with rebuilt bundles): PASS on every
  capture. It also refutes the one blocking FAIL in the round-2 judge's
  lineage — "dataops renders a dark charcoal light-theme sidebar" — by
  pixel sampling: both apps' light rails measure `rgb(246,248,250)` with
  identical 268px geometry and border hairline, both dark rails
  `rgb(33,38,45)`; that judge's image reads had silently corrupted
  mid-run. Cosmetic notes kept for the next pass: the mobile runs mono
  token wraps mid-word, the Auto-paused stat cell wraps at 390px, the
  connections mobile action-pair alignment reads improvised.

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
  header instance.

## Re-judge (2026-10-03, independent, focus states added)

The independent re-judge at 2adf21a predated the last two commits (the
standalone-bundle rebuild 187c0a6 and the vendored dakit refresh 45b5721),
and no earlier round captured keyboard focus. This round re-ran the full
27-capture matrix (console Home/Workflows/Runs/Connections + standalone
designer + statically served dataops reference; 1440×900 and 390×844, light
and dark, open drawers) against tip 45b5721 with freshly verified served
bytes, plus dedicated focus captures with DOM/pixel evidence.

Harness corrections (screenshot rig only, not the app): the stub server
double-wrapped the designer-workflows and connections payloads — the real
API returns `{"workflows": [...]}` / `{"connections": [...]}` (see
`designer_store.listing.api_list` and `api/overview.py`), so the designer
now mounts in captures and 187c0a6's claim holds; the rig also gained the
`/assets/fonts/*` + `/fonts/*` aliases mirroring `api/router.py`, so
captures render the family typefaces.

Findings, all fixed in this round:

- **Nav-row focus ring was invisible** — computed style said 3px/2px, but
  the ring's outer band clipped against the zero-padding scroll nav inside
  the `overflow: hidden` sidebar; dataops renders its ring fully. Nav rows
  now inset the family ring (`outline-offset: -2px`, family.md's row
  recipe); pixel-scans confirm the band renders in both themes.
- **Text controls used a 2px outline with no halo** — replaced with the
  dataops pair: 3px ring on `:focus-visible`, accent border + `0 0 0 3px`
  halo on `:focus`; the storage-form textarea's private exception folded
  into the shared recipe; open-row outlines went 2px → 3px.
- **The designer bundle still embedded the pre-dea377c dakit base** (2px
  focus rules overriding the refreshed vendor copy) — 45b5721 refreshed
  `vendor/dakit.css` without rebuilding; `make designer-console` now
  rebuilds it (designer.js reproduces byte-for-byte; css carries the 3px
  recipe), and the designer source's own input rules match the pair above.

Verification: `make test` 3747 passed + 42 subtests; rebuilt bundle clean;
focus states re-captured with computed-style, DOM-probe, and pixel-scan
evidence (ring band inside the row in both themes, halo band around the
focused input); the matrix re-read against the dataops reference confirms
shell geometry, header anatomy, strips, banded panels, row rhythm, button
hierarchy, status language, icon strokes, and both palettes unchanged.

**Re-judge verdict: PASS.**

## Re-judge (2026-10-03, independent, focus states added)

The independent re-judge at 2adf21a predated the last two commits (the
standalone-bundle rebuild 187c0a6 and the vendored dakit refresh 45b5721),
and no earlier round captured keyboard focus. This round re-ran the full
27-capture matrix (console Home/Workflows/Runs/Connections + standalone
designer + statically served dataops reference; 1440×900 and 390×844, light
and dark, open drawers) against tip 45b5721 with freshly verified served
bytes, plus dedicated focus captures with DOM/pixel evidence.

Harness corrections (screenshot rig only, not the app): the stub server
double-wrapped the designer-workflows and connections payloads — the real
API returns `{"workflows": [...]}` / `{"connections": [...]}` (see
`designer_store.listing.api_list` and `api/overview.py`), so the designer
now mounts in captures and 187c0a6's claim holds; the rig also gained the
`/assets/fonts/*` + `/fonts/*` aliases mirroring `api/router.py`, so
captures render the family typefaces.

Findings, all fixed in this round:

- **Nav-row focus ring was invisible** — computed style said 3px/2px, but
  the ring's outer band clipped against the zero-padding scroll nav inside
  the `overflow: hidden` sidebar; dataops renders its ring fully. Nav rows
  now inset the family ring (`outline-offset: -2px`, family.md's row
  recipe); pixel-scans confirm the band renders in both themes.
- **Text controls used a 2px outline with no halo** — replaced with the
  dataops pair: 3px ring on `:focus-visible`, accent border + `0 0 0 3px`
  halo on `:focus`; the storage-form textarea's private exception folded
  into the shared recipe; open-row outlines went 2px → 3px.
- **The designer bundle still embedded the pre-dea377c dakit base** (2px
  focus rules overriding the refreshed vendor copy) — 45b5721 refreshed
  `vendor/dakit.css` without rebuilding; `make designer-console` now
  rebuilds it (designer.js reproduces byte-for-byte; css carries the 3px
  recipe), and the designer source's own input rules match the pair above.

Verification: `make test` 3747 passed + 42 subtests; rebuilt bundle clean;
focus states re-captured with computed-style, DOM-probe, and pixel-scan
evidence (ring band inside the row in both themes, halo band around the
focused input); the matrix re-read against the dataops reference confirms
shell geometry, header anatomy, strips, banded panels, row rhythm, button
hierarchy, status language, icon strokes, and both palettes unchanged.

**Re-judge verdict: PASS.**
