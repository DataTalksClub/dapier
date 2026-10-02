# Dapier console — design contract

Dapier renders the shared DataTalksClub family design. The source of truth is
the **dakit** design system (`../dakit`): `docs/family.md` there is the rubric,
and dataops' rendered UI is the canonical reference. When this document and
dakit disagree, dakit wins and this file gets updated.

## How the family is consumed

- The console serves a **vendored copy** of the built bundle:
  `src/web/vendor/dakit.css` + `src/web/vendor/dakit-tokens.css` (+ fonts).
  Never link `../dakit` from the app. After changing dakit, run
  `make sync-dakit` and commit the refreshed vendor files.
- All app CSS (`src/web/app.css`, designer chrome) is written against
  `--dk-*` tokens only. No app-local hex values, radii, or shadows. The one
  sanctioned exception is provider brand marks (Google/Slack/Zoom logos),
  which family.md allows to keep full color.
- The designer is the family's sanctioned dense toolshell: same shell
  anatomy and tokens, canvas density may go below the console's rhythm.

## Deliberate departures (recorded, not drift)

- Machine values (workflow ids, timestamps, "Updated") stay mono — dapier's
  identifier texture; dataops uses sans for the same slots.
- The workflows list expresses On/Off with a switch (it is the toggle, not a
  status readout); run status comes from the dot+word "Latest run" read.
- Connections rows may carry two actions (Get token + Manage): token
  retrieval is a distinct operator task. Candidate for a follow-up menu.
- The standalone designer's footer shows the working-copy git state
  (branch + clean/dirty). It is local tool chrome for the operator who
  commits workflows, not a family-shell element.

## Verification

Render the real HTML/CSS/JS through the stubbed-API static server and
capture every console page plus the designer at 1440×900 and 390×844, light
and dark, with dataops' home as the side-by-side reference. An independent
judge compares against `../dakit/docs/family.md`; rounds and verdicts are
recorded in `docs/family-review.md`. Functional smoke: navigation, picker,
dialogs, grant save/revoke, token revoke, browser console errors.
