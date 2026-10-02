/* dapier's icon set: one drawing style family-wide — inline SVG on a 16x16
   grid, stroke-width 1.5, round caps and joins, no fills (dakit family.md).
   Geometry scales lucide to the 16 grid so every surface keeps one icon
   language; regenerate with the scratch converter, never by hand. */
const PATHS = {
  "layout-dashboard": "<rect width='4.67' height='6' x='2' y='2' rx='0.67'/><rect width='4.67' height='3.33' x='9.33' y='2' rx='0.67'/><rect width='4.67' height='6' x='9.33' y='8' rx='0.67'/><rect width='4.67' height='3.33' x='2' y='10.67' rx='0.67'/>",
  "list-checks": "<path d='M 8.67 3.33 h 5.33'/><path d='M 8.67 8 h 5.33'/><path d='M 8.67 12.67 h 5.33'/><path d='m 2 11.33 1.33 1.33 2.67 -2.67'/><path d='m 2 4.67 1.33 1.33 2.67 -2.67'/>",
  "bot": "<path d='M 8 5.33 V 2.67 H 5.33'/><rect width='10.67' height='8' x='2.67' y='5.33' rx='1.33'/><path d='M 1.33 9.33 h 1.33'/><path d='M 13.33 9.33 h 1.33'/><path d='M 10 8.67 v 1.33'/><path d='M 6 8.67 v 1.33'/>",
  "server": "<rect width='13.33' height='5.33' x='1.33' y='1.33' rx='1.33' ry='1.33'/><rect width='13.33' height='5.33' x='1.33' y='9.33' rx='1.33' ry='1.33'/><line x1='4' x2='4.01' y1='4' y2='4'/><line x1='4' x2='4.01' y1='12' y2='12'/>",
  "workflow": "<rect width='5.33' height='5.33' x='2' y='2' rx='1.33'/><path d='M 4.67 7.33 v 2.67 a 1.33 1.33 0 0 0 1.33 1.33 h 2.67'/><rect width='5.33' height='5.33' x='8.67' y='8.67' rx='1.33'/>",
  "plug-zap": "<path d='M 4.2 13.53 a 1.6 1.6 0 0 0 2.27 0 L 8 12 l -4 -4 -1.53 1.53 a 1.6 1.6 0 0 0 0 2.27 Z'/><path d='m 1.33 14.67 2 -2'/><path d='M 5 9 6.67 7.33'/><path d='M 7 11 8.67 9.33'/><path d='m 12 2 -2.67 2.67 h 4 l -2.67 2.67'/>",
  "mail": "<path d='m 14.67 4.67 -5.99 3.82 a 1.33 1.33 0 0 1 -1.34 0 L 1.33 4.67'/><rect x='1.33' y='2.67' width='13.33' height='10.67' rx='1.33'/>",
  "clock": "<path d='M 8 4 v 4 l 2.67 1.33'/><circle cx='8' cy='8' r='6.67'/>",
  "key-round": "<path d='M 1.72 11.61 A 1.33 1.33 0 0 0 1.33 12.55 V 14 a 0.67 0.67 0 0 0 0.67 0.67 h 2 a 0.67 0.67 0 0 0 0.67 -0.67 v -0.67 a 0.67 0.67 0 0 1 0.67 -0.67 h 0.67 a 0.67 0.67 0 0 0 0.67 -0.67 v -0.67 a 0.67 0.67 0 0 1 0.67 -0.67 h 0.11 a 1.33 1.33 0 0 0 0.94 -0.39 l 0.54 -0.54 a 4.33 4.33 0 1 0 -2.67 -2.67 z'/><circle cx='11' cy='5' r='0.33' fill='currentColor'/>",
  "key-square": "<path d='M 8.27 1.8 a 1.67 1.67 0 0 1 2.27 0 l 3.67 3.67 a 1.67 1.67 0 0 1 0 2.27 l -2.47 2.47 a 1.67 1.67 0 0 1 -2.27 0 L 5.8 6.53 a 1.67 1.67 0 0 1 0 -2.27 z'/><path d='m 9.33 4.67 2 2'/><path d='m 6.27 7.07 -4.54 4.54 A 1.33 1.33 0 0 0 1.33 12.55 V 14 a 0.67 0.67 0 0 0 0.67 0.67 h 2 a 0.67 0.67 0 0 0 0.67 -0.67 v -0.67 a 0.67 0.67 0 0 1 0.67 -0.67 h 0.67 a 0.67 0.67 0 0 0 0.67 -0.67 v -0.67 a 0.67 0.67 0 0 1 0.67 -0.67 h 0.11 a 1.33 1.33 0 0 0 0.94 -0.39 l 0.54 -0.54'/>",
  "database": "<ellipse cx='8' cy='3.33' rx='6' ry='2'/><path d='M 2 3.33 V 12.67 A 6 2 0 0 0 14 12.67 V 3.33'/><path d='M 2 8 A 6 2 0 0 0 14 8'/>",
  "scroll-text": "<path d='M 10 8 h -3.33'/><path d='M 10 5.33 h -3.33'/><path d='M 12.67 11.33 V 3.33 a 1.33 1.33 0 0 0 -1.33 -1.33 H 2.67'/><path d='M 5.33 14 h 8 a 1.33 1.33 0 0 0 1.33 -1.33 v -0.67 a 0.67 0.67 0 0 0 -0.67 -0.67 H 7.33 a 0.67 0.67 0 0 0 -0.67 0.67 v 0.67 a 1.33 1.33 0 1 1 -2.67 0 V 3.33 a 1.33 1.33 0 1 0 -2.67 0 v 1.33 a 0.67 0.67 0 0 0 0.67 0.67 h 2'/>",
  "x": "<path d='M 12 4 4 12'/><path d='m 4 4 8 8'/>",
  "moon": "<path d='M 13.99 8.32 a 6 6 0 1 1 -6.32 -6.31 c 0.27 -0.01 0.41 0.31 0.27 0.54 a 4 4 0 0 0 5.51 5.51 c 0.23 -0.14 0.55 0 0.54 0.27'/>",
  "sun": "<circle cx='8' cy='8' r='2.67'/><path d='M 8 1.33 v 1.33'/><path d='M 8 13.33 v 1.33'/><path d='m 3.29 3.29 0.94 0.94'/><path d='m 11.77 11.77 0.94 0.94'/><path d='M 1.33 8 h 1.33'/><path d='M 13.33 8 h 1.33'/><path d='m 4.23 11.77 -0.94 0.94'/><path d='m 12.71 3.29 -0.94 0.94'/>",
  "log-out": "<path d='m 10.67 11.33 3.33 -3.33 -3.33 -3.33'/><path d='M 14 8 H 6'/><path d='M 6 14 H 3.33 a 1.33 1.33 0 0 1 -1.33 -1.33 V 3.33 a 1.33 1.33 0 0 1 1.33 -1.33 h 2.67'/>",
  "menu": "<path d='M 2.67 3.33 h 10.67'/><path d='M 2.67 8 h 10.67'/><path d='M 2.67 12.67 h 10.67'/>",
  "arrow-left": "<path d='m 8 12.67 -4.67 -4.67 4.67 -4.67'/><path d='M 12.67 8 H 3.33'/>",
  "refresh-cw": "<path d='M 2 8 a 6 6 0 0 1 6 -6 6.5 6.5 0 0 1 4.49 1.83 L 14 5.33'/><path d='M 14 2 v 3.33 h -3.33'/><path d='M 14 8 a 6 6 0 0 1 -6 6 6.5 6.5 0 0 1 -4.49 -1.83 L 2 10.67'/><path d='M 5.33 10.67 H 2 v 3.33'/>",
  "more-horizontal": "<circle cx='8' cy='8' r='0.67'/><circle cx='12.67' cy='8' r='0.67'/><circle cx='3.33' cy='8' r='0.67'/>",
  "triangle-alert": "<path d='m 14.49 12 -5.33 -9.33 a 1.33 1.33 0 0 0 -2.32 0 l -5.33 9.33 A 1.33 1.33 0 0 0 2.67 14 h 10.67 a 1.33 1.33 0 0 0 1.15 -2'/><path d='M 8 6 v 2.67'/><path d='M 8 11.33 h 0.01'/>",
  "zap": "<path d='M 2.67 9.33 a 0.67 0.67 0 0 1 -0.52 -1.09 l 6.6 -6.8 a 0.33 0.33 0 0 1 0.57 0.31 l -1.28 4.01 A 0.67 0.67 0 0 0 8.67 6.67 h 4.67 a 0.67 0.67 0 0 1 0.52 1.09 l -6.6 6.8 a 0.33 0.33 0 0 1 -0.57 -0.31 l 1.28 -4.01 A 0.67 0.67 0 0 0 7.33 9.33 z'/>",
  "webhook": "<path d='M 12 11.32 h -3.99 c -0.73 0 -1.3 0.63 -1.65 1.27 A 2.67 2.67 0 0 1 1.33 11.33 c 0.01 -0.47 0.13 -0.93 0.38 -1.33'/><path d='m 4 11.33 2.09 -3.85 c 0.35 -0.65 0.07 -1.45 -0.33 -2.07 a 2.67 2.67 0 1 1 4.59 -2.71'/><path d='m 8 4 2.09 3.82 C 10.44 8.47 11.27 8.67 12 8.67 a 2.67 2.67 0 0 1 0 5.33'/>",
  "slack": "<rect width='2' height='5.33' x='8.67' y='1.33' rx='1'/><path d='M 12.67 5.67 V 6.67 h 1 A 1 1 0 1 0 12.67 5.67'/><rect width='2' height='5.33' x='5.33' y='9.33' rx='1'/><path d='M 3.33 10.33 V 9.33 H 2.33 A 1 1 0 1 0 3.33 10.33'/><rect width='5.33' height='2' x='9.33' y='8.67' rx='1'/><path d='M 10.33 12.67 H 9.33 v 1 a 1 1 0 1 0 1 -1'/><rect width='5.33' height='2' x='1.33' y='5.33' rx='1'/><path d='M 5.67 3.33 H 6.67 V 2.33 A 1 1 0 1 0 5.67 3.33'/>",
  "send": "<path d='M 9.69 14.46 a 0.33 0.33 0 0 0 0.62 -0.02 l 4.33 -12.67 a 0.33 0.33 0 0 0 -0.42 -0.42 l -12.67 4.33 a 0.33 0.33 0 0 0 -0.02 0.62 l 5.29 2.12 a 1.33 1.33 0 0 1 0.74 0.74 z'/><path d='m 14.57 1.43 -7.29 7.29'/>",
  "cloud-upload": "<path d='M 8 8.67 v 5.33'/><path d='M 2.67 9.93 A 4.67 4.67 0 1 1 10.47 5.33 h 1.19 a 3 3 0 0 1 1.67 5.49'/><path d='m 5.33 11.33 2.67 -2.67 2.67 2.67'/>",
  "trash-2": "<path d='M 6.67 7.33 v 4'/><path d='M 9.33 7.33 v 4'/><path d='M 12.67 4 v 9.33 a 1.33 1.33 0 0 1 -1.33 1.33 H 4.67 a 1.33 1.33 0 0 1 -1.33 -1.33 V 4'/><path d='M 2 4 h 12'/><path d='M 5.33 4 V 2.67 a 1.33 1.33 0 0 1 1.33 -1.33 h 2.67 a 1.33 1.33 0 0 1 1.33 1.33 v 1.33'/>",
  "file-text": "<path d='M 4 14.67 a 1.33 1.33 0 0 1 -1.33 -1.33 V 2.67 a 1.33 1.33 0 0 1 1.33 -1.33 h 5.33 a 1.6 1.6 0 0 1 1.14 0.47 l 2.39 2.39 A 1.6 1.6 0 0 1 13.33 5.33 v 8 a 1.33 1.33 0 0 1 -1.33 1.33 z'/><path d='M 9.33 1.33 v 3.33 a 0.67 0.67 0 0 0 0.67 0.67 h 3.33'/><path d='M 6.67 6 H 5.33'/><path d='M 10.67 8.67 H 5.33'/><path d='M 10.67 11.33 H 5.33'/>",
  "code-2": "<path d='m 12 10.67 2.67 -2.67 -2.67 -2.67'/><path d='m 4 5.33 -2.67 2.67 2.67 2.67'/><path d='m 9.67 2.67 -3.33 10.67'/>",
  "arrow-right": "<path d='M 3.33 8 h 9.33'/><path d='m 8 3.33 4.67 4.67 -4.67 4.67'/>",
  "copy": "<rect width='9.33' height='9.33' x='5.33' y='5.33' rx='1.33' ry='1.33'/><path d='M 2.67 10.67 c -0.73 0 -1.33 -0.6 -1.33 -1.33 V 2.67 c 0 -0.73 0.6 -1.33 1.33 -1.33 h 6.67 c 0.73 0 1.33 0.6 1.33 1.33'/>",
  "check": "<path d='M 13.33 4 6 11.33 l -3.33 -3.33'/>",
  "external-link": "<path d='M 10 2 h 4 v 4'/><path d='M 6.67 9.33 14 2'/><path d='M 12 8.67 v 4 a 1.33 1.33 0 0 1 -1.33 1.33 H 3.33 a 1.33 1.33 0 0 1 -1.33 -1.33 V 5.33 a 1.33 1.33 0 0 1 1.33 -1.33 h 4'/>",
  "plus": "<path d='M 3.33 8 h 9.33'/><path d='M 8 3.33 v 9.33'/>",
};

/* Historical names some call sites still use. */
const ALIAS = { "alert-triangle": "triangle-alert" };

/** One inline stroke icon: icon("x") -> svg markup. */
export function icon(name, cls) {
  const key = ALIAS[name] || name;
  const path = PATHS[key] || PATHS.x;
  const klass = cls ? `${cls} icon` : "icon";
  return `<svg class="${klass}" width="16" height="16" viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>${path}</svg>`;
}

/** Replace <i data-lucide="name"> placeholders with the family svg. */
export function mountIcons(root = document) {
  for (const el of root.querySelectorAll("i[data-lucide]")) {
    el.outerHTML = icon(el.getAttribute("data-lucide"), el.getAttribute("class"));
  }
}
