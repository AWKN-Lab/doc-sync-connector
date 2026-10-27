#!/usr/bin/env python3
"""Authorised-document sync: paginate every source, keep the source and the
permission mapping, and handle permission failures explicitly.

The three things this client is careful about, in order of how often they go
wrong in the field:

1. **Pagination.** A source that returns a next_page is not finished. The
   client follows it until it is null, and the report records how many pages
   each source contributed, so a truncated sync is visible.
2. **Permission mapping.** A document's source-side permission is carried
   through the source's own vocabulary into the target vocabulary. An
   unmapped permission is an error, not a silent downgrade to "read".
3. **Permission failures.** A document the token may not read is *skipped
   and reported*, not retried and not silently dropped. A source that fails
   for a transient reason is retried with backoff, and only becomes a hard
   failure once the retries are exhausted.

    python3 connector/sync.py --config connector/config.yaml --out work/sync-report.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("this tool needs PyYAML: pip install pyyaml\n")
    raise SystemExit(2)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SyncError(Exception):
    """A condition that must stop the sync."""


@dataclass
class Document:
    id: str
    title: str
    source: str
    source_permission: str
    target_permission: str | None
    status: str                      # synced | skipped_permission | failed
    content_sha256: str | None = None
    bytes: int | None = None
    page: int | None = None
    detail: str = ""


@dataclass
class SourceResult:
    name: str
    pages: int = 0
    documents: int = 0
    synced: int = 0
    skipped_permission: int = 0
    failed: int = 0
    retries: int = 0
    errors: list = field(default_factory=list)


class SourceClient:
    """One source, with its own base url, auth scheme and permission map."""

    def __init__(self, spec: dict, target_permissions: dict, sync_cfg: dict):
        self.name = spec["name"]
        self.type = spec["type"]
        self.base_url = spec["base_url"].rstrip("/")
        self.auth = spec.get("auth") or {}
        self.permission_map = spec.get("permission_map") or {}
        self.blocked = set(spec.get("blocked_permissions") or [])
        self.target_permissions = target_permissions
        self.sync_cfg = sync_cfg

    def _headers(self) -> dict:
        mode = self.auth.get("mode")
        if mode == "bearer":
            return {"Authorization": f"Bearer {self.auth.get('token', '')}"}
        if mode == "header":
            return {self.auth.get("header", "X-Token"): self.auth.get("token", "")}
        return {}

    def _request(self, url: str) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.sync_cfg.get("request_timeout_s", 10)) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except urllib.error.URLError as e:
            return 0, str(e.reason).encode()

    def _get_with_retry(self, url: str, result: SourceResult) -> tuple[int, bytes]:
        """Retry transient failures with backoff; give up on anything else."""
        max_retries = int(self.sync_cfg.get("max_retries", 3))
        backoff = float(self.sync_cfg.get("retry_backoff_s", 0.2))
        status, body = self._request(url)
        attempt = 0
        while status >= 500 and attempt < max_retries:
            attempt += 1
            result.retries += 1
            time.sleep(backoff * attempt)
            status, body = self._request(url)
        return status, body

    def list_page(self, page: int, page_size: int, result: SourceResult) -> dict:
        url = f"{self.base_url}/docs?page={page}&page_size={page_size}"
        status, body = self._get_with_retry(url, result)
        if status != 200:
            raise SyncError(f"{self.name}: listing page {page} failed with HTTP {status}: "
                            f"{body[:200].decode('utf-8', 'replace')}")
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise SyncError(f"{self.name}: listing page {page} did not return JSON: {exc}")
        # A 200 that is not a listing is not "zero documents" — it is a source
        # that broke its contract. Reporting it as an empty source would let a
        # broken sync look like a clean one.
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise SyncError(
                f"{self.name}: listing page {page} returned a response that is not a listing "
                f"(expected an 'items' array, got {type(payload.get('items')).__name__ if isinstance(payload, dict) else type(payload).__name__}): "
                f"{body[:200].decode('utf-8', 'replace')}"
            )
        return payload

    def fetch_content(self, doc_id: str, result: SourceResult) -> tuple[int, bytes]:
        url = f"{self.base_url}/docs/{doc_id}/content"
        return self._get_with_retry(url, result)

    def map_permission(self, source_permission: str) -> str:
        if source_permission in self.permission_map:
            return self.permission_map[source_permission]
        raise SyncError(
            f"{self.name}: source permission '{source_permission}' is not in the "
            f"permission_map — refusing to guess a target permission"
        )


def sync_source(client: SourceClient, sync_cfg: dict, result: SourceResult) -> list:
    page_size = int(sync_cfg.get("page_size", 2))
    page = 1
    documents: list[Document] = []

    while True:
        payload = client.list_page(page, page_size, result)
        result.pages += 1
        items = payload.get("items") or []
        result.documents += len(items)

        for item in items:
            doc = Document(
                id=str(item.get("id")),
                title=item.get("title", ""),
                source=client.name,
                source_permission=item.get("permission", ""),
                target_permission=None,
                status="failed",
                page=page,
            )
            # a blocked permission is known before we even try to read it
            if doc.source_permission in client.blocked:
                doc.status = "skipped_permission"
                doc.detail = (f"source permission '{doc.source_permission}' is blocked for this "
                              f"source; not fetched")
                result.skipped_permission += 1
                documents.append(doc)
                continue

            status, body = client.fetch_content(doc.id, result)
            if status == 403:
                doc.status = "skipped_permission"
                doc.detail = f"the token may not read this document (HTTP 403): {body[:120].decode('utf-8', 'replace')}"
                result.skipped_permission += 1
                documents.append(doc)
                continue
            if status != 200:
                doc.status = "failed"
                doc.detail = f"content fetch failed with HTTP {status} after {result.retries} retries"
                result.failed += 1
                result.errors.append({"document": doc.id, "error": doc.detail})
                documents.append(doc)
                continue

            doc.target_permission = client.map_permission(doc.source_permission)
            if doc.target_permission not in sync_cfg.get("_target_permissions", []):
                raise SyncError(
                    f"{client.name}: mapped target permission '{doc.target_permission}' is not in "
                    f"the target vocabulary"
                )
            doc.content_sha256 = hashlib.sha256(body).hexdigest()
            doc.bytes = len(body)
            doc.status = "synced"
            result.synced += 1
            documents.append(doc)

        next_page = payload.get("next_page")
        if not next_page:
            break
        page = int(next_page)

    return documents


def run(config_path: str, out_path: str) -> int:
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    sync_cfg = dict(config.get("sync") or {})
    target_permissions = config.get("target_permissions") or {}
    sync_cfg["_target_permissions"] = list(target_permissions.keys())

    results: list[SourceResult] = []
    all_documents: list[Document] = []

    for spec in config.get("sources") or []:
        client = SourceClient(spec, target_permissions, sync_cfg)
        result = SourceResult(name=client.name)
        results.append(result)
        try:
            all_documents.extend(sync_source(client, sync_cfg, result))
        except SyncError as exc:
            result.failed += 1
            result.errors.append({"error": str(exc)})
            print(f"error: {exc}", file=sys.stderr)

    report = {
        "generated_at": now(),
        "config": str(config_path),
        "sources": [asdict(r) for r in results],
        "documents": [asdict(d) for d in all_documents],
        "totals": {
            "sources": len(results),
            "pages": sum(r.pages for r in results),
            "documents": sum(r.documents for r in results),
            "synced": sum(r.synced for r in results),
            "skipped_permission": sum(r.skipped_permission for r in results),
            "failed": sum(r.failed for r in results),
            "retries": sum(r.retries for r in results),
        },
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for r in results:
        print(f"{r.name}: {r.pages} page(s), {r.documents} document(s), "
              f"synced={r.synced} skipped_permission={r.skipped_permission} "
              f"failed={r.failed} retries={r.retries}")
    t = report["totals"]
    print(f"totals: {t['documents']} document(s) over {t['pages']} page(s); "
          f"synced={t['synced']} skipped_permission={t['skipped_permission']} failed={t['failed']}")
    print(f"report: {out}")

    return 1 if t["failed"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="connector/config.yaml")
    ap.add_argument("--out", default="work/sync-report.json")
    args = ap.parse_args()
    return run(args.config, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
