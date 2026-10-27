#!/usr/bin/env bash
# Reproducible evidence run for the document-sync connector.
#
#   bash evidence/run.sh
#
# Produces, in order, what the task asks for:
#   1. the configuration
#   2. a paginated sync against a local stand-in source
#   3. the permission-failure handling examples
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="${PYTHON:-python3}"
LOG="$HERE/run.log"
PORT=8098
REPORT="$HERE/sync-report.json"

: > "$LOG"
run() {
  echo "\$ $*" >> "$LOG"
  "$@" >> "$LOG" 2>&1
  local rc=$?
  echo "[exit $rc]" >> "$LOG"
  echo "" >> "$LOG"
  return $rc
}
step() { echo "── $* ──" >> "$LOG"; }

cd "$ROOT"
overall=0

step "environment"
echo "python: $($PY -V 2>&1)" >> "$LOG"
echo "pyyaml: $($PY -c 'import yaml; print(yaml.__version__)' 2>&1)" >> "$LOG"
echo "date:   $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG"
echo "" >> "$LOG"

step "1. the configuration"
run sed -n '1,40p' connector/config.yaml || overall=1

step "2. start the local stand-in source"
"$PY" mock/server.py --port "$PORT" > "$HERE/mock.log" 2>&1 &
MOCK_PID=$!
trap 'kill $MOCK_PID 2>/dev/null' EXIT
sleep 2
run cat "$HERE/mock.log" || overall=1

step "3. the paginated sync"
run "$PY" connector/sync.py --config connector/config.yaml --out "$REPORT" || overall=1

step "4. the sync report — pages, sources, permissions"
run "$PY" - "$REPORT" <<'PY' || overall=1
import json, sys
report = json.load(open(sys.argv[1]))
print("totals:", json.dumps(report["totals"], ensure_ascii=False))
print()
print(f"{'source':<14} {'pages':>5} {'docs':>4} {'synced':>6} {'skipped':>7} {'failed':>6} {'retries':>7}")
for s in report["sources"]:
    print(f"{s['name']:<14} {s['pages']:>5} {s['documents']:>4} {s['synced']:>6} "
          f"{s['skipped_permission']:>7} {s['failed']:>6} {s['retries']:>7}")
print()
print("per document:")
print(f"  {'id':<6} {'source':<13} {'src perm':<12} {'target':<8} {'status':<20} page  sha256")
for d in report["documents"]:
    sha = (d["content_sha256"] or "-")[:12]
    print(f"  {d['id']:<6} {d['source']:<13} {d['source_permission']:<12} "
          f"{str(d['target_permission'] or '-'):<8} {d['status']:<20} {d['page']}    {sha}")
print()
print("permission-failure examples:")
for d in report["documents"]:
    if d["status"] == "skipped_permission":
        print(f"  {d['id']}: {d['detail']}")
PY

echo "OVERALL: $([ $overall -eq 0 ] && echo PASS || echo FAIL)" >> "$LOG"
echo "log: $LOG" >> "$LOG"
tail -n 30 "$LOG"
exit $overall
