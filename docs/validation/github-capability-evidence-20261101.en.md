# GitHub capability diagnostic real-token verification evidence (audit-200 #178, 2026-11-01 data)

**简体中文** | [English](github-capability-evidence-20261101.en.md)

## Purpose and boundary

audit-200 ledger item #178 "Check GitHub capabilities without test writes" was previously partial: fixture tests
already covered the read/write distinction and the unknown fallback of `readiness.github_capabilities`
(`adaptive_rsi/readiness.py:14`), but the capability diagnostic had never been run with a real token. This round
closes that gap. **The whole round is read-only**: the diagnostic itself issues one GET request; additionally,
`git ls-remote` snapshots of remote refs were taken before and after the diagnostic for comparison. No remote ref
was created, modified, or deleted, and no new credential was created.

One factual boundary must be recorded honestly: **the machine's existing token is not purely read-only**.
`gh auth status` shows its scopes as `gist`, `read:org`, `repo` (a classic token), and the `repo` scope includes
push (this loop uses it daily to push branches and open PRs). The literal read-only branch (`write: unavailable`)
therefore cannot be reproduced against the real remote this round — that branch is covered by fixture
`test_read_only_repository_permissions_never_imply_write`; no new restricted credential was created to exercise it.

## Read-only design of the function (code walkthrough)

`github_capabilities` (`adaptive_rsi/readiness.py:14`) issues exactly one request:

- `gh api repos/<repo> --method GET` (via `run_process`, `adaptive_rsi/process.py:112`)

The function provides no write endpoint: the result always carries `test_write_performed: false`;
`pull_requests_write` and `actions_write` stay `unknown` (the comment at `adaptive_rsi/readiness.py:33-34` states
that repository push permission does not prove fine-grained token scopes, Actions dispatch permission, or
permission to modify workflow files). Within the full `inspect` flow (readiness.py:217-222) the diagnostic also
only reads; no test write is ever performed.

## Real diagnostic run method and result

Run method (this round's probe, read-only wrapper):

```sh
python3 -B probe_capability_178.py   # wraps subprocess.Popen: records every process spawned during the diagnostic, exposes write flags in checks; no mocking, real calls pass through
```

Two runs (20:50:35Z, 20:51:23Z) agreed. The diagnostic spawned exactly one process:

```text
gh api repos/Haoog/Adaptive-RSI --method GET
```

`github_capabilities('Haoog/Adaptive-RSI')` returned:

| Field | Value | Note |
| --- | --- | --- |
| `read` | `available` | repository readable |
| `write` | `available` | server-side `permissions.push=true`, consistent with the classic `repo` scope (a true positive, not a misreport) |
| `actions_write` | `unknown` | not claimed |
| `pull_requests_write` | `unknown` | not claimed |
| `test_write_performed` | `false` | zero test writes throughout |

### Server-side cross-check

One separate read-only GET (`gh api repos/Haoog/Adaptive-RSI --jq .permissions`, 20:50Z) returned
`{"admin":true,"maintain":true,"pull":true,"push":true,"triage":true}`. `push=true` confirms the diagnostic's
`write: available` matches the server-side grant — a true positive.

## Acceptance verdicts

1. **"A read-only token is never misreported as all delivery capabilities available"** — verified: even with push
   really granted, the diagnostic keeps `pull_requests_write` and `actions_write` at `unknown`; "all delivery
   capabilities available" is never claimed (the `write: unavailable` read-only branch is covered by fixture).
2. **"The diagnostic performs no test write"** — verified: the Popen wrapper recorded exactly one spawned process,
   `gh api repos/<repo> --method GET`, with none of `-f`/`-F`/`--input`/`-X`/POST/PUT/PATCH/DELETE present; the
   remote refs snapshot sha256 is identical before and after (`fc10b687…473727`, 335 rows).

## Read-only proof

- `git ls-remote` snapshots before and after the diagnostic share one sha256
  (`fc10b687a6ab18a9608cfb2f48d5ef01d69447c783316b9908b9637719473727`, 335 rows).
- The Popen wrapper observed no non-GET request; the diagnostic function contains no write endpoint in code.
- The token value was never read or recorded; the probe only references scope names from `gh auth status` output.

## Conclusion and status suggestion

- Both acceptance clauses are verified: the diagnostic performs no test write (fixed `test_write_performed: false`,
  no write endpoint, zero write requests in the real run), and "all delivery capabilities available" is never
  misreported even on a push-capable token (fine-grained capabilities stay `unknown`). The read-only-token
  `write: unavailable` branch is covered by fixture; no new credential was created to reproduce it live.
- Suggested ledger update for #178: partial → done. The function is implemented with offline contract tests, and
  this round adds real scoped-token diagnostic evidence; the residual boundary (no purely read-only token to
  exercise the real `unavailable` branch) is kept honestly in the evidence JSON under `boundary_notes`.
