"""Ordered additive migrations with explicitly identified legacy checksums."""
import hashlib
from pathlib import Path
import sqlite3
import time


SCRIPTS = ((3, "003_seal_bootstrap.sql"), (4, "004_revision_snapshots.sql"),
           (5, "005_task_group_lineage.sql"), (6, "006_dependency_dag.sql"))


def scripts():
    folder = Path(__file__).with_name("migrations")
    return [(version, (folder / filename).read_bytes()) for version, filename in SCRIPTS]


def validate_history(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
        return False
    expected = {version: hashlib.sha256(("adaptive-rsi-schema-v" + str(version)).encode()).hexdigest()
                for version in (1, 2)}
    expected.update({version: hashlib.sha256(content).hexdigest() for version, content in scripts()})
    applied = set()
    for row in db.execute("SELECT version,checksum FROM schema_migrations"):
        version = row["version"]
        if version not in expected:
            raise RuntimeError("Database migration history is newer than this runtime")
        if row["checksum"] != expected[version]:
            raise RuntimeError("Database migration checksum mismatch")
        applied.add(version)
    if applied and set(range(1, max(applied) + 1)) != applied:
        raise RuntimeError("Database migration history has a gap")
    return 3 in applied


def apply(db):
    for version, content in scripts():
        if db.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
            continue
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        # executescript commits implicitly; execute complete statements within the
        # same transaction as the ledger row so interrupted migrations roll back.
        statement = ""
        for line in content.decode("utf-8").splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                db.execute(statement)
                statement = ""
        if statement.strip():
            raise RuntimeError("Incomplete database migration statement")
        db.execute("INSERT INTO schema_migrations VALUES (?,?,?)",
                   (version, hashlib.sha256(content).hexdigest(), time.time()))
