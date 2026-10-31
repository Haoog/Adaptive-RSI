"""Ordered additive migrations with explicitly identified legacy checksums."""
import hashlib
import logging
from pathlib import Path
import sqlite3
import time


LOGGER = logging.getLogger(__name__)

SCRIPTS = ((3, "003_seal_bootstrap.sql"), (4, "004_revision_snapshots.sql"),
           (5, "005_task_group_lineage.sql"), (6, "006_dependency_dag.sql"),
           (7, "007_call_prices.sql"), (8, "008_intervention_history_index.sql"))

# 账本缺行时用于判断迁移是否真的执行过的可见痕迹：说明 + 只读检查。
# 缺失的版本只能补账本、不能重放迁移，因此每一项都必须能由 schema 自身核对。
EVIDENCE = {
    1: ("base tables settings, projects, tasks, runs, events",
        lambda db, tables, indexes: {"settings", "projects", "tasks", "runs", "events"} <= tables),
    2: ("observability tables request_log, phase_times, remote_waits, task_completions",
        lambda db, tables, indexes: {"request_log", "phase_times", "remote_waits", "task_completions"} <= tables),
    3: ("the removed request_log_created index",
        lambda db, tables, indexes: "request_log" in tables and "request_log_created" not in indexes),
    4: ("table task_revision_snapshots",
        lambda db, tables, indexes: "task_revision_snapshots" in tables),
    5: ("table task_group_members with index task_group_original",
        lambda db, tables, indexes: "task_group_members" in tables and "task_group_original" in indexes),
    6: ("table task_dependencies(task,dependency) keyed on both columns",
        lambda db, tables, indexes: (_columns(db, "task_dependencies").get("task"),
                                     _columns(db, "task_dependencies").get("dependency")) == ((1, 1), (1, 2))),
    7: ("calls price columns input_price, output_price, cache_read_price, cache_write_price, price_basis",
        lambda db, tables, indexes: {"input_price", "output_price", "cache_read_price", "cache_write_price",
                                     "price_basis"} <= set(_columns(db, "calls"))),
    8: ("index events_intervention_task on events(project, task, seq)",
        lambda db, tables, indexes: "events_intervention_task" in indexes),
}


def scripts():
    folder = Path(__file__).with_name("migrations")
    return [(version, (folder / filename).read_bytes()) for version, filename in SCRIPTS]


def _expected():
    expected = {version: hashlib.sha256(("adaptive-rsi-schema-v" + str(version)).encode()).hexdigest()
                for version in (1, 2)}
    expected.update({version: hashlib.sha256(content).hexdigest() for version, content in scripts()})
    return expected


def has_ledger(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone() is not None


def _history(db):
    """Return the expected checksums and applied versions, rejecting foreign or rewritten history."""
    expected = _expected()
    applied = set()
    for row in db.execute("SELECT version,checksum FROM schema_migrations"):
        version = row["version"]
        if version not in expected:
            raise RuntimeError("Database migration history is newer than this runtime")
        if row["checksum"] != expected[version]:
            raise RuntimeError("Database migration checksum mismatch")
        applied.add(version)
    return expected, applied


def _tables(db):
    return {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(db):
    return {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _columns(db, table):
    # (notnull, pk) 足以区分 006 前后的单列主键与复合主键，无需读取数据。
    return {row["name"]: (row["notnull"], row["pk"]) for row in db.execute("PRAGMA table_info(" + table + ")")}


def _effect_visible(db, version, tables, indexes):
    # 未登记判据的版本（新增迁移还没补充痕迹检查）一律不可确认，交给 apply() 正常执行。
    evidence = EVIDENCE.get(version)
    return bool(evidence and evidence[1](db, tables, indexes))


def heal(db):
    """Backfill ledger rows for migrations whose effects are already visible.

    只写账本行，不重放迁移、不改动业务数据。缺口处看不到对应痕迹、却已经记录了
    更高版本时抛错：这种状态无法确认是中断还是人工删行，继续启动会掩盖问题，
    应当由维护者核对或恢复备份。

    Returns the backfilled versions.
    """
    if not has_ledger(db):
        return ()
    expected, applied = _history(db)
    tables, indexes = _tables(db), _indexes(db)
    highest = max(applied, default=0)
    healed = []
    for version in sorted(expected):
        if version in applied:
            continue
        if not _effect_visible(db, version, tables, indexes):
            # 从这个版本起没有任何生效痕迹：交给 apply() 按顺序正常执行。
            # 但账本已经记录了更高版本时状态无法确认，明确报错而不是静默跳过。
            if highest > version:
                raise RuntimeError(
                    "Database migration history has a gap at version %d: %s is missing, but version %d is "
                    "recorded; restore a backup or repair the ledger manually"
                    % (version, EVIDENCE.get(version, ("the migration schema effect",))[0], highest))
            break
        db.execute("INSERT INTO schema_migrations VALUES (?,?,?)",
                   (version, expected[version], time.time()))
        healed.append(version)
    if healed:
        LOGGER.warning("Backfilled migration ledger rows for version(s) %s without replaying migrations",
                       ", ".join(str(version) for version in healed))
    return tuple(healed)


def validate_history(db):
    if not has_ledger(db):
        return False
    _, applied = _history(db)
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
