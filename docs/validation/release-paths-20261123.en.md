# Measured release reuse paths

[简体中文](release-paths-20261123.md) | **English**

The read-only query covered completed `release.yml` and `release-verify.yml` runs in `Haoog/Adaptive-RSI` created between `2026-10-23T13:33:57Z` and `2026-11-23T16:01:00Z`. The window starts when the reuse job was introduced. It returned **479 runs and 488 attempts**. The release workflow accounted for 425 runs and 425 attempts. Exact-commit verification accounted for 54 runs and 63 attempts. The 376 release attempts without an identifiable publish path include preparation failures and runs that did not need to publish.

## Release paths

Times are in seconds. Ranges show the minimum and maximum.

| Path | Runs / attempts | Key job queue median (range) | Package verification or rebuild median (range) | `prepare` start to `publish` completion median (range) |
| --- | ---: | --- | --- | --- |
| Successful reuse | 45 / 45, 42 distinct workflow SHAs | `reuse` 3 (1–603), n=45 | `reuse` 49 (36–92), n=45 | 80 (53–691), n=45, 44 publish jobs succeeded and 1 failed |
| Rebuild with no reusable artifact | 3 / 3, 2 distinct workflow SHAs | `build` 2.5 (2–3), n=2, 1 missing | `prepare` completion to `build` completion 834 (774–894), n=2, 1 missing | 857 (798–916), n=2, 1 missing |
| Rebuild after revalidation failed | 0 / 0 | No samples | No samples | No samples |
| Rebuild after reuse was cancelled | 1 / 1 | No runner timing for `reuse`, `build` 3 | `reuse` job timestamps span 480 seconds. `reuse` completion to `build` completion 1284 | 1794, publish job succeeded and attempt conclusion was `cancelled` |

