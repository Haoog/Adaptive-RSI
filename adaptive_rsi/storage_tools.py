"""Consistent private backups and deliberately minimal shareable diagnostics."""
import os
import sqlite3
import stat
import tempfile
import time
from contextlib import closing
from pathlib import Path

from .store import Store, uid


def backup(store):
    folder = store.root / "backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / (uid() + ".sqlite3")
    fd, name = tempfile.mkstemp(prefix=".backup-", suffix=".tmp", dir=folder)
    staging = Path(name)
    try:
        os.close(fd)
        with store.connect() as source, closing(sqlite3.connect(str(staging))) as target:
            source.backup(target)
            # A portable snapshot must not require adjacent WAL/SHM files on restore.
            target.execute("PRAGMA journal_mode=DELETE")
        # Publish only the closed snapshot; a name collision must preserve the old backup.
        os.link(staging, path)
        return {"id": path.stem, "bytes": path.stat().st_size, "created": time.time()}
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(staging) + suffix).unlink(missing_ok=True)


def restore(source, destination):
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if destination.exists():
        raise ValueError("Restore requires a new state directory; existing state is never overwritten")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Backup integrity check failed")
        for table in ["projects", "tasks", "runs", "settings"]:
            db.execute("SELECT * FROM " + table + " LIMIT 0")
        destination.mkdir(mode=0o700, parents=True)
        target = destination / "state.sqlite3"
        with sqlite3.connect(str(target)) as out:
            db.backup(out)
        target.chmod(0o600)
    store = Store(destination)
    store.recover()
    store.execute("UPDATE projects SET paused=1")
    store.execute("UPDATE tasks SET state='expired' WHERE state='awaiting_approval'")
    store.execute("UPDATE runs SET state='expired' WHERE state='awaiting_approval'")
    # Machine paths and external credentials need reconnecting; pending side effects need reconciliation.
    store.execute("UPDATE tasks SET state='blocked' WHERE source IN ('system_publish','system_delivery') AND state='queued'")
    return {"restored": True, "paused": True, "artifacts_restored": False}


def _file_info(paths, follow_symlinks=False):
    for path in paths:
        try:
            info = path.stat() if follow_symlinks else path.lstat()
        except FileNotFoundError:
            continue  # SQLite sidecars can disappear during enumeration.
        if stat.S_ISREG(info.st_mode):
            yield path, info


def overview(store):
    categories = {}
    for label, directory in [("runs", store.root / "runs"), ("backups", store.root / "backups")]:
        paths = directory.rglob("*")
        if label == "backups":
            paths = (p for p in paths if not p.name.startswith(".backup-"))
        categories[label] = sum(info.st_size for _, info in _file_info(paths))
    categories["database"] = sum(info.st_size for _, info in _file_info(store.root.glob("state.sqlite3*"), True))
    backups = sorted(_file_info((store.root / "backups").glob("*.sqlite3"), True),
                     key=lambda item: item[1].st_mtime, reverse=True)
    return {"bytes": categories, "backups": [{"id": p.stem, "bytes": info.st_size, "created": info.st_mtime}
            for p, info in backups[:20]],
            "expiring": store.query("SELECT r.id,r.task,r.project,r.ended FROM runs r WHERE r.state='awaiting_approval' ORDER BY r.ended")}


def diagnostics(store):
    from . import __version__
    return {"version": __version__, "created": time.time(),
            "included": ["version", "state_counts", "failure_codes", "storage_bytes"],
            "tasks": store.query("SELECT state,count(*) AS count FROM tasks GROUP BY state"),
            "runs": store.query("SELECT state,count(*) AS count FROM runs GROUP BY state"),
            "failures": store.query("SELECT json_extract(result,'$.failure.code') AS code,count(*) AS count FROM runs WHERE json_extract(result,'$.failure.code') IS NOT NULL GROUP BY code"),
            "storage_bytes": overview(store)["bytes"]}
