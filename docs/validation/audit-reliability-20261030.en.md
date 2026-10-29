# Audit fixes and validation record

[简体中文](audit-reliability-20261030.md) | **English**

This batch starts at `20800c456deef03cdd3aae903ba3fec14f4cc802` and combines 11 defects and four optimizations. IDs follow the isolated audit. The table identifies implementation behavior and fixed regressions; model reviews are not test results.

| ID | Behavior and boundary | Regression entry |
| --- | --- | --- |
| B01 | Automatic retention and manual preview respect run and project holds; corrupt values retain evidence. Each artifact rechecks holds inside its write transaction, serializing deletion with new holds. | `tests.test_scheduler_isolation`, `tests.test_storage_acceptance` |
| B02 | Catalog and call reservation share connection and credential-generation identity; prices freeze at each reservation and unknown values stay NULL. A sealed run configuration does not lock prices for future calls. | `tests.test_model_center` |
| B03 | Cancellation atomically revokes awaiting candidates and unclaimed delivery jobs. Approval and publication recheck the source task; published history is preserved. | `tests.test_task_lifecycle`, `tests.test_runtime_safety` |
| B04 | Archived projects reject retry and requirement revision without queue, revision or event writes. | `tests.test_task_lifecycle` |
| B05 | Retention safely removes run-owned read-only dependencies without following links to change or delete outside targets. | `tests.test_scheduler_isolation` |
| B06 | SQLite/IO failures saving diagnostic details do not replace successful model results. Admission state and core usage persistence keep their failure semantics. | `tests.test_operability` |
| B07 | No-check producers return an empty object. Legacy empty lists remain readable, but a simultaneous pass is downgraded to insufficient evidence in the read model. Invalid fields are isolated; other evidence and raw database bytes remain. | `tests.test_task_value_feedback`, `tests.test_comprehensive_audit` |
| B08 | Original-merge and later-mainline results use one attribution function. Historical observations without a SHA are not assigned by guesswork. | `tests.test_task_value_feedback` |
| B09 | Workflow queries share one read snapshot; nested snapshots do not reopen transactions. | `tests.test_task_value_feedback` |
| B10 | Retained usage data identifies its original scope and success time. Details and pagination reuse the successful request window that produced the visible report. | `tests/web_requests.cjs`, `tests/browser_usage.cjs` |
| B11 | Detail polling shows persistent failures and retry, backs off consecutive failures and preserves unsaved feedback drafts. | `tests/browser_task_detail.cjs` |
| O01 | Cache hits scan the full identity without a temporary copy; materialization verifies while copying. Shared directory locks protect active copies; eviction takes a nonblocking exclusive lock. | `tests.test_audit_environment`, `tests.test_reliability` |
| O02 | HTTP backup manifests hash 64 KiB chunks from the opened descriptor. | `tests.test_server_hardening` |
| O03 | Anthropic capability and request budgets are separate. Named Claude 3.0 models retain 4096 with specific rules first. Truncation repair is limited to one attempt per complete call and usage is retained for every request. | `tests.test_native_audit_contracts` |
| O04 | Price forms submit only fields changed since opening; clearing sends null. Two windows editing different fields preserve each other's saves. | `tests/browser_model_center.cjs` |

The batch also fixes current-version bilingual desktop feed notes and project-selector text overflow at phone widths.

## Local fixed validation

At the final code checkpoint, full Python ran 1266 tests including 10 skips, with all four groups exiting zero; JavaScript passed 375 tests, Rust passed 51, and all 36 Chrome browser scenarios passed. Documentation pairs and links, i18n/AST, API contract, CI selectors, audit ledger, plugin packaging and out-of-repository wheel installation passed. The historical audit ledger remains 123/53/25; this batch does not rewrite those historical statuses.

The local signing, receipt and install-authorization harness passed. It reports `acceptance_scope=staging_only`; a real signed N→N+1 application replacement was not executed, so this is not end-to-end automatic-updater acceptance. The release pipeline and local installation are verified separately at delivery.

## Same-load measurements and limits

The cache fixture uses 32 files totaling 8 MiB, three samples per path on the same host and warm filesystem. Median capture/restore was 51.19 ms, old cache hit 68.96 ms and new cache hit 55.36 ms. Full-tree copies per hit fell from three to two. The new cache still reads the full source identity and copies data; this measurement does not show it faster than the direct path.

For an isolated real HTTP manifest, a 17,072,128-byte backup produced a 536,020-byte Python tracemalloc peak and the correct hash. This measures Python tracked allocations, not RSS or a production-memory guarantee.

The synthetic ten-thousand-task read benchmark passed the 5000 ms and 8 MiB per-response budgets. These measurements do not cover production concurrency, cold disks, networking, real-model effectiveness or billing, and promise no production speedup or cost saving. Claude 4.x request budgets increased from the old policy while remaining capped by capability metadata; no real provider invocation validated them. No general classifier, voting or counterexample-generation calls were added.
