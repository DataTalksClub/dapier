/* dapier's icon set: one drawing style family-wide — inline SVG on the
   family's 24x24 grid rendered at 20px, stroke-width 1.8, round caps and
   joins, no fills (dakit family.md). Paths are lucide's native geometry
   (regenerate with the scratch converter, never by hand). */
const PATHS = {
    "bot": "<path d='M 12 8 V 4 H 8'/><rect width='16' height='12' x='4' y='8' rx='2'/><path d='M 2 14 h 2'/><path d='M 20 14 h 2'/><path d='M 15 13 v 2'/><path d='M 9 13 v 2'/>",
    "server": "<rect width='20' height='8' x='2' y='2' rx='2' ry='2'/><rect width='20' height='8' x='2' y='14' rx='2' ry='2'/><line x1='6' x2='6.01' y1='6' y2='6'/><line x1='6' x2='6.01' y1='18' y2='18'/>",
    "workflow": "<rect width='8' height='8' x='3' y='3' rx='2'/><path d='M 7 11 v 4 a2 2 0 0 0 2 2 h 4'/><rect width='8' height='8' x='13' y='13' rx='2'/>",
    "mail": "<path d='m 22 7 -8.98 5.73 a2 2 0 0 1 -2.01 0 L 2 7'/><rect x='2' y='4' width='20' height='16' rx='2'/>",
    "clock": "<path d='M 12 6 v 6 l 4 2'/><circle cx='12' cy='12' r='10'/>",
    "database": "<ellipse cx='12' cy='5' rx='9' ry='3'/><path d='M 3 5 V 19 A9 3 0 0 0 21 19 V 5'/><path d='M 3 12 A9 3 0 0 0 21 12'/>",
    "x": "<path d='M 18 6 6 18'/><path d='m 6 6 12 12'/>",
    "refresh-cw": "<path d='M 3 12 a9 9 0 0 1 9 -9 9.75 9.75 0 0 1 6.74 2.75 L 21 8'/><path d='M 21 3 v 5 h -5'/><path d='M 21 12 a9 9 0 0 1 -9 9 9.75 9.75 0 0 1 -6.74 -2.75 L 3 16'/><path d='M 8 16 H 3 v 5'/>",
    "more-horizontal": "<circle cx='12' cy='12' r='1.01'/><circle cx='19' cy='12' r='1.01'/><circle cx='5' cy='12' r='1.01'/>",
    "triangle-alert": "<path d='m 21.73 18 -8 -14 a2 2 0 0 0 -3.48 0 l -8 14 A2 2 0 0 0 4 21 h 16 a2 2 0 0 0 1.72 -3'/><path d='M 12 9 v 4'/><path d='M 12 17 h 0.01'/>",
    "zap": "<path d='M 4 14 a1.01 1.01 0 0 1 -0.78 -1.64 l 9.9 -10.2 a0.49 0.49 0 0 1 0.85 0.46 l -1.92 6.01 A1.01 1.01 0 0 0 13 10 h 7 a1.01 1.01 0 0 1 0.78 1.64 l -9.9 10.2 a0.49 0.49 0 0 1 -0.85 -0.46 l 1.92 -6.01 A1.01 1.01 0 0 0 11 14 z'/>",
    "webhook": "<path d='M 18 16.98 h -5.99 c -1.09 0 -1.95 0.95 -2.47 1.91 A4 4 0 0 1 2 17 c 0.01 -0.7 0.2 -1.4 0.57 -2'/><path d='m 6 17 3.13 -5.78 c 0.52 -0.98 0.11 -2.17 -0.49 -3.1 a4 4 0 1 1 6.88 -4.06'/><path d='m 12 6 3.13 5.73 C 15.66 12.71 16.91 13 18 13 a4 4 0 0 1 0 8'/>",
    "slack": "<rect width='3' height='8' x='13' y='2' rx='1.5'/><path d='M 19 8.5 V 10 h 1.5 A1.5 1.5 0 1 0 19 8.5'/><rect width='3' height='8' x='8' y='14' rx='1.5'/><path d='M 5 15.5 V 14 H 3.5 A1.5 1.5 0 1 0 5 15.5'/><rect width='8' height='3' x='14' y='13' rx='1.5'/><path d='M 15.5 19 H 14 v 1.5 a1.5 1.5 0 1 0 1.5 -1.5'/><rect width='8' height='3' x='2' y='8' rx='1.5'/><path d='M 8.5 5 H 10 V 3.5 A1.5 1.5 0 1 0 8.5 5'/>",
    "send": "<path d='M 14.54 21.69 a0.49 0.49 0 0 0 0.93 -0.03 l 6.5 -19 a0.49 0.49 0 0 0 -0.63 -0.63 l -19 6.5 a0.49 0.49 0 0 0 -0.03 0.93 l 7.94 3.18 a2 2 0 0 1 1.11 1.11 z'/><path d='m 21.86 2.15 -10.94 10.94'/>",
    "cloud-upload": "<path d='M 12 13 v 8'/><path d='M 4 14.89 A7 7 0 1 1 15.71 8 h 1.78 a4.5 4.5 0 0 1 2.5 8.23'/><path d='m 8 17 4 -4 4 4'/>",
    "trash-2": "<path d='M 10 11 v 6'/><path d='M 14 11 v 6'/><path d='M 19 6 v 14 a2 2 0 0 1 -2 2 H 7 a2 2 0 0 1 -2 -2 V 6'/><path d='M 3 6 h 18'/><path d='M 8 6 V 4 a2 2 0 0 1 2 -2 h 4 a2 2 0 0 1 2 2 v 2'/>",
    "file-text": "<path d='M 6 22 a2 2 0 0 1 -2 -2 V 4 a2 2 0 0 1 2 -2 h 8 a2.4 2.4 0 0 1 1.71 0.7 l 3.58 3.58 A2.4 2.4 0 0 1 20 8 v 12 a2 2 0 0 1 -2 2 z'/><path d='M 14 2 v 5 a1.01 1.01 0 0 0 1.01 1.01 h 5'/><path d='M 10 9 H 8'/><path d='M 16 13 H 8'/><path d='M 16 17 H 8'/>",
    "code-2": "<path d='m 18 16 4 -4 -4 -4'/><path d='m 6 8 -4 4 4 4'/><path d='m 14.5 4 -5 16'/>",
    "folder": "<path d='M 20 20 a2 2 0 0 0 2 -2 V 8 a2 2 0 0 0 -2 -2 h -7.9 a2 2 0 0 1 -1.69 -0.9 L 9.6 3.9 A2 2 0 0 0 7.93 3 H 4 a2 2 0 0 0 -2 2 v 13 a2 2 0 0 0 2 2 Z'/>",
    "arrow-right": "<path d='M 5 12 h 14'/><path d='m 12 5 7 7 -7 7'/>",
    "copy": "<rect width='14' height='14' x='8' y='8' rx='2' ry='2'/><path d='M 4 16 c -1.09 0 -2 -0.9 -2 -2 V 4 c 0 -1.09 0.9 -2 2 -2 h 10 c 1.09 0 2 0.9 2 2'/>",
    "check": "<path d='M 20 6 9 17 l -5 -5'/>",
    "git-branch": "<line x1='6' x2='6' y1='3' y2='15'/><circle cx='18' cy='6' r='3'/><circle cx='6' cy='18' r='3'/><path d='M 18 9 a9 9 0 0 1 -9 9'/>",
    "git-merge": "<circle cx='18' cy='18' r='3'/><circle cx='6' cy='6' r='3'/><path d='M 6 21 V 9 a9 9 0 0 0 9 9'/>",
    "filter": "<polygon points='22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3'/>",
    "sparkles": "<path d='M 9.94 15.5 A2 2 0 0 0 8.5 14.06 l -6.14 -1.58 a0.5 0.5 0 0 1 0 -0.96 L 8.5 9.94 A2 2 0 0 0 9.94 8.5 l 1.58 -6.14 a0.5 0.5 0 0 1 0.96 0 L 14.06 8.5 A2 2 0 0 0 15.5 9.94 l 6.14 1.58 a0.5 0.5 0 0 1 0 0.96 L 15.5 14.06 a2 2 0 0 0 -1.44 1.44 l -1.58 6.14 a0.5 0.5 0 0 1 -0.96 0 z'/><path d='M 20 3 v 4'/><path d='M 22 5 h -4'/><path d='M 4 17 v 2'/><path d='M 5 18 H 3'/>",
    "repeat": "<path d='m 17 2 4 4 -4 4'/><path d='M 3 11 v -1 a4 4 0 0 1 4 -4 h 14'/><path d='m 7 22 -4 -4 4 -4'/><path d='M 21 13 v 1 a4 4 0 0 1 -4 4 H 3'/>",
    "layers": "<path d='m 12.83 2.18 a2 2 0 0 0 -1.66 0 L 2.6 6.08 a1 1 0 0 0 0 1.83 l 8.58 3.91 a2 2 0 0 0 1.66 0 l 8.58 -3.9 a1 1 0 0 0 0 -1.83 Z'/><path d='m 22 17.65 -9.17 4.16 a2 2 0 0 1 -1.66 0 L 2 17.65'/><path d='m 22 12.65 -9.17 4.16 a2 2 0 0 1 -1.66 0 L 2 12.65'/>",
    "inbox": "<polyline points='22 12 16 12 14 15 10 15 8 12 2 12'/><path d='M 5.45 5.11 2 12 v 6 a2 2 0 0 0 2 2 h 16 a2 2 0 0 0 2 -2 v -6 l -3.45 -6.89 A2 2 0 0 0 16.76 4 H 7.24 a2 2 0 0 0 -1.79 1.11 z'/>",
    "terminal": "<polyline points='4 17 10 11 4 5'/><line x1='12' x2='20' y1='19' y2='19'/>",
    "cpu": "<rect width='16' height='16' x='4' y='4' rx='2'/><rect width='6' height='6' x='9' y='9' rx='1'/><path d='M 15 2 v 2'/><path d='M 15 20 v 2'/><path d='M 2 15 h 2'/><path d='M 2 9 h 2'/><path d='M 20 15 h 2'/><path d='M 20 9 h 2'/><path d='M 9 2 v 2'/><path d='M 9 20 v 2'/>",
};

/** One inline stroke icon: icon("x") -> svg markup. */
export function icon(name, cls) {
  const path = PATHS[name] || PATHS.x;
  const klass = cls ? `${cls} icon` : "icon";
  return `<svg class="${klass}" width="20" height="20" viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>${path}</svg>`;
}

/** Replace <i data-lucide="name"> placeholders with the family svg. */
export function mountIcons(root = document) {
  for (const el of root.querySelectorAll("i[data-lucide]")) {
    el.outerHTML = icon(el.getAttribute("data-lucide"), el.getAttribute("class"));
  }
}
