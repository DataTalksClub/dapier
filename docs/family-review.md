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

## Independent judge rounds — integration + phone chrome (2026-10-02 night)

The icon-set branch (family 16-grid on console + designer), the anatomy
branch, and origin/main were integrated on `redesign/family-integrated`,
then the phone chrome was brought to the family spec and re-reviewed by
independent judge agents (general-purpose runners; every round ran against
fresh captures in `/tmp/dapier-judge-final/shots` — console Home /
Workflows / Connections / Runs + the designer, 1440×900 and 390×844, light
and dark, drawers and the designer sheet — with dakit's
`docs/family-reference` as the canonical side-by-side):

- **Desktop: PASS (round 2).** Pixel-verified exact family values: 268px
  sidebar with identical fills in both themes, 32px header scale,
  #D0D7DE hairlines, #315F8F primary, #CF222E danger, identical dark-role
  flip (#58A6FF accent), no off-family hex/radii/shadows.
- **Mobile: PASS (round 4)** after three fix rounds: a fixed 64px app bar
  (menu + title over a full-bleed hairline) with the page description and
  tools in the content column; every console and designer-chrome control at
  `--dk-size-touch` (44px) — including filter rows, workflow header and row
  actions, drawer footer, the designer tool row, zoom pill, sheet controls,
  and an 18px checkbox visual inside a 44px padded hit area; the designer
  node detail opens as a full-screen sheet with a close button (one pane at
  a time); the open drawer carries `--dk-shadow-overlay`; the zoom overlay
  uses the 10px overlay radius.

Fix-round findings, kept for the record: desktop-density pins
(`--dk-size-control-md/sm`) outranked the phone bumps until they moved
behind `min-width: 861px` queries; bare `input, select` filters needed
their own bump; the designer page loads only `designer.css`, so its touch
scale must live there; `.add-row`, `.node-jump-item`, `.sidebar-action`
and the zoom icon-buttons needed explicit 44px rules.

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

## Round 9 — drift audit at the plugin-era main tip (2026-10-03)

Fresh adversarial pass at main `5e7f063` (after the plugin program landed on
top of the family work). Method: the live `src/web` served through the
stubbed-API rig (`.tmp/family-r9/`, fixtures corrected to the real listing
shapes), 41 captures — every console register, the designer, public home and
terms, device-free mobile set at 390×844, mobile drawers, the connections
manage dialog, keyboard focus states, a live theme round-trip — plus
computed-style probes (`.tmp/family-r9/shots/probes.json`) and a vendored
bundle diff against `dakit/dist`.

Generic-tell sweep: clean. No gradients, no pastel pills (dot+word only), no
eyebrow micro-labels above content, no icons in labelled buttons, no
dark-sidebar/light-canvas split (nav shares the page plane), no uniform
metric-card rows, real type tension (32px/700 h1 over 11–12px mono metadata),
tokens byte-equal to the dapier remap in both themes.

What the audit did find — all family **drift** that accumulated while the
spec moved on dakit side (`71a39d3` icon geometry, `dea377c` focus recipe):

1. **Focus recipe stale** (probe: 2px outline everywhere; `box-shadow: none`
   on a focused input): app-local mirrors in `app.css` (3 rules) and
   `designer.css` (3 rules) still teach the old base recipe; the vendored
   bundle predates dakit `dea377c` (3px ring, 2px offset, input halo).
2. **Icons off-geometry** (probe: 16px svgs, stroke-width 1.5px): console
   `icons.js` and designer `icons.tsx` still draw the retired 16×16 grid;
   family.md now mandates the 24-grid at 20px/stroke 1.8.
3. **Off-family brand identity**: `favicon.svg` and the public pages' brand
   tile are the pre-family green rounded-`d` (raw hex); the app shell's mark
   is the accent square with the bold "D".
4. **Public pages ignore the theme** (`public.css` pins light; the dark-mode
   capture renders white).
5. **Designer mobile drawer** opens with a scrim but no `role="dialog"` /
   `aria-modal` (the console drawer has both).
6. **Null-trigger crash** (functional, surfaced by the no-trigger fixture
   row): `format.js` `triggerLabel()` dereferences `workflow.trigger.connector`
   unguarded, so a workflow without a resolvable trigger breaks the Home and
   Workflows renders with "Could not load workflows/activity" banners.

Fixes for 1–6 land in this round; verdict below.

### Round 9 verdict - PASS (2026-10-03, at the fix tip `9ee5226`)

Disclosure first: the round-9 judge pass was performed by the implementing
agent, not an independent runner - the workflow subagent runner was
unavailable in this session. The judge protocol was followed literally to
keep that honest: all 41 post-fix captures in `.tmp/family-r9/shots/` were
individually opened and reviewed (none verdicted from filename or probe
data alone), the pre-fix set (`shots-before/`) was compared for every
round-9 finding, the dataops `family-reference/home-1440x900-light.png` was
reviewed side-by-side against the dapier Home, and disputed values were
pixel-sampled with PIL:

- Brand tile in the public header samples at (49, 95, 143) - exactly the
  family accent `#315f8f` (was the off-palette green before).
- Public dark page background samples (13, 17, 23) - exactly `#0d1117`.
- The keyboard focus ring around a nav row measures 3px thick at both its
  top (y 158-160) and bottom (y 197-199) bands; computed probes agree
  (`3px solid rgba(49,95,143,.6) @2px`), and the manage dialog's focused
  input computes `border-color #315f8f` + `box-shadow 0 0 0 3px` halo with
  no outline - the dea377c recipe, in both themes.
- Sidebar fill samples (246, 248, 250) = `#f6f8fa`, page `#ffffff`; icons
  compute `width 20 / stroke-width 1.8` on `viewBox="0 0 24 24"` across
  console and designer chrome (brand marks keep their sanctioned sizes).
- Both mobile drawers: console `role=dialog aria-modal=true` (unchanged),
  designer now the same - plus live-verified `main.inert`, focus moved
  inside on open and restored to the toggle on Escape close.

Findings status: all six round-9 findings FIXED in the renders (focus
recipe, icon geometry, brand mark, public theme, drawer a11y, and the
null-trigger crash - the workflows register and Home now render the
"no-trigger" row with "No trigger yet / No runs yet" and no error banners).
Generic-tell sweep stays clean; the acceptance list (shell geometry, header
scale, row rhythm, button hierarchy, status language, icon strokes, focus
treatment, palette both themes) holds on every reviewed surface. Zero page
errors across all 41 captures; the dark-to-light theme round-trip works.

Non-blocking observations, kept for the next pass: the connections mobile
summary truncates ("1 needs att...") at 390px - acceptable, watched; the
workflow org tags remain outline chips (content labels, not status pills -
unchanged from the judged rounds); the public pages' single kicker label
above the h1 stays as the recorded public-page exception. An independent
re-judge can re-run the whole matrix with `.tmp/family-r9/shoot.py`
(idempotent, own ephemeral port per run) and the same protocol.
