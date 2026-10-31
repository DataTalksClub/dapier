"""Implementation of the `dapier gdocs` commands (Google Docs read/write).

A short-lived Google access token is minted through the agent API
(`/api/agent/token`, same binding check as dtoken.py) and used in-process only.
It is never printed, written to disk or put in the environment.
"""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from .. import api

__all__ = ["gdocs_get", "gdocs_append", "gdocs_replace", "parse_doc_id"]

DEFAULT_REF = "drive datatalks"
DEFAULT_AGENT = "admin-os"
DOCS = "https://docs.googleapis.com/v1/documents/"


def parse_doc_id(value):
    m = re.search(r"/document/d/([A-Za-z0-9_-]+)", value or "")
    return m.group(1) if m else (value or "").strip()


def _token(api_url, ref, agent, debug=False):
    q = urllib.parse.urlencode({"ref": ref, "agent": agent})
    view = api.call(api_url, "GET", f"/api/agent/connections/resolve?{q}", debug=debug)
    cid = str(view.get("connection_id") or "").strip()
    if not cid:
        raise api.ApiError(f"Could not resolve connection {ref!r}")
    conn = api.call(api_url, "GET", f"/api/agent/connections/{urllib.parse.quote(cid, safe='')}?"
                    + urllib.parse.urlencode({"agent": agent}), debug=debug)
    expected = {v for v in (conn.get("expected_account_id"), conn.get("verified_account_id")) if v}
    tok = api.call(api_url, "POST", "/api/agent/token",
                   body={"connection_id": cid, "agent": agent}, debug=debug)
    if not expected or tok.get("provider_account_id") not in expected:
        raise api.ApiError(f"Connection {cid}: provider account not verified/matching; refusing token")
    access = str(tok.get("access_token") or "")
    if not access:
        raise api.ApiError("Dapier returned no access token")
    return access


def _google(token, method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        raise api.ApiError(f"Google Docs {method} failed: HTTP {exc.code} {detail}",
                           status=exc.code) from None


def _text(doc):
    out = []
    for el in doc.get("body", {}).get("content", []):
        for pe in (el.get("paragraph") or {}).get("elements", []):
            out.append((pe.get("textRun") or {}).get("content", ""))
    return "".join(out)


def _read_text(text, file):
    if text is not None:
        return text
    if file and file != "-":
        with open(file, encoding="utf-8") as fh:
            return fh.read()
    return sys.stdin.read()


def gdocs_get(api_url, doc, ref=DEFAULT_REF, agent=DEFAULT_AGENT, as_json=False, debug=False):
    tok = _token(api_url, ref, agent, debug)
    d = _google(tok, "GET", DOCS + parse_doc_id(doc))
    if as_json:
        print(json.dumps({"id": d.get("documentId"), "title": d.get("title"),
                          "revision_id": d.get("revisionId"), "text": _text(d)}, ensure_ascii=False))
    else:
        print(f"# {d.get('title')}  (revision {d.get('revisionId')})")
        print(_text(d))
    return 0


def _batch(api_url, doc, requests, ref, agent, debug):
    tok = _token(api_url, ref, agent, debug)
    did = parse_doc_id(doc)
    r = _google(tok, "POST", DOCS + did + ":batchUpdate", {"requests": requests})
    rev = (r.get("writeControl") or {}).get("requiredRevisionId")
    print(f"ok: {did} updated, revision {rev}")
    return 0


def gdocs_append(api_url, doc, text=None, file=None, ref=DEFAULT_REF, agent=DEFAULT_AGENT, debug=False):
    body = _read_text(text, file)
    if not body.strip():
        raise api.ApiError("Nothing to append (empty text)")
    if not body.endswith("\n"):
        body += "\n"
    return _batch(api_url, doc, [{"insertText": {"endOfSegmentLocation": {}, "text": body}}],
                  ref, agent, debug)


def gdocs_replace(api_url, doc, find, text=None, file=None, ref=DEFAULT_REF, agent=DEFAULT_AGENT,
                  debug=False):
    body = _read_text(text, file)
    return _batch(api_url, doc, [{"replaceAllText": {
        "containsText": {"text": find, "matchCase": True}, "replaceText": body}}], ref, agent, debug)
