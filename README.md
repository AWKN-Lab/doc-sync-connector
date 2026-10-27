# doc-sync-connector — paginated authorised-document sync that keeps the
# source and the permission mapping, and handles permission failures explicitly

This is the delivery for **ClawHunt task #1519** — "企业 Agent 系统 32/50 ｜
飞书与 WPS 连接器" (budget USD 20). The task asks for an authorised-document
sync interface that *retains the source and the permission mapping*, and its
acceptance criteria are: *提交配置、分页同步及权限失败处理示例* — submit the
configuration, the paginated sync, and examples of permission-failure
handling.

All three are in `evidence/run.log`, produced by one command.

---

## 1. What is in the box

```
connector/
  config.yaml      the configuration: two sources, two permission vocabularies,
                   the target vocabulary, page size, retry policy
  sync.py          the client: pagination, permission mapping, failure handling
mock/
  server.py        a local stand-in for the two sources, with the awkward cases
                   made reproducible (pagination, 403, a transient 503)
checks/
  test_connector.py 21 executable checks
evidence/
  run.sh           the reproducible run -> run.log
  run.log          captured transcript of the last run
  sync-report.json the report the run produced
```

## 2. The configuration

Two sources with **different permission vocabularies** and different auth
schemes, mapped into one target vocabulary:

| source | auth | source permission → target |
|---|---|---|
| `feishu-wiki` | `Authorization: Bearer …` | `full_access→full`, `edit→write`, `read→read`, `none→none` |
| `wps-docs` | `X-Wps-Token: …` | `owner→full`, `collaborator→write`, `viewer→read`, `restricted→none` |

The mapping is explicit in the file, so a vocabulary change is a configuration
edit and not a code change — and an unmapped permission is an **error**, not a
silent downgrade to "read".

## 3. The paginated sync

The client follows `next_page` until it is null, and the report records the
page each document came from, so a truncated sync is visible rather than
silent. From the captured run:

```
source         pages docs synced skipped failed retries
feishu-wiki        3    5      3       2      0       0
wps-docs           2    3      2       1      0       1
```

Per document, with the source and both permissions kept:

```
  id     source        src perm     target   status               page  sha256
  f-1    feishu-wiki   full_access  full     synced               1    27b02a5f214d
  f-2    feishu-wiki   read         -        skipped_permission   1    -
  f-3    feishu-wiki   edit         write    synced               2    b36006afcfdf
  f-4    feishu-wiki   none         -        skipped_permission   2    -
  f-5    feishu-wiki   full_access  full     synced               3    4675c6198961
  w-1    wps-docs      owner        full     synced               1    f455ca0a34b2
  w-2    wps-docs      restricted   -        skipped_permission   1    -
  w-3    wps-docs      collaborator write    synced               2    338c86596c11
```

## 4. The permission-failure examples

Three different failure shapes, each handled the way it should be, and each
recorded with its reason:

```
  f-2: the token may not read this document (HTTP 403): {"error": "permission_denied", …}
  f-4: source permission 'none' is blocked for this source; not fetched
  w-2: source permission 'restricted' is blocked for this source; not fetched
```

- **A blocked permission is never fetched.** The listing says the document is
  not shareable; the client skips it without a content request, and no
  content hash is recorded for it.
- **A document the token may not read (403) is skipped and reported** — not
  retried, not dropped silently, and it does not fail the whole sync.
- **A transient failure is retried.** The WPS listing answers 503 on the
  first call; the client backs off, retries, and the source syncs normally.
  The retry count is in the report (`retries: 1`).
- **Permission skips are counted separately from failures** — a run with three
  unreadable documents is not a failed run, and a run with a genuinely broken
  source is.

## 5. Verification record

`python3 checks/test_connector.py` — **21 checks, all green**, covering the
happy path (all pages read, permissions mapped, hashes recorded, the transient
failure retried) and the failure paths:

- a source that answers 200 with something that is **not a listing** is a
  failure, not "zero documents" — a broken sync must not be able to look like
  a clean one;
- a source permission with no mapping **stops the sync** and says so, rather
  than guessing a target permission;
- a source that never answers is reported as a failure;
- a source that rejects a request without credentials answers 401.

## 6. Reproduce it yourself

```bash
git clone https://github.com/AWKN-Lab/doc-sync-connector
cd doc-sync-connector
pip install pyyaml
python3 checks/test_connector.py     # 21 checks
bash evidence/run.sh                  # the config, the sync, the failure examples
```

The mock source runs locally, so nothing needs credentials or network access.

## 7. Scope and honest limits

- The endpoints and field names in `mock/server.py` are a **stand-in**, chosen
  to match the shape of a paginated document list with a per-document
  permission. The real Feishu and WPS endpoints differ; the client's contract
  with a source is the pagination envelope and the per-document permission,
  and adapting to a real API means changing `_request` and the listing
  envelope, not the sync logic.
- The tokens in `connector/config.yaml` are mock tokens for the local
  stand-in. Real credentials are exchanged through the platform after the NDA
  and are expected to come from the environment or a secret store, not from a
  committed file — the config's `auth` block is the only place they appear.
- No real document, space or account id appears anywhere in this package.
- Third-party model API, cloud resources and training compute are excluded
  from the budget, as the task note states.
