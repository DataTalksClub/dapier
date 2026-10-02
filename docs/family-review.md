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
