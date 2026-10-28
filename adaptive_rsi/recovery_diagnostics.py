"""Offline, bounded recovery inspection without opening the original SQLite database."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import time


def inspect_database(source, timeout=10, max_mb=1024):
    if type(timeout) not in (int, float) or not 1 <= timeout <= 300:
        raise ValueError("Recovery inspection timeout must be 1..300 seconds")
    if type(max_mb) is not int or not 1 <= max_mb <= 102400:
        raise ValueError("Recovery inspection capacity must be 1..102400 MiB")
    source = Path(source).expanduser().absolute()
    deadline = time.monotonic() + timeout
    report = {"format": 1, "mode": "offline_copy", "source_modified": False,
              "contains": ["sizes", "hashes", "schema_version", "integrity", "table_counts"],
              "tables": [], "files": [], "errors": [], "consistent_source": True}

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError("Recovery inspection deadline exceeded")

    def identity(path):
        try:
            info = path.lstat()
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        except FileNotFoundError:
            return None

    paths = [source, Path(str(source) + "-wal")]
    before = {path: identity(path) for path in paths}
    if before[source] is None:
        raise ValueError("Recovery source does not exist")
    total = sum(info[2] for info in before.values() if info is not None)
    if total > max_mb * 1024 * 1024:
        raise ValueError("Recovery source exceeds capacity")
    with tempfile.TemporaryDirectory(prefix="rsi-recovery-inspect-") as directory:
        if shutil.disk_usage(directory).free < 2 * total + 16 * 1024 * 1024:
            raise ValueError("Insufficient disk space for recovery inspection")
        copied = Path(directory) / "state.sqlite3"
        for path in paths:
            if before[path] is None:
                continue
            check()
            target = copied if path == source else Path(str(copied) + "-wal")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            digest, count = hashlib.sha256(), 0
            with os.fdopen(descriptor, "rb") as raw, target.open("xb") as output:
                if not stat.S_ISREG(os.fstat(raw.fileno()).st_mode):
                    raise ValueError("Recovery source must be a regular file")
                while True:
                    check()
                    block = raw.read(65536)
                    if not block:
                        break
                    count += len(block)
                    if count > before[path][2]:
                        raise ValueError("Recovery source changed during inspection; stop the runtime and retry")
                    digest.update(block)
                    output.write(block)
            report["files"].append({"kind": "database" if path == source else "wal",
                                    "bytes": count, "sha256": digest.hexdigest()})
        report["consistent_source"] = all(identity(path) == before[path] for path in paths)
        if not report["consistent_source"]:
            report["errors"].append({"stage": "copy", "code": "source_changed"})
            return report
        try:
            # Only the disposable copy may perform SQLite WAL recovery.
            with closing(sqlite3.connect(str(copied), timeout=1)) as db:
                db.execute("PRAGMA trusted_schema=OFF")
                db.execute("PRAGMA query_only=ON")
                db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                report["schema_version"] = db.execute("PRAGMA user_version").fetchone()[0]
                try:
                    rows = db.execute("PRAGMA integrity_check(20)").fetchmany(20)
                    # Integrity messages can include user-controlled schema names.
                    report["integrity"] = "ok" if rows == [("ok",)] else "failed"
                except sqlite3.Error as exc:
                    report["errors"].append({"stage": "integrity", "code": type(exc).__name__})
                tables = [row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name LIMIT 101")]
                report["tables_truncated"] = len(tables) > 100
                for name in tables[:100]:
                    check()
                    # Never export settings, credentials, task text or raw damaged pages.
                    item = {"table": name if name in KNOWN_TABLES else "unrecognized"}
                    try:
                        quoted = '"' + name.replace('"', '""') + '"'
                        item["rows"] = db.execute("SELECT count(*) FROM " + quoted).fetchone()[0]
                    except sqlite3.Error as exc:
                        item["error"] = type(exc).__name__
                    report["tables"].append(item)
        except sqlite3.Error as exc:
            report["errors"].append({"stage": "database", "code": type(exc).__name__})
    return report


KNOWN_TABLES = {"settings", "projects", "tasks", "runs", "calls", "events", "cases", "skills",
                "candidates", "schedules", "stages", "task_revisions", "task_dependencies", "task_decisions",
                "submissions", "phase_times", "remote_waits", "task_completions", "schema_migrations",
                "request_log", "relationship_quarantine", "operations"}
