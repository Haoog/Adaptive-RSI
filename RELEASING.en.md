# Releases, deployments and user updates

[简体中文](RELEASING.md) | **English**

Ordinary mainline merges accumulate changes without bumping versions or prompting user updates. A version PR is prepared only when a stable release batch is explicitly started. Only a reconstructed version patch, complete verification, and all GitHub required checks and reviews allow normal protected merging and a formal GitHub Release. Building a plugin has no push or publish side effects. Publishing the product does not deploy any user's server.

## Repository versions and releases

`.github/workflows/release.yml` continues publication automatically for verified mechanical version commits, but ordinary code merges do not create version PRs. When a batch is stable, run the workflow in Actions with `prepare_release` enabled and choose `release_level` (patch by default; minor or major only when explicitly selected). With that checkbox off, no new version is prepared. Urgent fixes may use patch; commit count never determines version cadence. Ordinary fixes and UX improvements increment only the last component; use minor for substantial milestone changes and major for breaking changes. Previous `release:*` PR labels no longer trigger releases or select increments. Tests, version-PR gates, packaging and formal publication remain automated. This policy affects this repository only, not managed-project delivery authorization.

Versions agree in `plugin.json`, `pyproject.toml`, `adaptive_rsi/__init__.py` and `update.json`; bilingual `CHANGELOG` content moves from `Unreleased` into the new version. Commit messages are Chinese.

Version branches contain the target version and mainline SHA. When mainline advances, automation closes an old PR only after proving it equals the reconstructed mechanical version patch, then creates one replacement against the new baseline. Manual changes block replacement. Repeated triggers reuse the same PR; a closed identical PR is not automatically reopened.

PRs created with the default `GITHUB_TOKEN` do not trigger `pull_request` checks. Preparation therefore explicitly dispatches trusted-mainline `release-verify.yml`: independently verify the repository, target branch and exact PR SHA, reconstruct the expected six-file patch from its baseline, then run the complete tests, browser checks and ZIP/wheel installation and upgrade smoke. Only a separate reporting job receives `checks:write`; it writes the ordinary required `verify` check onto that exact PR SHA. Candidate execution jobs receive read permissions and no write token. Merging uses `--match-head-commit` without administrator bypass; required human reviews, failed checks or conflicts block it.

After a bot merge, the workflow explicitly dispatches formal publication because `GITHUB_TOKEN` merges do not trigger push workflows. The final merged commit is rebuilt and verified. Smoke starts the actual ZIP service in isolated state, checks its version and preserved data, and installs the previous official wheel before upgrading when one exists. With no prior wheel, the first release explicitly reports installation and repeated installation only, rather than claiming a cross-version upgrade. Publication uses the artifacts uploaded by that verification run, creates a fixed tag and draft, uploads all assets, then publishes it. Conflicting tags or differing existing formal assets stop the operation; formal assets and tags are never replaced. Fixed ZIP metadata and wheel `SOURCE_DATE_EPOCH` support reconciliation on retries.

Smoke may use optional `GH_TOKEN` credentials solely for this repository's fixed official latest Release API; workflows supply a read-only token for this step. That request rejects every redirect, and asset downloads receive no token. Only an API 404 means no prior formal release; 403 rate limits and all other errors still block validation.

GitHub must permit Actions to create PRs and grant the declared workflow permissions. Disabled capabilities fail explicitly; no extra PAT is assumed. Administrators must maintain required checks and review policies. Protect release tags and workflow files with repository rules.

## Managed-project authorization

New projects keep candidates locally and receive no PR, merge, release or deployment permission. This repository's maintenance authorization does not transfer to other projects. The project panel lists existing GitHub workflows that satisfy the contract. An explicit selection and authorization pins the current default-branch commit using `rsi-workflow/<full-SHA>`. It saves only the selected release or deployment permission and preserves PR mode and auto-merge settings.

Both `delivery.release` and `delivery.deploy` contain `enabled`, `workflow`, `ref` and `sha`. Workflow paths are fixed `.github/workflows/*.yml` or `.yaml` files; ref is a lightweight tag pointing to a previously trusted commit. Advanced users may configure the same fixed identity manually. Workflows must implement this contract; see [rsi-delivery-workflow.example.yml](scripts/rsi-delivery-workflow.example.yml):

- Top-level `run-name: ${{ inputs.rsi_operation_id }}`.
- Required string inputs `rsi_operation_id` and `rsi_sha` under `workflow_dispatch.inputs`.
- Release or deploy the exact full `rsi_sha`; persist `rsi_operation_id` at the target so retries remain idempotent.
- Maintainers preconfigure commands, targets and secrets in the workflow and GitHub environment. Models cannot select arbitrary commands or secrets or grant release permissions. The example fails until a real target is configured.

The workflow must already exist on that repository's default branch. The controller never promotes model candidates into trusted workflows. After ordinary gates and post-merge verification pass, it dispatches the sealed workflow policy with the actual merged SHA. Before each external action it rechecks project, repository, tag, pause, cancellation and current authorization. Revocation and target changes prevent unissued operations. Workflows already accepted by GitHub may continue and remain subject to the maintainer's GitHub environment rules.

