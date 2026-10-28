"""Consistent private backups and deliberately minimal shareable diagnostics."""
import json
import gzip
import hashlib
import os
import sqlite3
import stat
import tempfile
import time
import copy
import shutil
from contextlib import closing, contextmanager
from pathlib import Path

from .store import Store, uid


@contextmanager
def _regular_reader(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Archive source must be a regular file")
        yield stream


def archive(store, max_mb=1024, compression=6, timeout=300, cancel=None):
    """Export a private compressed consistent snapshot within an archive capacity budget."""
    if type(max_mb) is not int or not 1 <= max_mb <= 102400:
        raise ValueError("Archive capacity must be 1..102400 MiB")
    if type(compression) is not int or not 0 <= compression <= 9:
        raise ValueError("Archive compression must be 0..9")
    if type(timeout) not in (int, float) or not 1 <= timeout <= 3600:
        raise ValueError("Archive timeout must be 1..3600 seconds")
    import fcntl
    fd = os.open(store.root / "archive.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "wb") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError("Invalid archive lock")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another archive is in progress") from None
        return _archive(store, max_mb, compression, timeout, cancel)


def _archive(store, max_mb, compression=6, timeout=300, cancel=None):
    started = time.monotonic()
    cpu_started = time.thread_time()
    deadline = started + timeout
    def check_budget():
        if cancel and cancel():
            from .process import Cancelled
            raise Cancelled("Archive cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Archive deadline exceeded")
    check_budget()
    folder = store.root / "archives"
    folder.mkdir(mode=0o700, exist_ok=True)
    capacity = max_mb * 1024 * 1024
    used = 0
    for _, info in _file_info(folder.iterdir()):
        check_budget()
        used += info.st_size
    if used >= capacity:
        raise ValueError("Archive capacity reached; move existing archives before retrying")
    snapshot = backup(store, timeout=timeout, cancel=cancel, deadline=deadline)
    source = store.root / "backups" / (snapshot["id"] + ".sqlite3")
    staging = None
    manifest = None
    manifest_owned = False
    published = False
    try:
        fd, name = tempfile.mkstemp(prefix=".archive-", dir=folder)
        staging = Path(name)
        with os.fdopen(fd, "wb") as output:
            with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0,
                               compresslevel=compression) as compressed:
                with source.open("rb") as raw:
                    while True:
                        check_budget()
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
        check_budget()
        inventory = {"format": 1, "id": snapshot["id"], "created": time.time(),
                     "compression": compression, "archive_bytes": staging.stat().st_size,
                     "archive_sha256": file_digest(staging, check_budget), "source_bytes": source.stat().st_size,
                     "source_sha256": file_digest(source, check_budget), "contents": ["state.sqlite3"],
                     "scope": "database_only"}
        encoded = json.dumps(inventory, sort_keys=True).encode()
        if staging.stat().st_size + len(encoded) + used > capacity:
            raise ValueError("Archive capacity reached; move existing archives before retrying")
        check_budget()
        manifest = target.with_suffix(target.suffix + ".manifest.json")
        with manifest.open("xb") as output:
            manifest_owned = True
            os.chmod(manifest, 0o600)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        check_budget()
        os.link(staging, target)
        published = True
        return {"id": snapshot["id"], "path": str(target), "bytes": target.stat().st_size,
                "capacity_bytes": capacity, "source_bytes": snapshot["bytes"], "history_deleted": False,
                "manifest": str(manifest), "compression": compression,
                "seconds": time.monotonic() - started, "cpu_seconds": time.thread_time() - cpu_started}
    finally:
        if manifest_owned and not published:
            manifest.unlink(missing_ok=True)
        if staging is not None:
            staging.unlink(missing_ok=True)
        source.unlink(missing_ok=True)


