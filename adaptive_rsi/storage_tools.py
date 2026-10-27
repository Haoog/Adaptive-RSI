"""Consistent private backups and deliberately minimal shareable diagnostics."""
import json
import gzip
import hashlib
import os
import sqlite3
import stat
import tempfile
import time
from contextlib import closing
from pathlib import Path

from .store import Store, uid


def archive(store, max_mb=1024):
    """Export a private compressed consistent snapshot within an archive capacity budget."""
    if type(max_mb) is not int or not 1 <= max_mb <= 102400:
        raise ValueError("Archive capacity must be 1..102400 MiB")
    import fcntl
    fd = os.open(store.root / "archive.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "wb") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError("Invalid archive lock")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another archive is in progress") from None
        return _archive(store, max_mb)


def _archive(store, max_mb):
    folder = store.root / "archives"
    folder.mkdir(mode=0o700, exist_ok=True)
    capacity = max_mb * 1024 * 1024
    used = sum(info.st_size for _, info in _file_info(folder.iterdir()))
    if used >= capacity:
        raise ValueError("Archive capacity reached; move existing archives before retrying")
    snapshot = backup(store)
    source = store.root / "backups" / (snapshot["id"] + ".sqlite3")
    staging = None
    try:
        fd, name = tempfile.mkstemp(prefix=".archive-", dir=folder)
        staging = Path(name)
        with os.fdopen(fd, "wb") as output:
            with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
                with source.open("rb") as raw:
                    while True:
                        block = raw.read(65536)
                        if not block:
                            break
                        compressed.write(block)
                        compressed.flush()
                        if output.tell() + used > capacity:
                            raise ValueError("Archive capacity reached; move existing archives before retrying")
            if output.tell() + used > capacity:
                raise ValueError("Archive capacity reached; move existing archives before retrying")
            output.flush()
            os.fsync(output.fileno())
        target = folder / (snapshot["id"] + ".sqlite3.gz")
        os.link(staging, target)
        return {"id": snapshot["id"], "path": str(target), "bytes": target.stat().st_size,
                "capacity_bytes": capacity, "source_bytes": snapshot["bytes"], "history_deleted": False}
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)
        source.unlink(missing_ok=True)


def backup(store, cancel=None, progress=None):
    folder = store.root / "backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / (uid() + ".sqlite3")
    fd, name = tempfile.mkstemp(prefix=".backup-", suffix=".tmp", dir=folder)
    staging = Path(name)
    try:
        os.close(fd)
        with store.connect() as source, closing(sqlite3.connect(str(staging))) as target:
            def advance(status, remaining, total):
                if cancel and cancel():
                    from .process import Cancelled
                    raise Cancelled("Backup cancelled")
                if progress:
                    progress(total - remaining, total)
            if cancel or progress:
                source.backup(target, pages=128, progress=advance)
            else:
                source.backup(target)
            # A portable snapshot must not require adjacent WAL/SHM files on restore.
            target.execute("PRAGMA journal_mode=DELETE")
        # Publish only the closed snapshot; a name collision must preserve the old backup.
        os.link(staging, path)
        return {"id": path.stem, "bytes": path.stat().st_size, "created": time.time()}
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(staging) + suffix).unlink(missing_ok=True)


def verify_backup(path):
    """Open a backup in read-only mode and prove it is a usable state snapshot."""
    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise ValueError("Backup must be a regular file")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Backup integrity check failed")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise ValueError("Backup schema is newer than this runtime")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Backup contains invalid relationships")
        for table in ("projects", "tasks", "runs", "settings"):
            db.execute("SELECT * FROM " + table + " LIMIT 0")
    return {"valid": True, "schema_version": version, "bytes": path.stat().st_size}


def retain_backups(store, max_count=20, max_age_days=90, now=None):
    """Remove only older, successful backup snapshots after proving the newest remains."""
    if type(max_count) is not int or not 1 <= max_count <= 1000:
        raise ValueError("Backup count must be 1..1000")
    if type(max_age_days) is not int or not 1 <= max_age_days <= 3650:
        raise ValueError("Backup age must be 1..3650 days")
    now = time.time() if now is None else now
    folder = store.root / "backups"
    paths = sorted((path for path in folder.glob("*.sqlite3")
                    if path.is_file() and not path.is_symlink()),
                   key=lambda path: path.stat().st_mtime, reverse=True)
    removed = []
    for index, path in enumerate(paths):
        if index < max_count and now - path.stat().st_mtime <= max_age_days * 86400:
            continue
        path.unlink()
        removed.append(path.name)
    return {"kept": len(paths) - len(removed), "removed": removed}


def _create_directories(destination):
    """Create ancestors incrementally, then atomically claim the destination itself."""
    missing = []
    current = destination.parent
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    created = []
    try:
        for path in reversed(missing):
            try:
                path.mkdir(mode=0o777)
            except FileExistsError:
                # Another process won the race for an ancestor; reuse it but never claim ownership.
                continue
            created.append(path)
        try:
            # exist_ok=False rejects a concurrently created directory or symlink without following it.
            destination.mkdir(mode=0o700)
        except FileExistsError:
            raise ValueError(
                "Restore requires a new state directory; existing state is never overwritten") from None
        created.append(destination)
    except BaseException:
        _discard_incomplete_restore(destination, list(reversed(created)))
        raise
    return list(reversed(created))