Operation state, exact target SHA, workflow version, stable operation ID, GitHub run ID/attempt, URL and errors persist across service restarts. Timeouts cannot prove GitHub rejected a request: `dispatch_unknown` reconciles by stable ID and never blindly dispatches twice. If reconciliation cannot establish the result, inspect the original workflow rather than forcing another release. Confirmed failed workflows retry through the same GitHub run ID; repeated clicks do not duplicate an unacknowledged rerun.

An explicit GitHub 4xx rerun rejection (except 408 timeout) restores failure and records the HTTP status, allowing another retry after the cause is addressed. Network errors, 408 and 5xx cannot prove rejection; only a newer attempt on the same run confirms execution. After five minutes without one, `rerun_unknown` stops automatic polling and requires inspection on GitHub. Manual reconciliation only reads the result and never resends an uncertain request. The same rules apply to rollback workflows.

## Deployment health and rollback

Without `delivery.deploy.health_url`, workflow success remains `workflow_succeeded` with health `unverified`. Users may explicitly configure a fixed HTTPS health URL without credentials, query, fragment or cross-origin redirects. The endpoint must return HTTP 200 and JSON `{"sha":"full deployed SHA","healthy":true}` (`status: "ok"` is also accepted). Only an exact version match becomes `healthy`; timeouts, invalid responses and old SHAs fail health verification.

`delivery.deploy.automatic_rollback` defaults off and requires a health URL. Only SHAs actually verified healthy for the same project, repository and health target qualify. After a health failure, valid current authorization and a previous healthy SHA allow the same pinned deployment workflow to redeploy that SHA, followed by another health check. Workflow success alone cannot claim recovery: only that second health pass becomes `rolled_back`; otherwise the state is `rollback_failed`. Without a healthy historical version, retain `health_failed` with `rollback_unavailable`, rather than claiming a rollback.

Durable ownership serializes deployments to the same project's same target. Once a newer deployment owns the target, an older operation cannot issue a stale rollback. Health failures can retry the check; failed rollback workflows can rerun their original GitHub run. This rollback redeploys known code through a trusted workflow. It is not a database rollback guarantee; application migration and compatibility policies belong to that workflow's maintainer.

## Stable user updates

Daily checks and notifications are the default. `updates.mode` accepts `manual`, `notify` or `automatic`; automatic mode also uses `updates.auto_policy`: `patch` (default, same major/minor), `minor` (same major) or `stable` (any newer stable version). Drafts, prereleases, equal versions and downgrades are never installed. Updates wait for project runs, model probes and browser authorization to become idle. Calls and tokens are recorded without quotas.

The source is fixed to `Haoog/Adaptive-RSI`. Formal releases require `adaptive-rsi-X.Y.Z-plugin.zip` and its `.zip.sha256`; ZIP roots are `adaptive-rsi/`. There is currently no independent signature; checksums are provided by the same GitHub Release. `update.json` only accepts `format: 1`, `python_min: [3, 9]` and `state_schema: 1`. Incompatible state migrations are rejected rather than bypassed by a version change.

The same stable release also includes `adaptive-rsi-X.Y.Z-macos-arm64-adhoc.dmg` and its `.dmg.sha256`, built from the exact verified release commit. Validation covers Rust bridge tests, engine version, app signature integrity and the DMG checksum. Only macOS ARM64 is currently built. Its temporary ad-hoc signature is not an Apple Developer ID signature or notarization; macOS may show a security warning, so do not describe it as notarized. Users manually download and replace the desktop app; server auto-updates do not replace the desktop application.

The updater uses separate version directories and active pointers, rereads the official feed, verifies SHA-256 and checks archive paths. Identical staged content can resume; differing content stops installation. Interrupted downloads can retry. Automatic failures back off for up to three attempts; users may check again or retry manually. A supervisor uses an exclusive lock, persistent phases and the original database backup. Interrupted supervision resumes after restart while new work and mutations remain blocked. Work is admitted only after startup health passes. Startup failure restores the guarded pre-start snapshot only while no runtime owns the database. Missing backups, incompatible state protocols and disk damage require manual recovery; arbitrary migrations, power loss and external writes cannot be guaranteed reversible.

The legacy 0.3.0 supervisor has no phase journal or supervisor lock. Allow its original supervisor 90 seconds to finish; absence of the new lock never triggers takeover or candidate shutdown. If completion remains unconfirmed, preserve the current database and require manual recovery. The actual installed 0.3.0 supervisor has been tested upgrading to the current candidate code in temporary state while polling status continuously, verifying normal handoff and database preservation. This does not replace validation of official release assets.

Already loaded Codex plugin metadata still requires a marketplace refresh and Codex restart. After a managed upgrade, `STATE/installations/current` is a stable entry point. Wheel users update with pip or switch to the bundled launcher.
