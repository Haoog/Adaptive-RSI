# Test-runner recursion incident and regression rules

[简体中文](test-runner-recursion-20261028.md) | **English**

## Incident and impact

The maintainer reported two occurrences of the same failure on 2026-10-28. The identified fault was a test runner recursively dispatching itself, continually creating Python subprocesses in independent sessions until system process resources were exhausted and the Hermes backend became unreachable. Closing the desktop window did not ensure those processes exited. This record describes the shared cause; it does not invent a separate complete timeline for each occurrence.

The maintainer's original diagnosis included `fork: Resource temporarily unavailable (35)` and more than ten nested `[group 2]` prefixes. These are evidence of process recursion. A setup screen after recovery is not evidence that the Hermes installation disappeared.

## Cause and fix

A test in `tests/test_validation_planning.py` selects both a whole test class and one of its methods, expecting the runner to reject the overlap. The old runner checked only for duplicate selector strings, without checking expanded test overlap before spawning. A child therefore reran the test that invoked the runner, creating another generation. `start_new_session=True` detached descendants from the parent session.

The fix was merged in [PR #92](https://github.com/Haoog/Adaptive-RSI/pull/92), commit `fa09f3b`:

- `scripts/test_selected.py` checks selector containment and duplicate concrete tests before dispatch and rejects empty selections; exact duplicate selectors are idempotent and run once after deduplication.
- `scripts/test_parallel.py` uses `RSI_TEST_RUNNING` to reject reentry into a module already executing in a worker, and dispatches at most four workers. Controlled paths that execute other modules for isolation fixtures remain, so the guard must not be described as prohibiting every nested process.
- The `scripts.test_worker` execution path exits when its parent disappears. On cancellation, the runner terminates and reaps the process groups it owns.

## Rules for subsequent work

1. Read this record and the relevant call paths before changing runners or tests that invoke a runner.
2. Verify invalid selections and module reentry by intercepting `subprocess.Popen` and asserting zero spawns. Never reproduce the fault by allowing real recursion.
3. Runner tests must not dispatch their own test module through real workers. Mock `run_groups` for selection and grouping tests. Use a plain intercepting function to test the spawn boundary; a wrapper that activates a mock exemption does not establish that the guard works.
4. Assign one owner to the complete suite for each batch. Run the safety regressions before launching at most four workers. Stop the affected run immediately if nested group prefixes repeat or process growth becomes abnormal; do not start another complete suite.
5. Never remove overlap checks, running-module checks, process-group cleanup or parent monitoring to make tests pass, reduce validation time or permit nesting.

## Validation entry points

In a trusted checkout, first run the key regressions that do not dispatch real workers:

```sh
python3 -B -m unittest \
  tests.test_validation_planning.ValidationPlanningTest.test_selected_rejects_empty_and_overlapping_expansions \
  tests.test_validation_planning.ValidationPlanningTest.test_worker_refuses_to_spawn_another_runner -q
```

Then run `python3 -B -m unittest tests.test_validation_planning tests.test_release_parallel -q`. That broader command includes bounded real subprocess lifecycle fixtures and does not reproduce infinite recursion. Passing regressions verifies these defined boundaries; it is not a guarantee that every future process failure is impossible.