def _discard_incomplete_restore(destination, created):
    """Remove only a failed restore's own state files and self-created empty directories."""
    created = list(created)
    if destination in created:  # A concurrent process may own the destination; never delete its files.
        base = destination / "state.sqlite3"
        for suffix in ("", "-wal", "-shm", "-journal"):
            target = Path(str(base) + suffix)
            try:
                if target.is_symlink() or target.is_file():
                    target.unlink()
            except OSError:
                pass
    # rmdir preserves pre-existing or concurrently added data; retry remains possible when empty.
    for path in created:
        try:
            path.rmdir()
        except OSError:
            break


def restore(source, destination):
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser()
    if destination.is_symlink() or destination.exists():
        raise ValueError("Restore requires a new state directory; existing state is never overwritten")
    # Normalize existing ancestors only; the final component must be claimed without following links.
    destination = destination.parent.resolve() / destination.name
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Backup integrity check failed")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = ["projects", "tasks", "runs", "settings"]
        # Parent schema version 1 databases did not have these additive tables;
        # Store performs the supported migration after the snapshot is copied.
        if version >= 2:
            tables += ["task_decisions", "task_revisions", "task_dependencies", "submissions",
                       "phase_times", "remote_waits", "task_completions", "schema_migrations"]
        for table in tables:
            db.execute("SELECT * FROM " + table + " LIMIT 0")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Backup contains invalid relationships")
        if db.execute("SELECT 1 FROM runs r LEFT JOIN tasks t ON t.id=r.task "
                      "WHERE r.task IS NOT NULL AND (t.id IS NULL OR r.project<>t.project) LIMIT 1").fetchone():
            raise ValueError("Backup contains invalid run ownership")
        created = _create_directories(destination)
        if destination not in created:  # Never write into a directory this process did not exclusively create.
            raise ValueError("Restore requires a new state directory; existing state is never overwritten")
        try:
            target = destination / "state.sqlite3"
            with sqlite3.connect(str(target)) as out:
                db.backup(out)
            target.chmod(0o600)
        except BaseException:
            _discard_incomplete_restore(destination, created)
            raise
    try:
        store = Store(destination)
        try:
            store.recover()
            with store.connect() as db:
                db.execute("UPDATE projects SET paused=1")
                db.execute("UPDATE tasks SET state='expired' WHERE state='awaiting_approval'")
                invalidated = db.execute("SELECT id,result FROM runs WHERE state='awaiting_approval'").fetchall()
                for row in invalidated:
                    try:
                        result = json.loads(row["result"] or "{}")
                    except (TypeError, ValueError):
                        result = {}
                    if not isinstance(result, dict):
                        result = {}
                    # Candidate workspaces are not part of the database snapshot; record the real reason.
                    result["restore_invalidation"] = {"reason": "candidate_not_in_backup"}
                    db.execute("UPDATE runs SET state='expired',result=? WHERE id=?",
                               (json.dumps(result), row["id"]))
                # Only the database was copied. Never retain access to the source state's files.
                db.execute("UPDATE runs SET path=NULL")
                for row in db.execute("SELECT id,result FROM runs WHERE state IN ('pr_open','merged')").fetchall():
                    result = json.loads(row["result"])
                    result["feedback"] = unavailable_feedback(result.get("feedback", {}))
                    db.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps(result), row["id"]))
                # Local verification caches describe artifacts/environments absent from this restore.
                # Keep sealed remote identities, PR receipts and automation reconciliation state.
                for prefix in ("evaluation-baseline:", "evaluation-baseline-entry:", "verified-checks:", "task-proof:", "task-proof-current:"):
                    db.execute("DELETE FROM settings WHERE substr(key,1,?)=?", (len(prefix), prefix))
                # Machine paths and external credentials need reconnecting; pending side effects need reconciliation.
                db.execute("UPDATE tasks SET state='blocked' WHERE source IN ('system_publish','system_delivery') AND state='queued'")
        finally:
            store.close()
    except BaseException:
        _discard_incomplete_restore(destination, created)
        raise
    return {"restored": True, "paused": True, "artifacts_restored": False}


def _file_info(paths, follow_symlinks=False):
    for path in paths:
        try:
            info = path.stat() if follow_symlinks else path.lstat()
        except FileNotFoundError:
            continue  # SQLite sidecars can disappear during enumeration.
        if stat.S_ISREG(info.st_mode):
            yield path, info


MISSING_ARTIFACTS = ("Run artifacts are unavailable in this state directory. Restore the original acceptance and "
                     "dependency artifacts into this run's directory, or run fresh verification before release/deploy.")


def pending_investigation(report):
    """Find actionable database evidence, including wrappers written by older restores."""
    while isinstance(report, dict):
        if "investigation_pending" in report:
            if not report["investigation_pending"]:
                return None  # A completed wrapper supersedes its historical pending flag.
            return report.get("investigation_report", report)
        report = report.get("previous")
    return None