- Successful reuse had median provenance verification of 23 seconds, DMG and checksum revalidation of 4 seconds, and ZIP/wheel smoke of 7 seconds, n=45 each. In `45209940650/1`, `reuse` queued for 309 seconds and ran for 92 seconds. [Attempt](https://github.com/Haoog/Adaptive-RSI/actions/runs/45209940650/attempts/1)
- `build` determined the critical path in both successful rebuilds without a reusable artifact. Desktop build and checks took 505 and 415 seconds, and install and upgrade smoke took 8 seconds in each. The third `build` was cancelled without a publish job executing. A skipped `reuse` followed by a rebuild identifies the path. The jobs do not give the specific reason the artifact was unavailable. [First rebuild](https://github.com/Haoog/Adaptive-RSI/actions/runs/40712540811/attempts/1) · [Second rebuild](https://github.com/Haoog/Adaptive-RSI/actions/runs/40801795824/attempts/1) · [Incomplete rebuild](https://github.com/Haoog/Adaptive-RSI/actions/runs/40709279448/attempts/1)
- The cancelled reuse had no individual revalidation step timings. Its `build` ran for 1280 seconds, with 453 seconds for desktop build and checks and 7 seconds for install and upgrade smoke. The window contains no rebuild triggered by a failed or timed-out `reuse`. The cancellation is counted separately. [Rebuild after cancellation](https://github.com/Haoog/Adaptive-RSI/actions/runs/40534641447/attempts/1)

## Exact-commit verification

The recheck covered all 54 verification runs and 63 attempts in the window. Two of the nine reruns started a new `gate`. The other seven inherited a previous `gate`, leaving both entry timings blank. All 425 release attempts in the window were first attempts.

| Verification attempts | Total | Measured / missing | Attempt origin to `gate` start median (range) | Workflow creation to `gate` start median (range) |
| --- | ---: | ---: | ---: | ---: |
| All | 63 | 56 / 7 | 4 (3–410) | 4 (3–1311) |
| First attempts | 54 | 54 / 0 | 3.5 (3–410) | 3.5 (3–410) |
| Reruns | 9 | 2 / 7 | 4.5 (4–5) | 847 (383–1311) |

In `40934405014/2`, the attempt wait was 5 seconds and the elapsed time since workflow creation was 1311 seconds. In `41005242867/2`, those times were 4 and 383 seconds. The earlier script used `created_at` directly from the exact-attempt endpoint and reported 3 seconds for each rerun. Its median across all measured verification attempts was also 3 seconds. The raw `created_at` in the two rerun records was 2 seconds and 1 second after `run_started_at`, respectively. [First rerun](https://github.com/Haoog/Adaptive-RSI/actions/runs/40934405014/attempts/2) · [Second rerun](https://github.com/Haoog/Adaptive-RSI/actions/runs/41005242867/attempts/2)

Of the 63 attempts, 58 started a new `verify` job and 5 inherited an earlier job. For the new jobs, the median queue time was 3 seconds (2–304) and the median execution time was 612.5 seconds (209–1353), each with n=56 and 2 missing. Median desktop build and checks took 194 seconds (59–659) and install and upgrade smoke took 8 seconds (5–33), each with n=53 and 5 missing. The `verify` job in `44228968296/2` had already finished before that attempt started, so its timing appears only in attempt 1. [First attempt](https://github.com/Haoog/Adaptive-RSI/actions/runs/44228968296/attempts/1) · [Selective rerun](https://github.com/Haoog/Adaptive-RSI/actions/runs/44228968296/attempts/2)

Attempt `45206714950/1` verified candidate `e37e550807fae08d7acffd5024729ce955b2e2c4` for version PR #523. The workflow SHA was `39c1229f33eeda27435fd8718364097d7d6e2b5b`. The `verify` job queued for 2 seconds and ran for 359 seconds. Desktop build and checks took 265 seconds and install and upgrade smoke took 10 seconds. The post-merge publish attempt `45209940650/1` used workflow SHA `77b10646a2fac8069a411e4f1c938e6636b9e66b`. [Verification attempt](https://github.com/Haoog/Adaptive-RSI/actions/runs/45206714950/attempts/1) · [Version PR](https://github.com/Haoog/Adaptive-RSI/pull/523) · [Release attempt](https://github.com/Haoog/Adaptive-RSI/actions/runs/45209940650/attempts/1)

## Repeating the measurement

```sh
python3 -B scripts/release_path_metrics.py --created 2026-10-23T13:33:57Z..2026-11-23T16:01:00Z
python3 -B scripts/release_path_metrics.py --run 40934405014/2 --run 41005242867/2 --run 44228968296/2 --run 45209940650/1
```

The command uses `gh api --method GET` to read completed runs, attempts, and jobs and emits JSON on stdout. The time range filters run creation. Use `--run` to pin an exact attempt. The JSON preserves each attempt's workflow SHA, job URLs, conclusions, and timing. It records the candidate SHA separately when the run title includes it. Older verification runs without that title evidence leave the candidate SHA blank.

`attempt_to_entry_seconds` measures the time from the attempt origin to the start of a new `gate` or `prepare` job and can include workflow concurrency and scheduling. It uses the same origin as the exact-attempt timing in item #16. The first attempt starts at workflow creation. A rerun uses `run_started_at` from its exact attempt record when that timestamp follows workflow creation and all known earlier attempt starts. A missing or unverified origin, or an entry start before the origin, leaves the metric `null`. `run_to_entry_seconds` measures the cumulative time from `run_created_at` on the first attempt to the new entry job. `attempt_created_at` and `attempt_started_at` preserve the raw endpoint values, and `attempt_origin_at` records the selected origin.

Job queue time is creation to start for a job with an assigned runner. A job's timestamp span may include unallocated runner time. Execution medians use jobs with assigned runners. Skipped steps and missing timestamps remain `null`. A selective rerun can expose a new job ID with inherited `started_at` and `completed_at`. Jobs ending before the new attempt started are flagged as inherited. Their conclusions are retained and their timing is excluded. The rebuild critical path ends at the last completed parallel prerequisite and starts at the completion of `prepare` or `reuse`, whichever triggered the rebuild. The complete critical path to publication requires evidence that the publish job ran. These historical samples span different workflows, runners, and commits, so the observed difference between reuse and rebuild does not isolate the effect of reuse.
