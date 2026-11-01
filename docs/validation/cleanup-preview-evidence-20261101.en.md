# Cleanup preview real-remote verification evidence (audit-200 #176, 2026-11-01 data)

**简体中文** | [English](cleanup-preview-evidence-20261101.en.md)

## Purpose and boundary

audit-200 ledger item #176 "Preview stale remote branch cleanup" was previously partial: fixture tests already
covered `delivery.branch_cleanup_preview` (`adaptive_rsi/delivery.py:176`), but the read-only preview had never
been run against the authorized repository. This round closes that gap. **The whole round is read-only**: the
preview itself only issues two GET requests; additionally, `git ls-remote` snapshots of remote refs were taken
before and after the preview for comparison. No remote ref was deleted, modified, or created.

## Read-only design of the function (code walkthrough)

`branch_cleanup_preview` issues exactly two GET requests via `release.api` (`adaptive_rsi/release.py:39`):

- `repos/<repo>/branches?per_page=100` (first page of branches)
- `repos/<repo>/pulls?state=all&per_page=100` (first page of all-state pulls)

The function provides no deletion endpoint; the result always carries `deletion_performed: false` plus
`candidates`, `protected` (with reasons), and `complete` (whether both pages are under 100 rows). An incomplete
inventory or unverified ownership always lands in protected; nothing is left for the caller to delete.

## Remote state at verification time (2026-11-01T20:45Z)

- 129 remote branches (`git ls-remote --heads origin`); the branches API first page returns 100 rows, so the
  page is incomplete.
- 147 pull requests (state=all); the pulls API first page returns 100 rows, so the page is incomplete.
- 17 open PRs among them (#126, #132–#147).
- Branches matching the managed pattern `feat|fix/rsi-*`: **0**.
- `main` is not within the branches API first page of 100 rows (alphabetically truncated inside `chore/`/`fix/`
  prefixes).

## How the real preview was run, and results

Run method (the read-only-wrapped probe used this round):

```sh
python3 -B probe_cleanup_preview.py   # wraps release.api: only GET passes; every call is logged; non-GET raises
```

Both parameterizations ran once each, with identical results:

| Invocation | complete | deletion_performed | candidates | protected |
| --- | --- | --- | --- | --- |
| `branch_cleanup_preview('Haoog/Adaptive-RSI')` | false | false | 0 | 100 (all `not_managed`) |
| `branch_cleanup_preview('Haoog/Adaptive-RSI', recovery_refs=[])` | false | false | 0 | 100 (all `not_managed`) |

Each run made exactly two API calls (branches, pulls), all GET, matching the fixture assertions. Protected-list
samples: `chore/batch-20261101-rate-limit-crash-matrix`, `chore/release-v0.4.0-bb1374a98664`,
`fix/health-check-https-only`, all protected as "not managed".

### Consistency with the local fixture behavior

- `test_full_cleanup_page_yields_no_candidates` (any page at 100 rows → `complete=false`, zero candidates): the
  real run saw 100 rows on both first pages (129 branches / 147 PRs remotely) and produced the same
  `complete=false` with zero candidates — consistent.
- `test_cleanup_preview_protects_open_pr_recovery_and_unknown_branches`: the real remote currently has no
  branches matching the managed pattern, so the `open_pr` / `protected_or_recovery` / `unverified_ownership`
  paths had no real data to trigger this round. This does not contradict the fixture: the classification logic
  those tests cover is the same code path exercised by the real run; the remote just happens to hold no branches
  falling into those categories today.
- `main` is absent from the first page; even on a complete listing it would fall under `not_managed` protection
  and never be listed as a candidate.

## Read-only proof

- The sha256 of the `git ls-remote --heads origin` snapshot before the preview and after it are identical
  (`88de2296cd3e9c5918dcc45d3d697047f0230201129dabe6d5bc2d93c646dc2a`, 129 rows).
- The probe wrapper observed no non-GET request; the preview function contains no deletion endpoint.

## Conclusion and suggested status

- Both acceptance clauses are verified: the preview performs no deletion (always reports
  `deletion_performed: false`, and no deletion endpoint exists); branches referenced by open PRs are protected
  (this round's remote has no managed branches, so the open-PR protection path is covered by the fixture; real
  data did not trigger that category).
- Suggested ledger change for #176: partial → done. The function is implemented with offline contract tests, and
  this round adds real read-only preview evidence. The open-PR, recovery-inventory, and unknown-ownership
  protection categories are covered by fixtures; the real remote currently holds no managed branches that would
  fall into them.