def unavailable_feedback(previous):
    report = {"state": "insufficient_evidence", "reason": MISSING_ARTIFACTS, "previous": previous}
    investigation = pending_investigation(previous)
    if investigation is not None:
        report.update(investigation_pending=True, investigation_report=investigation)
    return report


def run_artifact_path(store, run, name=""):
    """Resolve artifacts only inside this state's own direct run directory."""
    identifier = run["id"]
    if not identifier or Path(identifier).parts != (identifier,) or identifier in (".", ".."):
        raise ValueError(MISSING_ARTIFACTS)
    expected = store.root / "runs" / identifier
    # A user may explicitly copy missing artifacts into the new state after restoration.
    directory = Path(run["path"] or expected).resolve()
    target = (directory / name).resolve()
    if directory != expected or not target.is_relative_to(directory):
        raise ValueError(MISSING_ARTIFACTS)
    return target


DELIVERY_ACTIVE = ("queued", "planning", "executing", "evaluating")
DELIVERY_COMPLETE = ("completed", "pr_open", "merged", "verified")
DELIVERY_CLOSED = ("cancelled", "superseded", "obsolete", "rejected")
CANDIDATE_CLOSED = ("cancelled", "expired", "obsolete", "superseded", "rejected")


def _approval_candidates(store):
    """Split awaiting candidates by their persisted publish intent without rewriting history."""
    rows = store.query(
        "SELECT r.id,r.task,r.project,r.ended,r.result,"
        "(SELECT t.state FROM tasks t WHERE t.id=r.task) AS candidate_state,"
        "(SELECT t.state FROM tasks t WHERE t.project=r.project AND t.source='system_publish' "
        " AND t.body=r.id ORDER BY t.updated DESC,t.id DESC LIMIT 1) AS publish_state,"
        "EXISTS(SELECT 1 FROM settings s WHERE s.key='delivery:' || r.id) AS receipt "
        "FROM runs r WHERE r.state='awaiting_approval' ORDER BY r.ended")
    pending, delivering, delivered = [], [], []
    for row in rows:
        entry = {key: row[key] for key in ("id", "task", "project", "ended")}
        if row["candidate_state"] in CANDIDATE_CLOSED:
            continue  # Cancelled/expired/superseded candidates stay in history but are not pending approval.
        publish = row["publish_state"]
        result = {}
        try:
            result = json.loads(row["result"] or "{}")
        except (TypeError, ValueError):
            pass
        if publish in DELIVERY_ACTIVE:
            delivering.append(entry)
        elif publish in DELIVERY_COMPLETE or row["receipt"] or (isinstance(result, dict) and result.get("pr")):
            delivered.append(entry)
        elif publish not in DELIVERY_CLOSED:
            pending.append(entry)
        else:
            # The publish intent was withdrawn without delivery; the still-live candidate can be approved again.
            pending.append(entry)
    return pending, delivering, delivered


def overview(store):
    categories = {}
    for label, directory in [("runs", store.root / "runs"), ("backups", store.root / "backups"),
                             ("suites", store.root / "suites"), ("archives", store.root / "archives"),
                             ("installations", store.root / "installations")]:
        paths = directory.rglob("*")
        if label == "backups":
            paths = (p for p in paths if not p.name.startswith(".backup-"))
        categories[label] = sum(info.st_size for _, info in _file_info(paths))
    categories["database"] = sum(info.st_size for _, info in _file_info(store.root.glob("state.sqlite3*"), True))
    backups = sorted(_file_info((store.root / "backups").glob("*.sqlite3"), True),
                     key=lambda item: item[1].st_mtime, reverse=True)
    expiring, delivering, delivered = _approval_candidates(store)
    manifest = [{"path": p.relative_to(store.root).as_posix(), "bytes": info.st_size,
                 "modified": info.st_mtime, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                for p, info in backups[:20]]
    return {"bytes": categories, "backups": [{"id": p.stem, "bytes": info.st_size, "created": info.st_mtime}
            for p, info in backups[:20]], "backup_manifest": manifest,
            "expiring": expiring, "delivering": delivering, "delivered": delivered}


def diagnostics(store):
    from . import __version__
    return {"version": __version__, "created": time.time(),
            "included": ["version", "state_counts", "failure_codes", "storage_bytes"],
            "tasks": store.query("SELECT state,count(*) AS count FROM tasks GROUP BY state"),
            "runs": store.query("SELECT state,count(*) AS count FROM runs GROUP BY state"),
            "failures": store.query("SELECT json_extract(result,'$.failure.code') AS code,count(*) AS count FROM runs WHERE json_valid(result) AND json_extract(result,'$.failure.code') IS NOT NULL GROUP BY code"),
            "invalid_evidence": store.one("SELECT count(*) AS count FROM runs WHERE NOT json_valid(result)")["count"],
            "database": store.metrics(),
            "schema_migrations": store.schema_migrations(),
            "relationship_audit": store.relationship_audit(),
            "quarantined_orphans": store.one("SELECT count(*) AS count FROM relationship_quarantine")
            if store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='relationship_quarantine'")
            else {"count": 0},
            "storage_bytes": overview(store)["bytes"]}
