/* dapier's icon set: one drawing style family-wide — inline SVG on a 16x16
 * grid, stroke-width 1.5, round caps and joins, no fills (dakit family.md).
 * Geometry scales lucide to the 16 grid so the canvas, palette and chrome
 * keep one icon language; regenerate with the scratch converter, never by hand. */

import type { IconProps } from "./catalog";

const ICON_NODES: Record<string, string> = {
  "arrow-left": "<path d='m 8 12.67 -4.67 -4.67 4.67 -4.67'/><path d='M 12.67 8 H 3.33'/>",
  "arrow-right": "<path d='M 3.33 8 h 9.33'/><path d='m 8 3.33 4.67 4.67 -4.67 4.67'/>",
  "bot": "<path d='M 8 5.33 V 2.67 H 5.33'/><rect width='10.67' height='8' x='2.67' y='5.33' rx='1.33'/><path d='M 1.33 9.33 h 1.33'/><path d='M 13.33 9.33 h 1.33'/><path d='M 10 8.67 v 1.33'/><path d='M 6 8.67 v 1.33'/>",
  "braces": "<path d='M 5.33 2 H 4.67 a 1.33 1.33 0 0 0 -1.33 1.33 v 3.33 a 1.33 1.33 0 0 1 -1.33 1.33 1.33 1.33 0 0 1 1.33 1.33 v 3.33 c 0 0.73 0.6 1.33 1.33 1.33 h 0.67'/><path d='M 10.67 14 h 0.67 a 1.33 1.33 0 0 0 1.33 -1.33 v -3.33 c 0 -0.73 0.6 -1.33 1.33 -1.33 a 1.33 1.33 0 0 1 -1.33 -1.33 V 3.33 a 1.33 1.33 0 0 0 -1.33 -1.33 h -0.67'/>",
  "calendar": "<path d='M 5.33 1.33 v 2.67'/><path d='M 10.67 1.33 v 2.67'/><rect width='12' height='12' x='2' y='2.67' rx='1.33'/><path d='M 2 6.67 h 12'/>",
  "check": "<path d='M 13.33 4 6 11.33 l -3.33 -3.33'/>",
  "chevron-down": "<path d='m 4 6 4 4 4 -4'/>",
  "chevron-right": "<path d='m 6 12 4 -4 -4 -4'/>",
  "clipboard-copy": "<rect width='5.33' height='2.67' x='5.33' y='1.33' rx='0.67' ry='0.67'/><path d='M 5.33 2.67 H 4 a 1.33 1.33 0 0 0 -1.33 1.33 v 9.33 a 1.33 1.33 0 0 0 1.33 1.33 h 8 a 1.33 1.33 0 0 0 1.33 -1.33 v -1.33'/><path d='M 10.67 2.67 h 1.33 a 1.33 1.33 0 0 1 1.33 1.33 v 2.67'/><path d='M 14 9.33 H 7.33'/><path d='m 10 6.67 -2.67 2.67 2.67 2.67'/>",
  "clipboard-paste": "<path d='M 7.33 9.33 h 6.67'/><path d='M 10.67 2.67 h 1.33 a 1.33 1.33 0 0 1 1.33 1.33 v 0.9'/><path d='m 11.33 12 2.67 -2.67 -2.67 -2.67'/><path d='M 5.33 2.67 H 4 a 1.33 1.33 0 0 0 -1.33 1.33 v 9.33 a 1.33 1.33 0 0 0 1.33 1.33 h 8 a 1.33 1.33 0 0 0 1.2 -0.74'/><rect x='5.33' y='1.33' width='5.33' height='2.67' rx='0.67'/>",
  "clock": "<path d='M 8 4 v 4 l 2.67 1.33'/><circle cx='8' cy='8' r='6.67'/>",
  "cloud-download": "<path d='M 8 8.67 v 5.33 l -2.67 -2.67'/><path d='m 8 14 2.67 -2.67'/><path d='M 2.93 10.18 A 4.67 4.67 0 1 1 10.47 5.33 h 1.19 a 3 3 0 0 1 1.62 5.52'/>",
  "code-2": "<path d='m 12 10.67 2.67 -2.67 -2.67 -2.67'/><path d='m 4 5.33 -2.67 2.67 2.67 2.67'/><path d='m 9.67 2.67 -3.33 10.67'/>",
  "copy": "<rect width='9.33' height='9.33' x='5.33' y='5.33' rx='1.33' ry='1.33'/><path d='M 2.67 10.67 c -0.73 0 -1.33 -0.6 -1.33 -1.33 V 2.67 c 0 -0.73 0.6 -1.33 1.33 -1.33 h 6.67 c 0.73 0 1.33 0.6 1.33 1.33'/>",
  "database": "<ellipse cx='8' cy='3.33' rx='6' ry='2'/><path d='M 2 3.33 V 12.67 A 6 2 0 0 0 14 12.67 V 3.33'/><path d='M 2 8 A 6 2 0 0 0 14 8'/>",
  "database-zap": "<ellipse cx='8' cy='3.33' rx='6' ry='2'/><path d='M 2 3.33 V 12.67 A 6 2 0 0 0 10 14.56'/><path d='M 14 3.33 V 5.33'/><path d='M 14 8 L 12 11.33 H 14.67 L 12.67 14.67'/><path d='M 2 8 A 6 2 0 0 0 9.73 9.91'/>",
  "external-link": "<path d='M 10 2 h 4 v 4'/><path d='M 6.67 9.33 14 2'/><path d='M 12 8.67 v 4 a 1.33 1.33 0 0 1 -1.33 1.33 H 3.33 a 1.33 1.33 0 0 1 -1.33 -1.33 V 5.33 a 1.33 1.33 0 0 1 1.33 -1.33 h 4'/>",
  "file-text": "<path d='M 4 14.67 a 1.33 1.33 0 0 1 -1.33 -1.33 V 2.67 a 1.33 1.33 0 0 1 1.33 -1.33 h 5.33 a 1.6 1.6 0 0 1 1.14 0.47 l 2.39 2.39 A 1.6 1.6 0 0 1 13.33 5.33 v 8 a 1.33 1.33 0 0 1 -1.33 1.33 z'/><path d='M 9.33 1.33 v 3.33 a 0.67 0.67 0 0 0 0.67 0.67 h 3.33'/><path d='M 6.67 6 H 5.33'/><path d='M 10.67 8.67 H 5.33'/><path d='M 10.67 11.33 H 5.33'/>",
  "filter": "<path d='M 6.67 13.33 a 0.67 0.67 0 0 0 0.37 0.6 l 1.33 0.67 A 0.67 0.67 0 0 0 9.33 14 v -4.67 a 1.33 1.33 0 0 1 0.34 -0.89 L 14.49 3.11 A 0.67 0.67 0 0 0 14 2 H 2 a 0.67 0.67 0 0 0 -0.49 1.11 l 4.82 5.33 A 1.33 1.33 0 0 1 6.67 9.33 z'/>",
  "flask-conical": "<path d='M 9.33 1.33 v 4 a 1.33 1.33 0 0 0 0.16 0.64 l 3.67 6.72 A 1.33 1.33 0 0 1 12 14.67 H 4 a 1.33 1.33 0 0 1 -1.17 -1.97 l 3.67 -6.72 A 1.33 1.33 0 0 0 6.67 5.33 V 1.33'/><path d='M 4.3 10 h 7.4'/><path d='M 5.67 1.33 h 4.67'/>",
  "folder": "<path d='M 13.33 13.33 a 1.33 1.33 0 0 0 1.33 -1.33 V 5.33 a 1.33 1.33 0 0 0 -1.33 -1.33 h -5.27 a 1.33 1.33 0 0 1 -1.13 -0.6 L 6.4 2.6 A 1.33 1.33 0 0 0 5.29 2 H 2.67 a 1.33 1.33 0 0 0 -1.33 1.33 v 8.67 a 1.33 1.33 0 0 0 1.33 1.33 Z'/>",
  "git-branch": "<line x1='4' x2='4' y1='2' y2='10'/><circle cx='12' cy='4' r='2'/><circle cx='4' cy='12' r='2'/><path d='M 12 6 a 6 6 0 0 1 -6 6'/>",
  "globe": "<circle cx='8' cy='8' r='6.67'/><path d='M 8 1.33 a 9.67 9.67 0 0 0 0 13.33 9.67 9.67 0 0 0 0 -13.33'/><path d='M 1.33 8 h 13.33'/>",
  "keyboard": "<path d='M 6.67 5.33 h 0.01'/><path d='M 8 8 h 0.01'/><path d='M 9.33 5.33 h 0.01'/><path d='M 10.67 8 h 0.01'/><path d='M 12 5.33 h 0.01'/><path d='M 4 5.33 h 0.01'/><path d='M 4.67 10.67 h 6.67'/><path d='M 5.33 8 h 0.01'/><rect width='13.33' height='10.67' x='1.33' y='2.67' rx='1.33'/>",
  "key-round": "<path d='M 1.72 11.61 A 1.33 1.33 0 0 0 1.33 12.55 V 14 a 0.67 0.67 0 0 0 0.67 0.67 h 2 a 0.67 0.67 0 0 0 0.67 -0.67 v -0.67 a 0.67 0.67 0 0 1 0.67 -0.67 h 0.67 a 0.67 0.67 0 0 0 0.67 -0.67 v -0.67 a 0.67 0.67 0 0 1 0.67 -0.67 h 0.11 a 1.33 1.33 0 0 0 0.94 -0.39 l 0.54 -0.54 a 4.33 4.33 0 1 0 -2.67 -2.67 z'/><circle cx='11' cy='5' r='0.33' fill='currentColor'/>",
  "key-square": "<path d='M 8.27 1.8 a 1.67 1.67 0 0 1 2.27 0 l 3.67 3.67 a 1.67 1.67 0 0 1 0 2.27 l -2.47 2.47 a 1.67 1.67 0 0 1 -2.27 0 L 5.8 6.53 a 1.67 1.67 0 0 1 0 -2.27 z'/><path d='m 9.33 4.67 2 2'/><path d='m 6.27 7.07 -4.54 4.54 A 1.33 1.33 0 0 0 1.33 12.55 V 14 a 0.67 0.67 0 0 0 0.67 0.67 h 2 a 0.67 0.67 0 0 0 0.67 -0.67 v -0.67 a 0.67 0.67 0 0 1 0.67 -0.67 h 0.67 a 0.67 0.67 0 0 0 0.67 -0.67 v -0.67 a 0.67 0.67 0 0 1 0.67 -0.67 h 0.11 a 1.33 1.33 0 0 0 0.94 -0.39 l 0.54 -0.54'/>",
  "layers": "<path d='M 8.55 1.45 a 1.33 1.33 0 0 0 -1.11 0 L 1.73 4.05 a 0.67 0.67 0 0 0 0 1.22 l 5.72 2.61 a 1.33 1.33 0 0 0 1.11 0 l 5.72 -2.6 a 0.67 0.67 0 0 0 0 -1.22 z'/><path d='M 1.33 8 a 0.67 0.67 0 0 0 0.39 0.61 l 5.73 2.61 a 1.33 1.33 0 0 0 1.1 0 l 5.72 -2.6 A 0.67 0.67 0 0 0 14.67 8'/><path d='M 1.33 11.33 a 0.67 0.67 0 0 0 0.39 0.61 l 5.73 2.61 a 1.33 1.33 0 0 0 1.1 0 l 5.72 -2.6 A 0.67 0.67 0 0 0 14.67 11.33'/>",
  "list-plus": "<path d='M 10.67 3.33 H 2'/><path d='M 7.33 8 H 2'/><path d='M 10.67 12.67 H 2'/><path d='M 12 6 v 4'/><path d='M 14 8 h -4'/>",
  "list-restart": "<path d='M 14 3.33 H 2'/><path d='M 4.67 8 H 2'/><path d='M 4.67 12.67 H 2'/><path d='M 8 12 a 3.33 3.33 0 0 0 6 -2 3 3 0 0 0 -3 -3 c -0.89 0 -1.69 0.36 -2.27 0.94 L 7.33 9.33'/><path d='M 7.33 6.67 v 2.67 h 2.67'/>",
  "list-tree": "<path d='M 5.33 3.33 h 8.67'/><path d='M 8.67 8 h 5.33'/><path d='M 8.67 12.67 h 5.33'/><path d='M 2 6.67 a 1.33 1.33 0 0 0 1.33 1.33 h 2'/><path d='M 2 3.33 v 8 a 1.33 1.33 0 0 0 1.33 1.33 h 2'/>",
  "loader-circle": "<path d='M 14 8 a 6 6 0 1 1 -4.15 -5.71'/>",
  "log-out": "<path d='m 10.67 11.33 3.33 -3.33 -3.33 -3.33'/><path d='M 14 8 H 6'/><path d='M 6 14 H 3.33 a 1.33 1.33 0 0 1 -1.33 -1.33 V 3.33 a 1.33 1.33 0 0 1 1.33 -1.33 h 2.67'/>",
  "mail": "<path d='m 14.67 4.67 -5.99 3.82 a 1.33 1.33 0 0 1 -1.34 0 L 1.33 4.67'/><rect x='1.33' y='2.67' width='13.33' height='10.67' rx='1.33'/>",
  "maximize": "<path d='M 5.33 2 H 3.33 a 1.33 1.33 0 0 0 -1.33 1.33 v 2'/><path d='M 14 5.33 V 3.33 a 1.33 1.33 0 0 0 -1.33 -1.33 h -2'/><path d='M 2 10.67 v 2 a 1.33 1.33 0 0 0 1.33 1.33 h 2'/><path d='M 10.67 14 h 2 a 1.33 1.33 0 0 0 1.33 -1.33 v -2'/>",
  "menu": "<path d='M 2.67 3.33 h 10.67'/><path d='M 2.67 8 h 10.67'/><path d='M 2.67 12.67 h 10.67'/>",
  "minus": "<path d='M 3.33 8 h 9.33'/>",
  "moon": "<path d='M 13.99 8.32 a 6 6 0 1 1 -6.32 -6.31 c 0.27 -0.01 0.41 0.31 0.27 0.54 a 4 4 0 0 0 5.51 5.51 c 0.23 -0.14 0.55 0 0.54 0.27'/>",
  "play": "<path d='M 3.33 3.33 a 1.33 1.33 0 0 1 2.01 -1.15 l 8 4.67 a 1.33 1.33 0 0 1 0 2.31 l -8 4.67 A 1.33 1.33 0 0 1 3.33 12.67 z'/>",
  "plus": "<path d='M 3.33 8 h 9.33'/><path d='M 8 3.33 v 9.33'/>",
  "refresh-cw": "<path d='M 2 8 a 6 6 0 0 1 6 -6 6.5 6.5 0 0 1 4.49 1.83 L 14 5.33'/><path d='M 14 2 v 3.33 h -3.33'/><path d='M 14 8 a 6 6 0 0 1 -6 6 6.5 6.5 0 0 1 -4.49 -1.83 L 2 10.67'/><path d='M 5.33 10.67 H 2 v 3.33'/>",
  "rotate-ccw": "<path d='M 2 8 a 6 6 0 1 0 6 -6 6.5 6.5 0 0 0 -4.49 1.83 L 2 5.33'/><path d='M 2 2 v 3.33 h 3.33'/>",
  "rotate-cw": "<path d='M 14 8 a 6 6 0 1 1 -6 -6 c 1.68 0 3.29 0.67 4.49 1.83 L 14 5.33'/><path d='M 14 2 v 3.33 h -3.33'/>",
  "rss": "<path d='M 2.67 7.33 a 6 6 0 0 1 6 6'/><path d='M 2.67 2.67 a 10.67 10.67 0 0 1 10.67 10.67'/><circle cx='3.33' cy='12.67' r='0.67'/>",
  "scroll-text": "<path d='M 10 8 h -3.33'/><path d='M 10 5.33 h -3.33'/><path d='M 12.67 11.33 V 3.33 a 1.33 1.33 0 0 0 -1.33 -1.33 H 2.67'/><path d='M 5.33 14 h 8 a 1.33 1.33 0 0 0 1.33 -1.33 v -0.67 a 0.67 0.67 0 0 0 -0.67 -0.67 H 7.33 a 0.67 0.67 0 0 0 -0.67 0.67 v 0.67 a 1.33 1.33 0 1 1 -2.67 0 V 3.33 a 1.33 1.33 0 1 0 -2.67 0 v 1.33 a 0.67 0.67 0 0 0 0.67 0.67 h 2'/>",
  "search": "<path d='m 14 14 -2.89 -2.89'/><circle cx='7.33' cy='7.33' r='5.33'/>",
  "send": "<path d='M 9.69 14.46 a 0.33 0.33 0 0 0 0.62 -0.02 l 4.33 -12.67 a 0.33 0.33 0 0 0 -0.42 -0.42 l -12.67 4.33 a 0.33 0.33 0 0 0 -0.02 0.62 l 5.29 2.12 a 1.33 1.33 0 0 1 0.74 0.74 z'/><path d='m 14.57 1.43 -7.29 7.29'/>",
  "server": "<rect width='13.33' height='5.33' x='1.33' y='1.33' rx='1.33' ry='1.33'/><rect width='13.33' height='5.33' x='1.33' y='9.33' rx='1.33' ry='1.33'/><line x1='4' x2='4.01' y1='4' y2='4'/><line x1='4' x2='4.01' y1='12' y2='12'/>",
  "slack": "<rect width='2' height='5.33' x='8.67' y='1.33' rx='1'/><path d='M 12.67 5.67 V 6.67 h 1 A 1 1 0 1 0 12.67 5.67'/><rect width='2' height='5.33' x='5.33' y='9.33' rx='1'/><path d='M 3.33 10.33 V 9.33 H 2.33 A 1 1 0 1 0 3.33 10.33'/><rect width='5.33' height='2' x='9.33' y='8.67' rx='1'/><path d='M 10.33 12.67 H 9.33 v 1 a 1 1 0 1 0 1 -1'/><rect width='5.33' height='2' x='1.33' y='5.33' rx='1'/><path d='M 5.67 3.33 H 6.67 V 2.33 A 1 1 0 1 0 5.67 3.33'/>",
  "sparkles": "<path d='M 7.34 1.88 a 0.67 0.67 0 0 1 1.31 0 l 0.7 3.71 a 1.33 1.33 0 0 0 1.06 1.06 l 3.71 0.7 a 0.67 0.67 0 0 1 0 1.31 l -3.71 0.7 a 1.33 1.33 0 0 0 -1.06 1.06 l -0.7 3.71 a 0.67 0.67 0 0 1 -1.31 0 l -0.7 -3.71 a 1.33 1.33 0 0 0 -1.06 -1.06 l -3.71 -0.7 a 0.67 0.67 0 0 1 0 -1.31 l 3.71 -0.7 a 1.33 1.33 0 0 0 1.06 -1.06 z'/><path d='M 13.33 1.33 v 2.67'/><path d='M 14.67 2.67 h -2.67'/><circle cx='2.67' cy='13.33' r='1.33'/>",
  "sticky-note": "<path d='M 14 6 a 1.6 1.6 0 0 0 -0.47 -1.14 l -2.39 -2.39 A 1.6 1.6 0 0 0 10 2 H 3.33 a 1.33 1.33 0 0 0 -1.33 1.33 v 9.33 a 1.33 1.33 0 0 0 1.33 1.33 h 9.33 a 1.33 1.33 0 0 0 1.33 -1.33 z'/><path d='M 10 2 v 3.33 a 0.67 0.67 0 0 0 0.67 0.67 h 3.33'/>",
  "sun": "<circle cx='8' cy='8' r='2.67'/><path d='M 8 1.33 v 1.33'/><path d='M 8 13.33 v 1.33'/><path d='m 3.29 3.29 0.94 0.94'/><path d='m 11.77 11.77 0.94 0.94'/><path d='M 1.33 8 h 1.33'/><path d='M 13.33 8 h 1.33'/><path d='m 4.23 11.77 -0.94 0.94'/><path d='m 12.71 3.29 -0.94 0.94'/>",
  "table": "<path d='M 8 2 v 12'/><rect width='12' height='12' x='2' y='2' rx='1.33'/><path d='M 2 6 h 12'/><path d='M 2 10 h 12'/>",
  "timer": "<line x1='6.67' x2='9.33' y1='1.33' y2='1.33'/><line x1='8' x2='10' y1='9.33' y2='7.33'/><circle cx='8' cy='9.33' r='5.33'/>",
  "trash-2": "<path d='M 6.67 7.33 v 4'/><path d='M 9.33 7.33 v 4'/><path d='M 12.67 4 v 9.33 a 1.33 1.33 0 0 1 -1.33 1.33 H 4.67 a 1.33 1.33 0 0 1 -1.33 -1.33 V 4'/><path d='M 2 4 h 12'/><path d='M 5.33 4 V 2.67 a 1.33 1.33 0 0 1 1.33 -1.33 h 2.67 a 1.33 1.33 0 0 1 1.33 1.33 v 1.33'/>",
  "triangle-alert": "<path d='m 14.49 12 -5.33 -9.33 a 1.33 1.33 0 0 0 -2.32 0 l -5.33 9.33 A 1.33 1.33 0 0 0 2.67 14 h 10.67 a 1.33 1.33 0 0 0 1.15 -2'/><path d='M 8 6 v 2.67'/><path d='M 8 11.33 h 0.01'/>",
  "video": "<path d='m 10.67 8.67 3.48 2.32 a 0.33 0.33 0 0 0 0.52 -0.28 V 5.25 a 0.33 0.33 0 0 0 -0.5 -0.29 L 10.67 7'/><rect x='1.33' y='4' width='9.33' height='8' rx='1.33'/>",
  "webhook": "<path d='M 12 11.32 h -3.99 c -0.73 0 -1.3 0.63 -1.65 1.27 A 2.67 2.67 0 0 1 1.33 11.33 c 0.01 -0.47 0.13 -0.93 0.38 -1.33'/><path d='m 4 11.33 2.09 -3.85 c 0.35 -0.65 0.07 -1.45 -0.33 -2.07 a 2.67 2.67 0 1 1 4.59 -2.71'/><path d='m 8 4 2.09 3.82 C 10.44 8.47 11.27 8.67 12 8.67 a 2.67 2.67 0 0 1 0 5.33'/>",
  "workflow": "<rect width='5.33' height='5.33' x='2' y='2' rx='1.33'/><path d='M 4.67 7.33 v 2.67 a 1.33 1.33 0 0 0 1.33 1.33 h 2.67'/><rect width='5.33' height='5.33' x='8.67' y='8.67' rx='1.33'/>",
  "x": "<path d='M 12 4 4 12'/><path d='m 4 4 8 8'/>",
  "zap": "<path d='M 2.67 9.33 a 0.67 0.67 0 0 1 -0.52 -1.09 l 6.6 -6.8 a 0.33 0.33 0 0 1 0.57 0.31 l -1.28 4.01 A 0.67 0.67 0 0 0 8.67 6.67 h 4.67 a 0.67 0.67 0 0 1 0.52 1.09 l -6.6 6.8 a 0.33 0.33 0 0 1 -0.57 -0.31 l 1.28 -4.01 A 0.67 0.67 0 0 0 7.33 9.33 z'/>",
};