def verify_archive(path, timeout=60, max_source_mb=102400):
    """Verify compressed bytes and restored SQLite without touching the active database."""
    path = Path(path).absolute()
    manifest = path.with_suffix(path.suffix + ".manifest.json")
    if path.is_symlink() or manifest.is_symlink() or not path.is_file() or not manifest.is_file():
        raise ValueError("Archive and manifest must be regular files")
    if type(timeout) not in (int, float) or not 1 <= timeout <= 3600:
        raise ValueError("Archive timeout must be 1..3600 seconds")
    if type(max_source_mb) is not int or not 1 <= max_source_mb <= 102400:
        raise ValueError("Invalid archive verification capacity")
    with _regular_reader(manifest) as stream:
        content = stream.read(16385)
    if len(content) > 16384:
        raise ValueError("Archive manifest is too large")
    metadata = json.loads(content)
    if (not isinstance(metadata, dict) or metadata.get("format") != 1 or
            type(metadata.get("source_bytes")) is not int or
            not 0 < metadata["source_bytes"] <= max_source_mb * 1024 * 1024):
        raise ValueError("Invalid archive manifest")
    deadline = time.monotonic() + timeout
    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError("Archive verification deadline exceeded")
    digest = hashlib.sha256()
    with _regular_reader(path) as raw:
        for block in iter(lambda: raw.read(65536), b""):
            check()
            digest.update(block)
    if path.stat().st_size != metadata.get("archive_bytes") or digest.hexdigest() != metadata.get("archive_sha256"):
        raise ValueError("Archive compressed content checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="rsi-archive-verify-") as directory:
        if shutil.disk_usage(directory).free < metadata["source_bytes"] + 16 * 1024 * 1024:
            raise ValueError("Insufficient disk space to verify archive")
        restored = Path(directory) / "state.sqlite3"
        digest, size = hashlib.sha256(), 0
        with _regular_reader(path) as compressed, gzip.GzipFile(fileobj=compressed, mode="rb") as raw, restored.open("wb") as output:
            for block in iter(lambda: raw.read(65536), b""):
                check()
                size += len(block)
                if size > metadata["source_bytes"]:
                    raise ValueError("Archive expanded size exceeds manifest")
                digest.update(block)
                output.write(block)
        if size != metadata["source_bytes"] or digest.hexdigest() != metadata.get("source_sha256"):
            raise ValueError("Archive source checksum mismatch")
        check()
        result = verify_backup(restored, deadline=deadline)
    return {**result, "archive": path.name, "checked_at": time.time(), "scope": "database_only"}


def backup(store, cancel=None, progress=None, timeout=300, reserve_bytes=16 * 1024 * 1024, deadline=None):
    folder = store.root / "backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    if not 1 <= timeout <= 3600 or reserve_bytes < 0:
        raise ValueError("Invalid backup resource budget")
    deadline = min(deadline, time.monotonic() + timeout) if deadline is not None else time.monotonic() + timeout
    if shutil.disk_usage(folder).free < store.path.stat().st_size + reserve_bytes:
        raise ValueError("Insufficient disk space for a consistent backup")
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
                if time.monotonic() >= deadline:
                    raise TimeoutError("Backup exceeded its total deadline")
                if shutil.disk_usage(folder).free < reserve_bytes:
                    raise ValueError("Backup stopped to preserve disk reserve")
                if progress:
                    progress(total - remaining, total)
            source.backup(target, pages=128, progress=advance)
            # A portable snapshot must not require adjacent WAL/SHM files on restore.
            target.execute("PRAGMA journal_mode=DELETE")
        # Publish only the closed snapshot; a name collision must preserve the old backup.
        os.link(staging, path)
        store._storage_snapshot = None
        return {"id": path.stem, "bytes": path.stat().st_size, "created": time.time()}
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(staging) + suffix).unlink(missing_ok=True)


