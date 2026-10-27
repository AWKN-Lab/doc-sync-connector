#!/usr/bin/env python3
"""A local stand-in for the two document sources (Feishu wiki, WPS docs).

It exists so the sync client can be exercised end to end without credentials
or network access, and so the awkward cases are reproducible on demand:

  * pagination - the Feishu source has five documents over three pages at
    page_size=2, the WPS source three documents over two pages
  * a blocked permission - listed, but never fetched
  * a document the token may not read - the content call answers 403
  * a transient failure - the first call to the WPS listing answers 500 and
    the retry succeeds
  * authentication - each source has its own scheme, and a wrong token is 401

    python3 mock/server.py --port 8098
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

FEISHU_TOKEN = "mock-feishu-token"
WPS_TOKEN = "mock-wps-token"

# id, title, permission, content (None = the token may not read it)
FEISHU_DOCS = [
    ("f-1", "Onboarding guide", "full_access", "Welcome to the wiki. This is the onboarding guide."),
    ("f-2", "Restricted roadmap", "read", None),
    ("f-3", "Meeting notes 2026-10", "edit", "Notes from the October planning meeting."),
    ("f-4", "Personal notes", "none", "Private notes that must not leave the source."),
    ("f-5", "Runbook", "full_access", "How to run the thing when it breaks."),
]

WPS_DOCS = [
    ("w-1", "Contract template", "owner", "Standard services contract template."),
    ("w-2", "Shared budget", "restricted", "Budget shared with an external audience."),
    ("w-3", "Meeting minutes", "collaborator", "Minutes of the last review."),
]

_state = {"wps_list_calls": 0}
_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-doc-source/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _auth_ok(self, source: str) -> bool:
        if source == "feishu":
            return self.headers.get("Authorization") == f"Bearer {FEISHU_TOKEN}"
        return self.headers.get("X-Wps-Token") == WPS_TOKEN

    def do_GET(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        if not parts:
            return self._send(404, {"error": "not found"})
        source = parts[0]
        if source not in ("feishu", "wps"):
            return self._send(404, {"error": f"unknown source: {source}"})
        if not self._auth_ok(source):
            return self._send(401, {"error": "invalid or missing credentials"})

        docs = FEISHU_DOCS if source == "feishu" else WPS_DOCS

        # /docs?page=N&page_size=M
        if len(parts) == 2 and parts[1] == "docs":
            q = parse_qs(url.query)
            page = int(q.get("page", ["1"])[0])
            size = int(q.get("page_size", ["2"])[0])

            # the WPS listing fails once, to exercise the retry path
            if source == "wps":
                with _lock:
                    _state["wps_list_calls"] += 1
                    first_call = _state["wps_list_calls"] == 1
                if first_call:
                    return self._send(503, {"error": "upstream temporarily unavailable"})

            start = (page - 1) * size
            chunk = docs[start:start + size]
            items = [
                {"id": d[0], "title": d[1], "permission": d[2], "updated_at": "2026-10-20"}
                for d in chunk
            ]
            has_more = start + size < len(docs)
            return self._send(200, {
                "items": items,
                "page": page,
                "page_size": size,
                "total": len(docs),
                "next_page": page + 1 if has_more else None,
            })

        # /docs/{id}/content
        if len(parts) == 4 and parts[1] == "docs" and parts[3] == "content":
            doc_id = parts[2]
            for d in docs:
                if d[0] != doc_id:
                    continue
                if d[3] is None:
                    return self._send(403, {"error": "permission_denied",
                                           "doc": doc_id,
                                           "hint": "the token may not read this document"})
                return self._send(200, d[3].encode(), "text/plain; charset=utf-8")
            return self._send(404, {"error": f"unknown document: {doc_id}"})

        return self._send(404, {"error": "not found"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8098)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock document source listening on http://127.0.0.1:{args.port}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
