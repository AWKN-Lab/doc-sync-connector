#!/usr/bin/env python3
"""Executable tests for the document-sync connector.

The task asks for a configuration, a paginated sync, and examples of handling
permission failures. These tests cover all three, and they cover the failure
paths: an unmapped permission must stop the sync rather than quietly
downgrade a document to "read", and a source that never becomes readable must
be reported as a failure.

    python3 checks/test_connector.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable
PORT = 8098
BASE = f"http://127.0.0.1:{PORT}"


class T:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, name, ok, detail=""):
        if ok:
            print(f"  [PASS] {name}")
            self.passed += 1
        else:
            print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
            self.failed += 1
        return ok


def start_server() -> subprocess.Popen:
    proc = subprocess.Popen(
        [PY, str(ROOT / "mock/server.py"), "--port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for _ in range(40):
        time.sleep(0.25)
        try:
            urllib.request.urlopen(f"{BASE}/feishu/docs?page=1&page_size=1", timeout=2)
            return proc
        except urllib.error.HTTPError:
            return proc          # the server is up, it just rejected the auth
        except Exception:
            continue
    raise SystemExit("mock server did not start")


def run_sync(config: str, out: str, expect_ok: bool = True):
    proc = subprocess.run(
        [PY, str(ROOT / "connector/sync.py"), "--config", config, "--out", out],
        capture_output=True, text=True, cwd=ROOT,
    )
    return proc


def main() -> int:
    t = T()
    tmp = Path(tempfile.mkdtemp(prefix="connector-test-"))
    server = None
    try:
        server = start_server()

        # ------------------------------------------------ the happy path
        report_path = tmp / "sync-report.json"
        proc = run_sync("connector/config.yaml", str(report_path))
        t.check("the sync completes without a hard failure", proc.returncode == 0,
                (proc.stdout + proc.stderr)[-300:])
        if not report_path.exists():
            print(f"\n== {t.passed} passed, {t.failed} failed")
            return 1
        report = json.loads(report_path.read_text(encoding="utf-8"))
        totals = report["totals"]

        # ------------------------------------------------ pagination
        t.check("every page of every source was read", totals["pages"] == 5,
                f"pages={totals['pages']} (expected 3 feishu + 2 wps)")
        t.check("every listed document was seen", totals["documents"] == 8,
                f"documents={totals['documents']}")
        t.check("the report records the page each document came from",
                all(d.get("page") for d in report["documents"]))
        by_source = {s["name"]: s for s in report["sources"]}
        t.check("each source reports its own page count",
                by_source["feishu-wiki"]["pages"] == 3 and by_source["wps-docs"]["pages"] == 2,
                str({k: v["pages"] for k, v in by_source.items()}))

        # ------------------------------------------------ permission mapping
        docs = {d["id"]: d for d in report["documents"]}
        t.check("the Feishu permission vocabulary is mapped to the target",
                docs["f-1"]["target_permission"] == "full", str(docs["f-1"]))
        t.check("the WPS permission vocabulary is mapped too",
                docs["w-1"]["target_permission"] == "full", str(docs["w-1"]))
        t.check("the source permission is kept alongside the mapped one",
                docs["f-3"]["source_permission"] == "edit" and docs["f-3"]["target_permission"] == "write",
                str(docs["f-3"]))
        t.check("the source of every document is recorded",
                all(d["source"] in ("feishu-wiki", "wps-docs") for d in report["documents"]))

        # ------------------------------------------------ permission failures
        t.check("a blocked permission is skipped without being fetched",
                docs["f-4"]["status"] == "skipped_permission" and docs["f-4"]["bytes"] is None,
                str(docs["f-4"]))
        t.check("a document the token may not read is skipped and reported",
                docs["f-2"]["status"] == "skipped_permission", str(docs["f-2"]))
        t.check("the reason for the skip is recorded",
                "403" in docs["f-2"]["detail"], docs["f-2"]["detail"])
        t.check("permission skips are counted separately from failures",
                totals["skipped_permission"] == 3 and totals["failed"] == 0,
                f"skipped={totals['skipped_permission']} failed={totals['failed']}")
        t.check("a skipped document carries no content hash",
                docs["f-2"]["content_sha256"] is None)

        # ------------------------------------------------ transient failures
        t.check("the transient 503 was retried, not reported as a failure",
                totals["retries"] >= 1, f"retries={totals['retries']}")
        t.check("the WPS source still synced after the retry",
                by_source["wps-docs"]["synced"] == 2, str(by_source["wps-docs"]))
        t.check("every synced document carries a content hash",
                all(d["content_sha256"] for d in report["documents"] if d["status"] == "synced"))

        # ------------------------------------------------ authentication
        try:
            urllib.request.urlopen(f"{BASE}/feishu/docs?page=1", timeout=5)
            unauth = 200
        except urllib.error.HTTPError as e:
            unauth = e.code
        t.check("a source rejects a request without credentials", unauth == 401, f"HTTP {unauth}")

        # ------------------------------------------------ negative: unmapped permission
        bad_config = tmp / "bad-permission.yaml"
        cfg = (ROOT / "connector/config.yaml").read_text(encoding="utf-8")
        bad_config.write_text(
            cfg.replace("      full_access: full\n", ""), encoding="utf-8")
        proc = run_sync(str(bad_config), str(tmp / "bad-report.json"), expect_ok=False)
        t.check("an unmapped source permission stops the sync instead of guessing",
                proc.returncode != 0, "the sync accepted an unmapped permission")
        t.check("the refusal names the offending permission",
                "permission_map" in (proc.stdout + proc.stderr),
                (proc.stdout + proc.stderr)[-200:])

        # ------------------------------------------------ negative: unreachable source
        dead_cfg = tmp / "dead.yaml"
        dead_cfg.write_text(cfg.replace("http://127.0.0.1:8098", "http://127.0.0.1:8097"),
                            encoding="utf-8")
        proc = run_sync(str(dead_cfg), str(tmp / "dead-report.json"), expect_ok=False)
        t.check("a source that never answers is reported as a failure",
                proc.returncode != 0, "an unreachable source did not fail the sync")

    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except Exception:
                server.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n== {t.passed} passed, {t.failed} failed")
    return 1 if t.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