def verify_backup(path, deadline=None):
    """Open a backup in read-only mode and prove it is a usable state snapshot."""
    path = Path(path).expanduser().absolute()
    if not path.is_file() or path.is_symlink():
        raise ValueError("Backup must be a regular file")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        if deadline is not None:
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
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
    protected = None
    invalid = []
    for path in paths:
        try:
            verify_backup(path)
        except (ValueError, OSError, sqlite3.Error):
            invalid.append(path.name)
        else:
            protected = path
            break
    if paths and protected is None:
        return {"kept": len(paths), "removed": [], "invalid": invalid,
                "reason": "no_verified_recovery_point"}
    for index, path in enumerate(paths):
        if path == protected or path.name in invalid:
            continue
        if index < max_count and now - path.stat().st_mtime <= max_age_days * 86400:
            continue
        path.unlink()
        removed.append(path.name)
    return {"kept": len(paths) - len(removed), "removed": removed, "invalid": invalid,
            "protected": protected.name if protected else None}


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
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
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
            with closing(sqlite3.connect(str(target))) as out:
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
                for case in db.execute("SELECT id,spec FROM cases").fetchall():
                    try:
                        spec = json.loads(case["spec"])
                        spec.update(repository=str(destination / "suites" / case["id"] / "repository"),
                                    overlay=None, available=False,
                                    restore_invalidation="case_files_not_in_database_backup")
                        db.execute("UPDATE cases SET spec=? WHERE id=?", (json.dumps(spec), case["id"]))
                    except (ValueError, TypeError, AttributeError):
                        continue
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


def file_digest(path, check=None):
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Manifest source must be a regular file")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            if check:
                check()
            digest.update(block)
    return digest.hexdigest()


def overview(store, refresh=False):
    cached = getattr(store, "_storage_snapshot", None)
    if not refresh and cached and time.monotonic() - cached["clock"] < 10:
        result = copy.deepcopy(cached["data"])
        result["expiring"], result["delivering"], result["delivered"] = _approval_candidates(store)
        return result
    categories = {}
    for label, directory in [("runs", store.root / "runs"), ("backups", store.root / "backups"),
                             ("suites", store.root / "suites"), ("archives", store.root / "archives"),
                             ("installations", store.root / "installations")]:
        paths = directory.rglob("*")
        if label == "backups":
            paths = (p for p in paths if not p.name.startswith(".backup-"))
        categories[label] = sum(info.st_size for _, info in _file_info(paths))
    categories["database"] = sum(info.st_size for _, info in _file_info(store.root.glob("state.sqlite3*"), True))
    backups = sorted(_file_info((store.root / "backups").glob("*.sqlite3")),
                     key=lambda item: item[1].st_mtime, reverse=True)
    expiring, delivering, delivered = _approval_candidates(store)
    manifest = [{"path": p.relative_to(store.root).as_posix(), "bytes": info.st_size,
                 "modified": info.st_mtime, "sha256": file_digest(p)}
                for p, info in backups[:20]]
    result = {"bytes": categories, "backups": [{"id": p.stem, "bytes": info.st_size, "created": info.st_mtime}
            for p, info in backups[:20]], "backup_manifest": manifest,
            "expiring": expiring, "delivering": delivering, "delivered": delivered,
            "scanned_at": time.time(), "cache_seconds": 10}
    store._storage_snapshot = {"clock": time.monotonic(), "data": copy.deepcopy(result)}
    return result


def diagnostics(store):
    from . import __version__
    return {"version": __version__, "created": time.time(),
            "included": ["version", "state_counts", "failure_codes", "storage_bytes", "database_metrics",
                         "query_fingerprints_and_opaque_context", "schema_migrations", "relationship_identifiers"],
            "tasks": store.query("SELECT state,count(*) AS count FROM tasks GROUP BY state"),
            "runs": store.query("SELECT state,count(*) AS count FROM runs GROUP BY state"),
            "failures": store.query("SELECT json_extract(result,'$.failure.code') AS code,count(*) AS count FROM runs WHERE json_valid(result) AND json_extract(result,'$.failure.code') IS NOT NULL GROUP BY code"),
            "invalid_evidence": store.one("SELECT count(*) AS count FROM runs WHERE NOT json_valid(result)")["count"],
            "database": store.metrics(integrity=True),
            "wal": store.wal_diagnostics(),
            "schema_migrations": store.schema_migrations(),
            "relationship_audit": store.relationship_audit(),
            "quarantined_orphans": store.one("SELECT count(*) AS count FROM relationship_quarantine")
            if store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='relationship_quarantine'")
            else {"count": 0},
            "storage_bytes": overview(store)["bytes"]}
