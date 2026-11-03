# Offline legacy file queue import

`adaptive_rsi.legacy_queue.import_queue(store, project_id, snapshot_path)` is an explicit Python entry point; it does not automatically scan, start workers, or switch the old system. Use only a quiescent local snapshot. The caller must first confirm old workers are stopped and reconcile `.claim`, `.hold`, failures, worktrees, and existing PRs to prevent duplicate claims. The target project must be paused with no unfinished runs. It stays paused after import; complete the handoff manually before resuming.

Only non-hidden regular `.md` files directly under the snapshot root are eligible; symlinks and subdirectories are not imported. Direct Markdown entries in `.claim`, `.hold`, `failed`, and `done` are listed in `reserved` for manual review, not turned into tasks. Source files are never moved or modified. Markdown must be nonempty UTF-8 and at most 30,000 characters; otherwise the whole batch fails.

Tasks use the existing `goal` source and `queued` state, with canonical source path, legacy filename, and content SHA-256 in evidence. Deduplication uses project and canonical absolute path: reruns at the same path return `existing`; changed content aborts the whole transaction for manual reconciliation. Moving the snapshot changes its identity, so keep its path stable. `imported` and `existing` contain task IDs. Imported tasks are never automatically updated, deleted, or requeued, and identical tasks across directories or projects are not identified. File ordering does not guarantee scheduler execution order.

This example uses only a caller-created temporary directory (`store` is a local Store and `project_id` is a paused project):

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from adaptive_rsi.legacy_queue import import_queue

with TemporaryDirectory() as tmp:
    snapshot = Path(tmp) / 'snapshot'
    snapshot.mkdir()
    (snapshot / 'example.md').write_text('# Example\n', encoding='utf-8')
    result = import_queue(store, project_id, snapshot)
```

Actual migration requires a stable retained local snapshot path; this example is for offline verification only. This feature is not connected to old scripts or servers and does not verify PRs or remote state.
