# Classification and conclusions for the unclassified flaky records (2026-10-31 data)

**简体中文** | [English](flaky-unclassified-triage-20261031.md)

## Data source and verification

PROC-02 of audit-20261031 (`~/rsi-ops/audit-20261031.md`, with the detailed ledger on the user's Mac under `/Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-audit-20261031/`) claims that `.ci/test-durations.failures.json` holds 21 unclassified records, "all macOS, random seeds, no repeat offenders", matching the known open `worker exit -11` issue. This round checked all three claims. The conclusion: **the 21-record data source was not located; "all macOS" and "matches exit -11" are both unsupported by verifiable data**.

Verification steps:

- The only failure records findable on the server are two batches, both Linux: 7 records (the `wt-bug06` worktree's `.ci/test-durations.failures.json`, 2026-10-31 09:37–09:39 UTC) and 36 records (4 orphaned `tmp*.failures.json` receipts under `TMPDIR`, 11:57:54–11:58:35 UTC). Together 43 records; no file holds exactly 21.
- All 190 runs of `release-verify.yml` and `release.yml` were scanned on GitHub, and every one of the 45 `timing-*` artifacts existing at scan time (2026-10-31) was downloaded (upload steps at `release-verify.yml:62-68` and `release.yml:136-142`): the artifacts contain only `test-durations.json`, never a failures file. Passing selector groups produce no records; a suite terminated by a segfault before adjudication likewise produces none (mechanism below; receipts already written are unaffected by a later worker crash). The data source may still sit on the user's Mac side — the audit cites a relative path — but this round could not confirm it.
- One possible explanation for "21" is a sum of the orphaned receipts (the 4 files hold 15/10/6/5 records, so several combinations reach 21), but there is no evidence the audit ever read those files, so no conclusion is drawn.

## How records are produced (suite-level correspondence, not mutual exclusivity)

A failure record is written by `scripts/test_worker.py:278-292` only when **a class-level suite finishes and unittest adjudicates failures**: the worker's `suites()` (`scripts/test_worker.py:50-71`) first splits module selectors into class-level groups, and the receipt's `selector` field keeps the original module selector; receipts accumulate in one file and the parent collects them regardless of the worker's exit code (`scripts/test_parallel.py:152-159,180-188`). Therefore:

- a suite terminated by a segfault (SIGSEGV / exit -11) before adjudication writes no record for its own group;
- but a worker that crashes in a **later** suite or during shutdown does not erase receipts it wrote earlier — those receipts are aggregated as usual. So "records exist" does not prove "that worker was healthy throughout", and "a worker exited -11" does not imply "none of its groups left failure records";
- when the parent process ends before its workers, receipts remain as orphaned files under `TMPDIR` and never reach `.ci/`. The 4 `tmp*.failures.json` files found this round are exactly such leftovers.

Conclusion: the relation between records and crashes must be matched at suite level and time window. The exit -11 exclusion below rests on platform, timing, and "the CI exit -11 group has no corresponding records" — not on "records and crashes are mutually exclusive by mechanism".

## Classification

### Batch one: round 203 full run (7 records, 2026-10-31 09:37–09:39 UTC)

| Selector | Records | Failed tests | Time (UTC) |
|---|---|---|---|
| tests.test_audit_environment | 1 | 1 | 09:37:27 |
| tests.test_controls | 1 | 5 | 09:37:38 |
| tests.test_experience | 1 | 1 | 09:38:00 |
| tests.test_reliability | 1 | 3 | 09:38:25 |
| tests.test_delivery_loop | 1 | 1 | 09:38:26 |
| tests.test_runtime | 1 | 2 | 09:38:32 |
| tests.test_workflow_experience | 1 | 1 | 09:39:04 |

Total: 7 records, 7 modules, 14 failed test cases (13 after deduplicating one parameterized repeat).

### Batch two: the 11:58 cascade (36 orphaned receipts, 11:57:54–11:58:35 UTC, ~42 seconds)

| Selector | Records | Failed tests |
|---|---|---|
| tests.test_backend_hardening | 5 | 17 |
| tests.test_delivery_readiness_audit | 4 | 19 |
| tests.test_model_center | 3 | 59 |
| tests.test_audit_round6 | 2 | 26 |
| tests.test_credential_lock_scope | 2 | 6 |
| tests.test_data_hardening | 2 | 11 |
| tests.test_discovery_hardening | 2 | 29 |
| tests.test_controls | 1 | 8 |
| tests.test_operability | 1 | 19 |
| tests.test_cleanup_recovery | 1 | 11 |
| tests.test_delivery_loop | 1 | 33 |
| 12 further modules (1 record each) | 12 | 49 |

Total: 36 records, 23 modules, 287 failed test cases (286 after deduplication). Multiple records for the same module do not imply repeated execution: the worker splits selectors by test class and each failing class writes its own receipt while keeping the module selector — `test_backend_hardening` has exactly 5 test classes and `test_model_center` exactly 3, matching the record counts one to one. The batch is therefore consistent with a single execution producing 1–5 failing-class receipts per module (revision note: the first draft inferred repeated execution from "same module, several records, disjoint failure sets"; corrected after review against the source. The round-213 parallelism attribution, which relies on the time window and module coverage, is unaffected).

### Dimension summary

| Dimension | Batch one (7 records) | Batch two (36 records) |
|---|---|---|
| Platform | Linux-7.0.0-1014-aws-aarch64-with-glibc2.39 (this server, 2 vCPU / 2 GB) | same |
| Python | 3.14.9 | same |
| hash_seed | all `random` (no `PYTHONHASHSEED` in the environment; not a reproducible-seed setup) | same |
| quarantined | all false | same |
| Repeat offenders (same test in several records) | none | none |

Across both batches, only 7 test cases (6 test methods; the controls case `test_deferred_and_rejected_never_execute_and_keep_value_evidence` has the two parameterizations `decision='deferred'/'rejected'`) appear in more than one record when counted by full test ID, and all of them are the intersection of the two batches — i.e. cases that already failed deterministically on this machine at 09:37 and failed again at 11:58, not random "green usually, red occasionally" flakiness.

## Relation to worker exit -11: no match

The real evidence for the known `worker exit -11` lives in CI: release-verify run [41203204855](https://github.com/Haoog/Adaptive-RSI/actions/runs/41203204855) (PR #119 @f4b29a3, macOS runner), tests job, `[group 1] exit -11`, at the moment the 60-second stall watchdog fired a faulthandler dump (stack frames truncated at `test_worker.py line 0 in _frames`), with no test assertion failures anywhere in the log.

The 43 records above do not match it:

1. Different platform: CI's exit -11 happened on a macOS runner; both batches are Linux on this server.
2. Different record nature: all 43 records were produced by unittest adjudication and each carries concrete failed test cases; the CI exit -11 group (group 1) left no record with failed test cases (no assertion failures in the log). Per the mechanism above, the segfaulting suite terminates before adjudication and writes nothing, while earlier suites' receipts from the same worker are unaffected — matched at suite level, the two sets of evidence do not overlap.
3. Different time: the two server batches occurred 2026-10-31 09:37 and 11:58 UTC; CI run 41203204855 started 12:55 UTC.
4. Different distribution: batch one is unrelated to the test-runner recursion incident record and matches the machine's known stable failure set recorded in the adaptive-rsi-loop operating notes (test_audit_environment / test_controls / test_experience / test_delivery_loop / test_runtime / test_reliability / test_workflow_experience); batch two spans 23 modules bursting within seconds — the shape of parallel resource interference, not a single-worker segfault.

## Attribution of the cascade

Round 213 (started 2026-10-31 11:52, task bug20-audit-size) launched the full `python3 -B scripts/test_parallel.py --durations .ci/test-durations.json` in the background (run.log records session `proc_c1baa3de3660`) and ended the round without waiting for it. The 36 records of 11:57:54–11:58:35 fall inside that background run, overlapping the loop's next round (round 214, started 11:58:27). On a 2 vCPU / 2 GB machine, four full-run workers plus the loop's own processes stacked up and caused the sweeping non-deterministic failures; after round 213's worktree was reclaimed, the parent could not collect its receipts, leaving the 4 orphaned files. This attribution is consistent with the time window and run logs but is not a controlled reproduction.

## Conclusions

1. None of the 43 verifiable unclassified records on the server is genuine flakiness: batch one reproduces the machine's known environment-bound failures; batch two is resource interference between a full run and the loop's rounds.
2. The three PROC-02 claims (21 records, all macOS, matches exit -11) all disagree with the verifiable data; the origin of the "21" remains unlocated and needs confirmation from the user side (Mac working copy).
3. The investigation target for `worker exit -11` should remain the watchdog faulthandler dump path on CI macOS runners (the existing conclusion for run 41203204855); the verifiable failures.json records contain no failed-test-case evidence corresponding to it.
4. Process lesson: never run the full `test_parallel.py` concurrently with loop rounds on this 2 vCPU / 2 GB machine; a round must wait for or terminate the background full run it started and record the final state in its SUMMARY.

This round is read-only analysis plus a new document: no test-runner behavior was changed, the existing incident record (`test-runner-recursion-20261028.md`) was left untouched, and no guardrail was relaxed.