function makeIcon(name: string) {
  return function DkGlyph({ size = 16, strokeWidth = 1.5, className, x, y }: IconProps) {
    return (
      <svg
        width={size}
        height={size}
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        className={className}
        x={x}
        y={y}
        dangerouslySetInnerHTML={{ __html: ICON_NODES[name] ?? "" }}
      />
    );
  };
}

export const ArrowLeft = makeIcon("arrow-left");
export const ArrowRight = makeIcon("arrow-right");
export const Bot = makeIcon("bot");
export const Braces = makeIcon("braces");
export const Calendar = makeIcon("calendar");
export const Check = makeIcon("check");
export const ChevronDown = makeIcon("chevron-down");
export const ChevronRight = makeIcon("chevron-right");
export const ClipboardCopy = makeIcon("clipboard-copy");
export const ClipboardPaste = makeIcon("clipboard-paste");
export const Clock = makeIcon("clock");
export const CloudDownload = makeIcon("cloud-download");
export const Code2 = makeIcon("code-2");
export const Copy = makeIcon("copy");
export const Database = makeIcon("database");
export const DatabaseZap = makeIcon("database-zap");
export const ExternalLink = makeIcon("external-link");
export const FileText = makeIcon("file-text");
export const Filter = makeIcon("filter");
export const FlaskConical = makeIcon("flask-conical");
export const Folder = makeIcon("folder");
export const GitBranch = makeIcon("git-branch");
export const Globe = makeIcon("globe");
export const Keyboard = makeIcon("keyboard");
export const KeyRound = makeIcon("key-round");
export const KeySquare = makeIcon("key-square");
export const Layers = makeIcon("layers");
export const ListPlus = makeIcon("list-plus");
export const ListRestart = makeIcon("list-restart");
export const ListTree = makeIcon("list-tree");
export const Loader2 = makeIcon("loader-circle");
export const LoaderCircle = makeIcon("loader-circle");
export const LogOut = makeIcon("log-out");
export const Mail = makeIcon("mail");
export const Maximize = makeIcon("maximize");
export const Menu = makeIcon("menu");
export const Minus = makeIcon("minus");
export const Moon = makeIcon("moon");
export const Play = makeIcon("play");
export const Plus = makeIcon("plus");
export const RefreshCw = makeIcon("refresh-cw");
export const RotateCcw = makeIcon("rotate-ccw");
export const RotateCw = makeIcon("rotate-cw");
export const Rss = makeIcon("rss");
export const ScrollText = makeIcon("scroll-text");
export const Search = makeIcon("search");
export const Send = makeIcon("send");
export const Server = makeIcon("server");
export const Slack = makeIcon("slack");
export const Sparkles = makeIcon("sparkles");
export const StickyNote = makeIcon("sticky-note");
export const Sun = makeIcon("sun");
export const Table = makeIcon("table");
export const Timer = makeIcon("timer");
export const Trash2 = makeIcon("trash-2");
export const TriangleAlert = makeIcon("triangle-alert");
export const Video = makeIcon("video");
export const Webhook = makeIcon("webhook");
export const Workflow = makeIcon("workflow");
export const X = makeIcon("x");
export const Zap = makeIcon("zap");
