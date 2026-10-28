/* dapier's icon set: one drawing style family-wide — inline SVG on the
 * family's 24x24 grid rendered at 20px, stroke-width 1.8, round caps and
 * joins, no fills (dakit family.md). Paths are lucide's native geometry so
 * canvas, palette and chrome keep one icon language; regenerate with the
 * scratch converter, never by hand. */

import type { IconProps } from "./catalog";

const ICON_NODES: Record<string, string> = {
  "bot": "<path d='M 12 8 V 4 H 8'/><rect width='16' height='12' x='4' y='8' rx='2'/><path d='M 2 14 h 2'/><path d='M 20 14 h 2'/><path d='M 15 13 v 2'/><path d='M 9 13 v 2'/>",
  "braces": "<path d='M 8 3 H 7 a2 2 0 0 0 -2 2 v 5 a2 2 0 0 1 -2 2 2 2 0 0 1 2 2 v 5 c 0 1.09 0.9 2 2 2 h 1.01'/><path d='M 16 21 h 1.01 a2 2 0 0 0 2 -2 v -5 c 0 -1.09 0.9 -2 2 -2 a2 2 0 0 1 -2 -2 V 5 a2 2 0 0 0 -2 -2 h -1.01'/>",
  "calendar": "<path d='M 8 2 v 4'/><path d='M 16 2 v 4'/><rect width='18' height='18' x='3' y='4' rx='2'/><path d='M 3 10 h 18'/>",
  "chevron-down": "<path d='m 6 9 6 6 6 -6'/>",
  "chevron-right": "<path d='m 9 18 6 -6 -6 -6'/>",
  "clipboard-copy": "<rect width='8' height='4' x='8' y='2' rx='1.01' ry='1.01'/><path d='M 8 4 H 6 a2 2 0 0 0 -2 2 v 14 a2 2 0 0 0 2 2 h 12 a2 2 0 0 0 2 -2 v -2'/><path d='M 16 4 h 2 a2 2 0 0 1 2 2 v 4'/><path d='M 21 14 H 11'/><path d='m 15 10 -4 4 4 4'/>",
  "clipboard-paste": "<path d='M 11 14 h 10'/><path d='M 16 4 h 2 a2 2 0 0 1 2 2 v 1.35'/><path d='m 17 18 4 -4 -4 -4'/><path d='M 8 4 H 6 a2 2 0 0 0 -2 2 v 14 a2 2 0 0 0 2 2 h 12 a2 2 0 0 0 1.8 -1.11'/><rect x='8' y='2' width='8' height='4' rx='1.01'/>",
  "clock": "<path d='M 12 6 v 6 l 4 2'/><circle cx='12' cy='12' r='10'/>",
  "cloud-download": "<path d='M 12 13 v 8 l -4 -4'/><path d='m 12 21 4 -4'/><path d='M 4.4 15.27 A7 7 0 1 1 15.71 8 h 1.78 a4.5 4.5 0 0 1 2.43 8.28'/>",
  "code-2": "<path d='m 18 16 4 -4 -4 -4'/><path d='m 6 8 -4 4 4 4'/><path d='m 14.5 4 -5 16'/>",
  "copy": "<rect width='14' height='14' x='8' y='8' rx='2' ry='2'/><path d='M 4 16 c -1.09 0 -2 -0.9 -2 -2 V 4 c 0 -1.09 0.9 -2 2 -2 h 10 c 1.09 0 2 0.9 2 2'/>",
  "database-zap": "<ellipse cx='12' cy='5' rx='9' ry='3'/><path d='M 3 5 V 19 A9 3 0 0 0 15 21.84'/><path d='M 21 5 V 8'/><path d='M 21 12 L 18 17 H 22 L 19 22'/><path d='M 3 12 A9 3 0 0 0 14.6 14.87'/>",
  "file-text": "<path d='M 6 22 a2 2 0 0 1 -2 -2 V 4 a2 2 0 0 1 2 -2 h 8 a2.4 2.4 0 0 1 1.71 0.7 l 3.58 3.58 A2.4 2.4 0 0 1 20 8 v 12 a2 2 0 0 1 -2 2 z'/><path d='M 14 2 v 5 a1.01 1.01 0 0 0 1.01 1.01 h 5'/><path d='M 10 9 H 8'/><path d='M 16 13 H 8'/><path d='M 16 17 H 8'/>",
  "filter": "<path d='M 10 20 a1.01 1.01 0 0 0 0.55 0.9 l 2 1.01 A1.01 1.01 0 0 0 14 21 v -7 a2 2 0 0 1 0.51 -1.33 L 21.73 4.67 A1.01 1.01 0 0 0 21 3 H 3 a1.01 1.01 0 0 0 -0.73 1.67 l 7.23 8 A2 2 0 0 1 10 14 z'/>",
  "flask-conical": "<path d='M 14 2 v 6 a2 2 0 0 0 0.24 0.96 l 5.5 10.08 A2 2 0 0 1 18 22 H 6 a2 2 0 0 1 -1.75 -2.96 l 5.5 -10.08 A2 2 0 0 0 10 8 V 2'/><path d='M 6.45 15 h 11.1'/><path d='M 8.5 2 h 7'/>",
  "folder": "<path d='M 20 20 a2 2 0 0 0 2 -2 V 8 a2 2 0 0 0 -2 -2 h -7.9 a2 2 0 0 1 -1.69 -0.9 L 9.6 3.9 A2 2 0 0 0 7.94 3 H 4 a2 2 0 0 0 -2 2 v 13 a2 2 0 0 0 2 2 Z'/>",
  "git-branch": "<line x1='6' x2='6' y1='3' y2='15'/><circle cx='18' cy='6' r='3'/><circle cx='6' cy='18' r='3'/><path d='M 18 9 a9 9 0 0 1 -9 9'/>",
  "git-merge": "<circle cx='18' cy='18' r='3'/><circle cx='6' cy='6' r='3'/><path d='M 6 21 V 9 a9 9 0 0 0 9 9'/>",
  "globe": "<circle cx='12' cy='12' r='10'/><path d='M 12 2 a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0 -20'/><path d='M 2 12 h 20'/>",
  "keyboard": "<path d='M 10 8 h 0.01'/><path d='M 12 12 h 0.01'/><path d='M 14 8 h 0.01'/><path d='M 16 12 h 0.01'/><path d='M 18 8 h 0.01'/><path d='M 6 8 h 0.01'/><path d='M 7 16 h 10'/><path d='M 8 12 h 0.01'/><rect width='20' height='16' x='2' y='4' rx='2'/>",
  "layers": "<path d='M 12.83 2.17 a2 2 0 0 0 -1.67 0 L 2.59 6.07 a1.01 1.01 0 0 0 0 1.83 l 8.58 3.92 a2 2 0 0 0 1.67 0 l 8.58 -3.9 a1.01 1.01 0 0 0 0 -1.83 z'/><path d='M 2 12 a1.01 1.01 0 0 0 0.58 0.92 l 8.6 3.92 a2 2 0 0 0 1.65 0 l 8.58 -3.9 A1.01 1.01 0 0 0 22 12'/><path d='M 2 17 a1.01 1.01 0 0 0 0.58 0.92 l 8.6 3.92 a2 2 0 0 0 1.65 0 l 8.58 -3.9 A1.01 1.01 0 0 0 22 17'/>",
  "list-plus": "<path d='M 16 5 H 3'/><path d='M 11 12 H 3'/><path d='M 16 19 H 3'/><path d='M 18 9 v 6'/><path d='M 21 12 h -6'/>",
  "list-restart": "<path d='M 21 5 H 3'/><path d='M 7 12 H 3'/><path d='M 7 19 H 3'/><path d='M 12 18 a5 5 0 0 0 9 -3 4.5 4.5 0 0 0 -4.5 -4.5 c -1.33 0 -2.54 0.54 -3.41 1.41 L 11 14'/><path d='M 11 10 v 4 h 4'/>",
  "list-tree": "<path d='M 8 5 h 13'/><path d='M 13 12 h 8'/><path d='M 13 19 h 8'/><path d='M 3 10 a2 2 0 0 0 2 2 h 3'/><path d='M 3 5 v 12 a2 2 0 0 0 2 2 h 3'/>",
  "loader-circle": "<path d='M 21 12 a9 9 0 1 1 -6.23 -8.56'/>",
  "mail": "<path d='m 22 7 -8.98 5.73 a2 2 0 0 1 -2.01 0 L 2 7'/><rect x='2' y='4' width='20' height='16' rx='2'/>",
  "maximize": "<path d='M 8 3 H 5 a2 2 0 0 0 -2 2 v 3'/><path d='M 21 8 V 5 a2 2 0 0 0 -2 -2 h -3'/><path d='M 3 16 v 3 a2 2 0 0 0 2 2 h 3'/><path d='M 16 21 h 3 a2 2 0 0 0 2 -2 v -3'/>",
  "menu": "<path d='M 4 5 h 16'/><path d='M 4 12 h 16'/><path d='M 4 19 h 16'/>",
  "minus": "<path d='M 5 12 h 14'/>",
  "play": "<path d='M 5 5 a2 2 0 0 1 3.01 -1.72 l 12 7 a2 2 0 0 1 0 3.46 l -12 7 A2 2 0 0 1 5 19 z'/>",
  "plus": "<path d='M 5 12 h 14'/><path d='M 12 5 v 14'/>",
  "refresh-cw": "<path d='M 3 12 a9 9 0 0 1 9 -9 9.75 9.75 0 0 1 6.74 2.75 L 21 8'/><path d='M 21 3 v 5 h -5'/><path d='M 21 12 a9 9 0 0 1 -9 9 9.75 9.75 0 0 1 -6.74 -2.75 L 3 16'/><path d='M 8 16 H 3 v 5'/>",
  "rotate-ccw": "<path d='M 3 12 a9 9 0 1 0 9 -9 9.75 9.75 0 0 0 -6.74 2.75 L 3 8'/><path d='M 3 3 v 5 h 5'/>",
  "rotate-cw": "<path d='M 21 12 a9 9 0 1 1 -9 -9 c 2.52 0 4.94 1.01 6.74 2.75 L 21 8'/><path d='M 21 3 v 5 h -5'/>",
  "search": "<path d='m 21 21 -4.33 -4.33'/><circle cx='11' cy='11' r='8'/>",
  "send": "<path d='M 14.54 21.69 a0.49 0.49 0 0 0 0.93 -0.03 l 6.5 -19 a0.49 0.49 0 0 0 -0.63 -0.63 l -19 6.5 a0.49 0.49 0 0 0 -0.03 0.93 l 7.94 3.18 a2 2 0 0 1 1.11 1.11 z'/><path d='m 21.86 2.15 -10.94 10.94'/>",
  "slack": "<rect width='3' height='8' x='13' y='2' rx='1.5'/><path d='M 19 8.5 V 10 h 1.5 A1.5 1.5 0 1 0 19 8.5'/><rect width='3' height='8' x='8' y='14' rx='1.5'/><path d='M 5 15.5 V 14 H 3.5 A1.5 1.5 0 1 0 5 15.5'/><rect width='8' height='3' x='14' y='13' rx='1.5'/><path d='M 15.5 19 H 14 v 1.5 a1.5 1.5 0 1 0 1.5 -1.5'/><rect width='8' height='3' x='2' y='8' rx='1.5'/><path d='M 8.5 5 H 10 V 3.5 A1.5 1.5 0 1 0 8.5 5'/>",
  "sparkles": "<path d='M 11.01 2.82 a1.01 1.01 0 0 1 1.97 0 l 1.05 5.56 a2 2 0 0 0 1.59 1.59 l 5.56 1.05 a1.01 1.01 0 0 1 0 1.97 l -5.56 1.05 a2 2 0 0 0 -1.59 1.59 l -1.05 5.56 a1.01 1.01 0 0 1 -1.97 0 l -1.05 -5.56 a2 2 0 0 0 -1.59 -1.59 l -5.56 -1.05 a1.01 1.01 0 0 1 0 -1.97 l 5.56 -1.05 a2 2 0 0 0 1.59 -1.59 z'/><path d='M 20 2 v 4'/><path d='M 22 4 h -4'/><circle cx='4' cy='20' r='2'/>",
  "sticky-note": "<path d='M 21 9 a2.4 2.4 0 0 0 -0.7 -1.71 l -3.58 -3.58 A2.4 2.4 0 0 0 15 3 H 5 a2 2 0 0 0 -2 2 v 14 a2 2 0 0 0 2 2 h 14 a2 2 0 0 0 2 -2 z'/><path d='M 15 3 v 5 a1.01 1.01 0 0 0 1.01 1.01 h 5'/>",
  "table": "<path d='M 12 3 v 18'/><rect width='18' height='18' x='3' y='3' rx='2'/><path d='M 3 9 h 18'/><path d='M 3 15 h 18'/>",
  "timer": "<line x1='10' x2='14' y1='2' y2='2'/><line x1='12' x2='15' y1='14' y2='11'/><circle cx='12' cy='14' r='8'/>",
  "trash-2": "<path d='M 10 11 v 6'/><path d='M 14 11 v 6'/><path d='M 19 6 v 14 a2 2 0 0 1 -2 2 H 7 a2 2 0 0 1 -2 -2 V 6'/><path d='M 3 6 h 18'/><path d='M 8 6 V 4 a2 2 0 0 1 2 -2 h 4 a2 2 0 0 1 2 2 v 2'/>",
  "triangle-alert": "<path d='m 21.73 18 -8 -14 a2 2 0 0 0 -3.48 0 l -8 14 A2 2 0 0 0 4 21 h 16 a2 2 0 0 0 1.72 -3'/><path d='M 12 9 v 4'/><path d='M 12 17 h 0.01'/>",
  "video": "<path d='m 16 13 5.22 3.48 a0.49 0.49 0 0 0 0.78 -0.42 V 7.88 a0.49 0.49 0 0 0 -0.75 -0.43 L 16 10.5'/><rect x='2' y='6' width='14' height='12' rx='2'/>",
  "webhook": "<path d='M 18 16.98 h -5.99 c -1.09 0 -1.95 0.95 -2.47 1.91 A4 4 0 0 1 2 17 c 0.01 -0.7 0.2 -1.4 0.57 -2'/><path d='m 6 17 3.13 -5.78 c 0.52 -0.98 0.11 -2.17 -0.49 -3.1 a4 4 0 1 1 6.88 -4.06'/><path d='m 12 6 3.13 5.73 C 15.66 12.71 16.91 13 18 13 a4 4 0 0 1 0 8'/>",
  "workflow": "<rect width='8' height='8' x='3' y='3' rx='2'/><path d='M 7 11 v 4 a2 2 0 0 0 2 2 h 4'/><rect width='8' height='8' x='13' y='13' rx='2'/>",
  "x": "<path d='M 18 6 6 18'/><path d='m 6 6 12 12'/>",
  "zap": "<path d='M 4 14 a1.01 1.01 0 0 1 -0.78 -1.64 l 9.9 -10.2 a0.49 0.49 0 0 1 0.85 0.46 l -1.92 6.01 A1.01 1.01 0 0 0 13 10 h 7 a1.01 1.01 0 0 1 0.78 1.64 l -9.9 10.2 a0.49 0.49 0 0 1 -0.85 -0.46 l 1.92 -6.01 A1.01 1.01 0 0 0 11 14 z'/>",
};

