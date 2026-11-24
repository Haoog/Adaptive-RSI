# Offline scheduling and cost evaluation

[简体中文](policy-evaluation.md) | **English**

Run these commands from the repository root. Both modes print JSON and accept `--output` for a new file. They read only the supplied local inputs and run without starting the app, contacting a model, or updating scheduling.

## Replay a legitimate task queue

```sh
python3 -B scripts/benchmark_history.py \
  --policy-replay tests/fixtures/policy_queue_trace.json \
  --workers 1 --planning-slots 1 --old-after 30 --old-quota 1 \
  --urgent-quota 1 --quota-window 100
```

The command runs the same input under serial planning, isolated planning, old task opportunities, and cross-project user urgency. Each change is compared independently with the serial-planning baseline. Replay reuses the app's task and project ranking, accounts for the last 24 hours of worker time by project weight, and models exclusive writers for each repository. Separate planning slots can start a plan while a writer is active in that repository. At claim time, an intervening mainline update makes the plan stale. The task plans again in its writer slot, and the report records the rework seconds. Replay performs no actual planning or Git writes.

See the [fixed queue fixture](../tests/fixtures/policy_queue_trace.json) for format 1. To replay observations, supply a redacted file with `redacted_history` provenance. Tasks provide `id`, `project`, `source`, `created`, `ready_at`, `plan_seconds`, and `work_seconds`. Times are Unix seconds and durations are seconds. `projects` maps project IDs to redacted repository IDs and optional weights. Use the same repository ID for aliases of the same repository. `mainline_changes` must cover every included repository. An empty array indicates that mainline was observed and did not move. Each task is continuously eligible from `ready_at`, or supplies half-open `eligible_windows` with a final open interval ending in `null` so the replay can drain. Exclude periods of ineligibility such as pauses, non-working hours, unsatisfied dependencies, and retry backoff. An admission after a retry can use a distinct redacted ID. Incomplete eligibility or mainline evidence supports only explicitly stated scenarios.

`claimable_wait_seconds` is the time spent eligible and unclaimed, including waits for capacity or fair allocation. Tasks with at least `--old-after` eligible seconds may receive up to `--old-quota` promotions per project in a rolling window. `urgency` retains its normal ranking semantics. Set `urgency_origin` to `user` or `system` only with explicit evidence, and use `unknown` or omit the field when uncertain. User-urgent tasks at the head of their project queues may receive up to `--urgent-quota` cross-project promotions across all projects in each rolling window. Set a quota to `0` to disable that experiment. The report includes changes in waiting time per task, total/median/maximum ordinary-task waiting, and waiting, promotions and weight-adjusted worker time for each project.

Replay starts with an empty queue and fairness ledger. Task durations, mainline updates, and eligibility windows are fixed inputs. The simulation leaves out the Issue synchronization overlap exception, live resource pressure, and feedback from changed claim order to eligibility or mainline. Both fixed and redacted traces support only observation and scenario comparison for the supplied inputs.

## Report stage history and automatic task cost ranges

```sh
python3 -B scripts/benchmark_history.py \
  --policy-history /path/to/offline-snapshot.sqlite3 \
  --start 1700000000 --end 1702592000 --project REDACTED_PROJECT_ID
```

Supply a redacted, standalone SQLite snapshot previously taken using a safe backup of the app database. Keep the snapshot free of `-wal`, `-shm`, and `-journal` sidecars. Avoid a live database or a raw copy of its main file while the app is writing. The reader performs no migrations. The snapshot needs the fields used by the current usage report. `--project` is optional and the time window is `[start, end)`.

- `stage_history` groups by project, task source, role, provider, model, reasoning effort, call unit, and saved tariffs. One observation covers the calls in that group for a run. The report counts runs, distinct tasks, fully priced samples, and quality coverage. A pass requires both evaluation and review to pass. Evidence of either gate failing counts as a failure, and insufficient evidence is counted separately. Run-level quality may appear in multiple role or model groups. Cost and quality each require evidence from at least eight distinct tasks. Comparisons are descriptive. Task complexity, retries, and model selection can all affect the groups.
- `automatic_task_cost` includes all attempts and recorded calls for terminal `system_` tasks, including priced failed calls. It groups by project, source, and the full set of recorded routes and tariffs. To contribute to the range, a task must have been created and reached its snapshot terminal state inside the window, with all attempts inside the same window. P25, median, and P75 in USD require at least eight fully priced tasks. Window-boundary or unfinished tasks, missing calls, unknown usage, and unknown prices are counted separately. Missing estimates remain `null`. An explicit zero tariff can produce a known zero estimate.

The range describes the middle half of the observed sample. See `insufficient_samples` and coverage counts when observations are sparse. The report does not delay tasks automatically.

The repository's [history-scale benchmark](validation/history-performance-20261028.json) uses synthetic tasks. The [desktop usage review](validation/desktop-task-usage-review-20261023.json) records validation summaries. These sources lack observed eligibility intervals, complete mainline events, and priced task-level samples needed to estimate production benefits or costs.
