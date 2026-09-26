# Dapier console — design contract

## Attack: what the current console gets wrong

1. The dark sidebar beside a pale content field makes the shell resemble a stock admin template. The rail is louder than the operator's work.
2. Four metrics march across Overview at equal weight, even though connected accounts and recent runs answer different questions. The values lack a clear time or scope cue.
3. The Connections page repeats the page title, then explains the section before showing any account. Five equally sized provider cards once pushed the actual accounts below the fold.
4. Generic rounded controls make actions with different consequences look alike. In the connection dialog, save, grant management, reconnect, and revoke compete in one button band.
5. The table spends a full column repeating provider names while the account identity—the information that distinguishes two Google Calendars—has been absent.
6. Helper prose appears under most headings. It can bury the status and next action an operator came to see.
7. The modal has the same soft card treatment as every other app surface. Long forms and access grants need the clarity of a working sheet, not a decorative card.

These are claims about the UI in this repository. They are the checklist for screenshot review.

## Direction: the operations register

Dapier is a control plane. It should look like a working register: quiet paper, sharp rules, strong type, and one green action signal. Navigation belongs on the same plane as the work, separated by a vertical rule. Account identity and state outrank decorative provider branding. The interface is deliberately dense where people compare records and spacious where they make a decision.

### Type

- IBM Plex Sans (self-hosted) for prose, controls, and the single large page title.
- IBM Plex Mono (self-hosted) for IDs, timestamps, technical values, small structural labels, and metric numbers.
- Scale: 11px structural labels; 12px metadata; 13px table/control text; 15px section headings; 18px dialog titles; 32px page title; 38px principal metric. Weight 500/600 provides hierarchy; no all-caps micro-labels above every heading.

### Color

| Token | Light | Dark | Use |
|---|---|---|---|
| Paper | `#eeede6` | `#101611` | page and navigation plane |
| Surface | `#faf9f4` | `#182019` | forms and dialogs |
| Ink | `#202820` | `#edf2e9` | primary text |
| Secondary ink | `#485348` | `#bac8b8` | table prose |
| Muted ink | `#667166` | `#91a090` | metadata |
| Rule | `#c4cabf` | `#344235` | rows and controls |
| Strong rule | `#879688` | `#627563` | shell and section boundaries |
| Signal green | `#0b6745` | `#49b981` | primary action and active navigation |
| Signal dark | `#074e33` | `#70d59e` | hover/focus |
| Alert | `#9c4b20` | `#e6a36f` | errors and incomplete setup |

No gradients. No colored status chips. A dot and a word carry state; a rule or solid fill marks the chosen action. Feedback may use a faint paper tint but routine content does not.

### Geometry and spacing

- Navigation width: 204px desktop; drawer on narrow screens. Navigation and content share the paper color; a 1px strong rule separates them.
- Content gutters: 40px desktop, 24px tablet, 18px mobile. Major section rhythm: 40px; table rows: at least 52px.
- Controls are square (`0px` radius), 38px tall. Dialogs also have square corners. The only circles are status dots and the environment indicator.
- One-pixel rules provide structure. No drop shadows on cards, buttons, or tables. A dialog uses a single hard `4px 4px 0` offset shadow.
- Provider choices are ruled rows, not equal-height cards. The connection table is the first content block.
- Mobile tables become stacked records with the account identity first. Actions wrap below metadata; no horizontal overflow for common operations.

### Components

- Page title: one 32px title in the top bar. Sections get a distinct heading only when they introduce a new task; no duplicate “Connections” heading.
- Primary button: solid signal green. Secondary: transparent with a strong rule. Destructive actions are text/rule and visually separated from routine save.
- Status: 7px dot plus plain text. “Setup incomplete” and “Needs reconnection” use alert ink; connected uses signal green.
- Table: one hairline per record, no zebra tint. Name and verified account identity share the first cell; internal ID lives in Manage.
- Form: labels above inputs, square borders, generous grouping rules. Grants are working rows with permission and expiry metadata.
- Dialog: flat surface, strong top and bottom rules, hard offset shadow. It must remain usable without visual dependence on the backdrop.
- Icons: navigation and unlabelled controls only. Never inside a labelled action button.

### Banned tells

Dark sidebar against a light page; equal metric cards; uniform rounded rectangles; pastel status pills; eyebrow text above every heading; decorative icons in labelled buttons; generic explainer copy; gradients; glass effects; repeated shadows; equal-size provider cards above account data.

## Verification

Render the real HTML, CSS, and JS at desktop and mobile widths. Capture login/forbidden, Overview, Connections (including add picker and pending/connected states), a management dialog, the grants dialog, and a mobile table. Check the attack list against the images, then smoke-check navigation, picker, dialogs, grant save/revoke, token revoke, and browser console errors. The designer iframe keeps its full-height canvas behavior and gets a navigation smoke check; its own editor visual system is outside this console reskin.