function makeIcon(name: string) {
  return function DkGlyph({ size = 20, strokeWidth = 1.8, className, x, y }: IconProps) {
    return (
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
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

export const Bot = makeIcon("bot");
export const Braces = makeIcon("braces");
export const Calendar = makeIcon("calendar");
export const ChevronDown = makeIcon("chevron-down");
export const ChevronRight = makeIcon("chevron-right");
export const ClipboardCopy = makeIcon("clipboard-copy");
export const ClipboardPaste = makeIcon("clipboard-paste");
export const Clock = makeIcon("clock");
export const CloudDownload = makeIcon("cloud-download");
export const Code2 = makeIcon("code-2");
export const Copy = makeIcon("copy");
export const DatabaseZap = makeIcon("database-zap");
export const FileText = makeIcon("file-text");
export const Filter = makeIcon("filter");
export const FlaskConical = makeIcon("flask-conical");
export const Folder = makeIcon("folder");
export const GitBranch = makeIcon("git-branch");
export const GitMerge = makeIcon("git-merge");
export const Globe = makeIcon("globe");
export const Keyboard = makeIcon("keyboard");
export const Layers = makeIcon("layers");
export const ListPlus = makeIcon("list-plus");
export const ListRestart = makeIcon("list-restart");
export const ListTree = makeIcon("list-tree");
export const Loader2 = makeIcon("loader-circle");
export const Mail = makeIcon("mail");
export const Maximize = makeIcon("maximize");
export const Menu = makeIcon("menu");
export const Minus = makeIcon("minus");
export const Play = makeIcon("play");
export const Plus = makeIcon("plus");
export const RefreshCw = makeIcon("refresh-cw");
export const RotateCcw = makeIcon("rotate-ccw");
export const RotateCw = makeIcon("rotate-cw");
export const Search = makeIcon("search");
export const Send = makeIcon("send");
export const Slack = makeIcon("slack");
export const Sparkles = makeIcon("sparkles");
export const StickyNote = makeIcon("sticky-note");
export const Table = makeIcon("table");
export const Timer = makeIcon("timer");
export const Trash2 = makeIcon("trash-2");
export const TriangleAlert = makeIcon("triangle-alert");
export const Video = makeIcon("video");
export const Webhook = makeIcon("webhook");
export const Workflow = makeIcon("workflow");
export const X = makeIcon("x");
export const Zap = makeIcon("zap");
