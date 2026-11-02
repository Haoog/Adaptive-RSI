# GitHub ruleset read-only verification evidence / 2026-11-01

[简体中文](ruleset-verification-evidence-20261101.md) | **English**

This round performs one real read-only verification for audit item #162, "Inspect GitHub ruleset requirements": the branch protection and rule endpoints are read with the same `gh api` commands that `adaptive_rsi.delivery.merge_protection` uses, the raw responses are kept as test fixtures, and the decision code is replayed against those real responses. No write operation was issued and the decision code was not changed. Aggregated results are in the [evidence JSON](ruleset-verification-evidence-20261101.json); raw responses live in [tests/fixtures/github_protection](../../tests/fixtures/github_protection).

## Method

- Captured at 2026-11-01T20:32:26Z with account `Haoog` and token scopes `gist`, `read:org`, `repo`. Repository administration comes from owner membership, not an extra admin scope.
- Only these read-only endpoints were called, matching the product code:

```sh
gh api repos/<repository>
gh api repos/<repository>/branches/<branch>/protection
gh api repos/<repository>/rules/branches/<branch> --paginate --slurp
gh api repos/<repository>/rulesets
```

- Each response is stored as one JSON fixture with repository, branch, endpoint, capture time and a read-only marker. Tests replay failures through `ProcessError` with the original `exit_code`, `stdout` and `stderr`, so failure paths also come from real command output rather than hand-written error strings.
- `vercel/next.js` and `microsoft/TypeScript` are public reference repositories, used only to supply ruleset response shapes the managed repository does not have. They are not managed projects and take no part in any delivery decision.

## Three observations

| Repository / branch | Configuration | `protection` read | `rules` read | Decision output | Source |
| --- | --- | --- | --- | --- | --- |
| `Haoog/Adaptive-RSI` / `main` | Classic only | observed: `strict=true`, required check `verify`, `enforce_admins=true`, 0 required approvals, conversation resolution required | observed: 0 rules; repository ruleset index is empty | `state=strict`, `strict_checks=true`, `merge_queue=false`, `complete=true` | Live |
| `Haoog/Ours` / `master` | No protection configured | failed: `Branch not protected` (HTTP 404) | observed: 0 rules | `state=unknown`, `strict_checks=null`, `merge_queue=false`, `complete=false` | Live |
| `Haoog/adaptive-rsi-validation-20261020` / `main` | Private repository on a limited plan | failed: `Upgrade to GitHub Pro or make this repository public to enable this feature.` (HTTP 403) | failed: same 403 | `state=unknown`, `strict_checks=null`, `merge_queue=null`, `complete=false` | Live |
| `vercel/next.js` / `canary` | Ruleset only | failed: HTTP 404 | observed: 17 rules including `required_status_checks` with `strict_required_status_checks_policy=false` | `state=unknown`, `strict_checks=null`, `merge_queue=false`, `complete=false` | Live (public reference repository) |
| `microsoft/TypeScript` / `main` | Ruleset only, with merge queue | failed: HTTP 404 | observed: 6 rules including `merge_queue` and `required_status_checks` | `state=unknown`, `strict_checks=null`, `merge_queue=true`, `complete=false` | Live (public reference repository) |
| Replay: managed classic plus reference ruleset | Classic and ruleset combined | observed | observed | `state=strict`, `strict_checks=true`, `merge_queue=true`, `complete=true` | Replayed fixture, not live |

Three conclusions:

1. Both protection shapes are covered: classic-only strict status checks were observed live on the managed repository, and ruleset-only effective rules were observed live on a reference repository, where `required_status_checks` carries no strict policy.
2. A failed read never becomes "mergeable": both 404 and 403 leave only that source `unknown` with `complete=false`; when either side is unreadable the decision does not claim completeness. With a ruleset present but no classic read, `strict_checks` stays `null`.
3. The combined configuration (classic plus ruleset) does not exist on the managed repository: its ruleset index is empty and creating a ruleset is a write operation outside this read-only round. That row therefore comes from replaying a fixture assembled from two real captures, and `merge_queue=true` comes from the real `microsoft/TypeScript` response.

## Decision code

`merge_protection` in `adaptive_rsi/delivery.py` is unchanged. The real responses exposed no gap: `sources`, `complete` and `state` match what the endpoints return, and a failed capture keeps its source `unknown`, consistent with the ledger acceptance "show unknown instead of claiming mergeability when permissions are insufficient". The existing UI copy likewise only states that strict status checks were observed and GitHub still decides whether merging is allowed, or that current permissions may not expose every protection rule.

## Residual boundaries

- The managed repository has classic protection only and an empty ruleset index; the classic+ruleset combination is still covered by replayed fixtures, not by live acceptance.
- No live sample of an active ruleset with `strict_required_status_checks_policy=true` was observed; that branch remains covered by the offline regression `test_active_rules_endpoint_can_establish_strictness_without_classic_protection`.
- 403 and 404 wording depends on the account plan and repository visibility; fixtures are point-in-time snapshots and do not prove GitHub enforces the same policy later, nor that a real merge would be allowed.
- This round verifies only reading and judging protection rules, not reviews, check results or actual merge-queue execution.

## Verification

```sh
python3 -B -m unittest tests.test_delivery_readiness_audit -q
python3 -B scripts/check_audit_ledger.py
```

Results from this round: 34 tests passed in `tests.test_delivery_readiness_audit`, 8 in `ProtectionRulesTest`, and the ledger and replay index check passed. Per repository convention the full suite was not run on this host; CI runs it on the PR.
