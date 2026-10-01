/* Markdown → HTML for agent output, safe by construction: every text run is
   HTML-escaped before any tag is generated, dynamic attributes go through
   escapeAttr, and link/image URLs must be http(s) or mailto — so the only
   markup in the result is what this module emits. Covers what agents actually
   write: headings, lists, tables, code fences, blockquotes, emphasis, links,
   bare URLs and images. */
import { escapeHtml } from './format.js';

const SAFE_URL = /^(https?:\/\/|mailto:)/i;

function escapeAttr(value) {
  return escapeHtml(value).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function decodeEntities(value) {
  return value.replace(/&(?:amp|lt|gt|quot|#39);/g, entity => ({ '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'" })[entity]);
}

/* Bare URLs arrive as escaped text; trim the punctuation markdown leaves
   hanging (including escaped quotes and unbalanced ") and re-escape for the
   attribute. Returns null for non-http(s) schemes. */
function bareAnchor(escapedUrl) {
  let url = escapedUrl.replace(/(?:&gt;|&quot;|&#39;)+$/, '');
  while (/[.,;:!?)'"\]]$/.test(url)) url = url.slice(0, -1);
  const opens = (url.match(/\(/g) || []).length, closes = (url.match(/\)/g) || []).length;
  if (closes > opens) url = url.replace(/\)+$/, '');
  const raw = decodeEntities(url);
  if (!/^https?:\/\//i.test(raw)) return null;
  return { href: escapeAttr(raw), label: url };
}

function emphasis(text) {
  return text
    .replace(/\*\*\*(?=\S)([\s\S]*?\S)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/___(?=\S)([\s\S]*?\S)___/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^\w\\])__(?=\S)([\s\S]*?\S)__(?!\w)/g, '$1<strong>$2</strong>')
    .replace(/(^|[^*\\])\*(?=\S)([^*\n]*?\S)\*(?!\*)/g, '$1<em>$2</em>')
    .replace(/(^|[^\w\\])_(?=\S)([^_\n]*?\S)_(?!\w)/g, '$1<em>$2</em>')
    .replace(/~~(?=\S)([\s\S]*?\S)~~/g, '<del>$1</del>');
}

function inline(text) {
  const codes = [], links = [], autos = [];
  let out = String(text).replace(/(`+)([\s\S]*?[^`])\1(?!`)/g, (_, ticks, code) => {
    codes.push(code);
    return `\u0001${codes.length - 1}\u0001`;
  });
  out = out.replace(/(!?)\[([^\]\n]*)\]\(\s*(<[^>\n]*>|\S+?)(?:\s+(?:"[^"\n]*"|'[^'\n]*'))?\s*\)/g, (_, bang, label, href) => {
    links.push({ image: bang === '!', label, href });
    return `\u0002${links.length - 1}\u0002`;
  });
  out = escapeHtml(out);
  out = out.replace(/https?:\/\/[^\s<>"']+/gi, match => {
    const anchor = bareAnchor(match);
    if (!anchor) return match;
    autos.push(`<a href="${anchor.href}" target="_blank" rel="noopener noreferrer">${anchor.label}</a>`);
    return `\u0003${autos.length - 1}\u0003`;
  });
  out = emphasis(out);
  out = out.replace(/\n/g, '<br>');
  out = out.replace(/\u0001(\d+)\u0001/g, (_, n) => `<code>${escapeHtml(codes[n])}</code>`);
  out = out.replace(/\u0002(\d+)\u0002/g, (_, n) => {
    const link = links[n];
    if (!link) return '';
    const href = link.href.replace(/^<|>$/g, '');
    if (!SAFE_URL.test(href)) return escapeHtml(link.label);
    if (link.image) return `<img src="${escapeAttr(href)}" alt="${escapeAttr(link.label)}" loading="lazy">`;
    return `<a href="${escapeAttr(href)}" target="_blank" rel="noopener noreferrer">${emphasis(escapeHtml(link.label))}</a>`;
  });
  out = out.replace(/\u0003(\d+)\u0003/g, (_, n) => autos[n] || '');
  return out;
}

function listItems(lines, start, base) {
  const ordered = /^\d{1,9}[.)]\s/.test(String(lines[start] || '').slice(base));
  const tag = ordered ? 'ol' : 'ul';
  const items = [];
  let i = start;
  while (i < lines.length) {
    const match = String(lines[i] || '').slice(base).match(/^(?:[-*+]|\d{1,9}[.)])\s+(.*)$/);
    if (!match) break;
    let content = match[1], nested = '';
    i++;
    for (;;) {
      const line = i < lines.length ? String(lines[i] || '').slice(base) : '';
      if (!line.trim()) {
        const next = i + 1 < lines.length ? String(lines[i + 1] || '').slice(base) : '';
        if (/^\s*(?:[-*+]|\d{1,9}[.)])\s+/.test(next)) { content += '\n'; i++; continue; }
        break;
      }
      if (/^(?:[-*+]|\d{1,9}[.)])\s+/.test(line)) break;
      if (/^\s+(?:[-*+]|\d{1,9}[.)])\s+/.test(line)) {
        const deeper = [];
        while (i < lines.length) {
          const shifted = String(lines[i] || '').slice(base);
          if (/^\s+(?:[-*+]|\d{1,9}[.)])\s+/.test(shifted) || (/^\s{2,}\S/.test(shifted) && deeper.length)) {
            deeper.push(shifted);
            i++;
          } else break;
        }
        /* Re-base on the deepest block's common indent so markers sit at
           column 0 for the recursive pass while deeper levels keep theirs. */
        const indent = Math.min(...deeper.map(l => l.length - l.replace(/^ +/, '').length));
        nested += listItems(deeper.map(l => l.slice(indent)), 0, 0)[0];
        continue;
      }
      if (/^\s{2,}\S/.test(line)) { content += '\n' + line.trim(); i++; continue; }
      break;
    }
    items.push(`<li>${inline(content)}${nested}</li>`);
  }
  return [`<${tag}>${items.join('')}</${tag}>`, i];
}

function splitRow(row) {
  return row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim());
}

function cellAttr(aligns, index) {
  const value = aligns[index] || '';
  return value ? ` style="text-align:${value}"` : '';
}

function blocks(lines, base) {
  const out = [];
  const at = n => String(lines[n] || '').slice(base);
  let i = 0;
  while (i < lines.length) {
    const line = at(i);
    if (!line.trim()) { i++; continue; }
    const fence = line.match(/^ {0,3}(`{3,}|~{3,})\s*(\S*)\s*$/);
    if (fence) {
      const closing = fence[1][0] === '`' ? /^ {0,3}`{3,}\s*$/ : /^ {0,3}~{3,}\s*$/;
      const pad = line.length - line.replace(/^ +/, '').length;
      const body = [];
      i++;
      while (i < lines.length && !closing.test(at(i))) { body.push(at(i).slice(pad).replace(/\s+$/, '')); i++; }
      if (i < lines.length) i++; /* consume the closing fence; an unclosed fence swallows the rest */
      const lang = /^[A-Za-z0-9_-]+$/.test(fence[2] || '') ? fence[2] : '';
      out.push(`<pre><code${lang ? ` class="language-${lang}"` : ''}>${escapeHtml(body.join('\n'))}</code></pre>`);
      continue;
    }
    const heading = line.match(/^ {0,3}(#{1,6})\s+(\S.*?)\s*#*\s*$/);
    if (heading) {
      const level = heading[1].length;
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      i++;
      continue;
    }
    if (/^ {0,3}(?:[-*_] *){3,}$/.test(line)) { out.push('<hr>'); i++; continue; }
    if (/^ {0,3}>/.test(line)) {
      const quoted = [];
      while (i < lines.length && /^ {0,3}>/.test(at(i))) { quoted.push(at(i).replace(/^ {0,3}> ?/, '')); i++; }
      out.push(`<blockquote>${blocks(quoted, 0)}</blockquote>`);
      continue;
    }
    if (/^(?:[-*+]|\d{1,9}[.)])\s+/.test(line)) {
      const [html, next] = listItems(lines, i, base);
      out.push(html);
      i = next;
      continue;
    }
    if (line.includes('|') && i + 1 < lines.length && /^\s*\|? *:?-+:? *\|/.test(at(i + 1))) {
      const aligns = splitRow(at(i + 1)).map(cell =>
        /^:-+:$/.test(cell) ? 'center' : /^-+:$/.test(cell) ? 'right' : /^:-+/.test(cell) ? 'left' : '');
      const header = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && at(i).includes('|') && at(i).trim()) { rows.push(splitRow(at(i))); i++; }
      out.push(`<table><thead><tr>${header.map((cell, c) => `<th${cellAttr(aligns, c)}>${inline(cell)}</th>`).join('')}</tr></thead>` +
        `<tbody>${rows.map(cells => `<tr>${header.map((_, c) => `<td${cellAttr(aligns, c)}>${inline(cells[c] || '')}</td>`).join('')}</tr>`).join('')}</tbody></table>`);
      continue;
    }
    const paragraph = [line];
    i++;
    while (i < lines.length) {
      const next = at(i);
      if (!next.trim() || /^ {0,3}(?:#{1,6}\s|`{3,}|~{3,}|>)/.test(next) ||
        /^(?:[-*+]|\d{1,9}[.)])\s+/.test(next) || /^ {0,3}(?:[-*_] *){3,}$/.test(next)) break;
      paragraph.push(next);
      i++;
    }
    out.push(`<p>${inline(paragraph.join('\n'))}</p>`);
  }
  return out.join('');
}

export function renderMarkdown(text) {
  return text == null ? '' : blocks(String(text).replace(/\r\n?/g, '\n').split('\n'), 0);
}
